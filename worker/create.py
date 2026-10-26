"""Explicit owner-only creation and enrollment of one approved private worker."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile

if __package__:
    from . import enroll
    from .policy import Policy
else:
    import enroll
    from policy import Policy


def approved(cloud):
    properties = cloud.template
    if (set(properties) != {'machineType', 'canIpForward', 'serviceAccounts',
                            'networkInterfaces', 'disks', 'tags', 'labels', 'metadata'}
            or properties['canIpForward'] is not False or properties['serviceAccounts'] != []
            or properties['tags'] != {'items': ['ci-buddy-worker']}
            or properties['labels'] != {'ci-buddy-enrollment': 'ready'}):
        raise ValueError('creation requires the exact private worker profile')
    interfaces, disks = properties['networkInterfaces'], properties['disks']
    if len(interfaces) != 1 or len(disks) != 1:
        raise ValueError('creation requires one private NIC and one approved boot disk')
    nic, disk = interfaces[0], disks[0]
    if (set(nic) != {'subnetwork', 'stackType', 'accessConfigs'}
            or nic['stackType'] != 'IPV4_ONLY' or nic['accessConfigs'] != []
            or set(disk) != {'boot', 'autoDelete', 'type', 'initializeParams'}
            or disk['boot'] is not True or disk['autoDelete'] is not True
            or disk['type'] != 'PERSISTENT'
            or set(disk['initializeParams']) != {'sourceImage', 'diskSizeGb'}):
        raise ValueError('public networking or additional disks are not approved')
    for field, name in (('startup-script', 'bootstrap.sh'),
                        ('ci-buddy-firewall-template', 'firewall.nft.in')):
        if cloud.values[field] != (Path(__file__).parent / name).read_text():
            raise ValueError('profile must use the reviewed bootstrap and firewall verbatim')
    for field, expected in (('enable-guest-attributes', 'true'),
                            ('block-project-ssh-keys', 'true'), ('enable-oslogin', 'false')):
        if cloud.values.get(field, '').lower() != expected:
            raise ValueError('profile must retain independent guest pins and restricted SSH')
    project = cloud.json('compute', 'project-info', 'describe')
    zonal = cloud.json('compute', 'project-zonal-metadata', 'describe', '--zone', cloud.zone)
    for inherited in (project.get('commonInstanceMetadata', {}), zonal):
        if {row['key'] for row in inherited.get('items', [])} & {'startup-script', 'startup-script-url', 'user-data'}:
            raise ValueError('inherited startup overrides are not approved')
    image = disk['initializeParams']['sourceImage']
    if not re.fullmatch(r'https://www\.googleapis\.com/compute/v1/projects/[A-Za-z0-9_-]+/global/images/[A-Za-z0-9_-]+', image):
        raise ValueError('provide the approved immutable image self-link')
    actual = cloud.json('compute', 'images', 'describe', image)
    if str(actual.get('id')) != cloud.values['ci-buddy-image-id'] or actual.get('status') != 'READY':
        raise ValueError('source image no longer has the approved identity')
    return nic, disk['initializeParams']


def claim(path, request):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('creation claim directory must be owned and private')
    if subprocess.run(['git', '-C', str(path.parent), 'rev-parse', '--git-dir'],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError('creation claims must live outside Git repositories')
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as output:
        json.dump(request, output)
        output.flush()
        os.fsync(output.fileno())


def wait_bootstrap(cloud):
    command = ['gcloud', '--quiet', '--project', cloud.project, 'compute', 'instances',
               'tail-serial-port-output', cloud.instance, '--zone', cloud.zone]
    marker, failure, previous, count = b'ci-buddy-bootstrap-ready', b'ci-buddy-bootstrap-failed', b'', 0
    # Native tail owns polling. No serial output or credentials reach the terminal.
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            while chunk := process.stdout.read1(4096):
                count += len(chunk)
                if count > 8 * 1024 * 1024:
                    raise ValueError('bootstrap serial output exceeded its byte limit')
                if failure in previous + chunk:
                    raise ValueError('worker first boot failed; inspect its provider serial log')
                if marker in previous + chunk:
                    return
                previous = (previous + chunk)[-len(marker):]
            raise ValueError('bootstrap serial stream ended before readiness')
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            process.wait()


def create(args):
    cloud = enroll.Cloud(args)
    nic, disk = approved(cloud)
    policy = Policy(cloud.secrets / 'tailscale-token', cloud.values['ci-buddy-tailnet'], enroll.peer_addresses(cloud.values))
    policy.verify(json.loads((cloud.profile / 'tailnet-policy.json').read_text()))
    enroll.source_archive()
    if cloud.json('compute', 'instances', 'list', '--filter=labels.ci-buddy-enrollment:*') != []:
        raise ValueError('this project already has a CI worker; inspect it before creating another')
    request = {'project': cloud.project, 'zone': cloud.zone,
               'body': dict(cloud.template, name=cloud.instance)}
    claim(Path.home() / '.local/state/ci-buddy' / ('create-' + cloud.project + '.json'), request)
    claim(cloud.profile / 'create-claim.json', request)
    try:
        with tempfile.TemporaryDirectory(prefix='create-', dir=cloud.secrets) as directory:
            files = []
            for index, (name, value) in enumerate(cloud.values.items()):
                path = Path(directory) / str(index)
                path.write_text(value)
                files.append(name + '=' + str(path))
            # A custom gcloud list separator also supports commas in owner paths.
            separator = '|'
            while separator in directory:
                separator += '|'
            cloud.command('compute', 'instances', 'create', cloud.instance, '--zone', cloud.zone,
                '--no-address', '--no-service-account', '--no-scopes', '--stack-type=IPV4_ONLY',
                '--machine-type', cloud.template['machineType'], '--subnet', nic['subnetwork'],
                '--image', disk['sourceImage'], '--boot-disk-size', str(disk['diskSizeGb']),
                '--tags=ci-buddy-worker', '--labels=ci-buddy-enrollment=ready',
                '--metadata-from-file=^' + separator + '^' + separator.join(files))
        wait_bootstrap(cloud)
        enroll.enroll(args)
    except BaseException:
        print('Worker ' + args.vm + ' may exist and incur charges. Claims retained; inspect it explicitly. '
              'No VM was deleted and creation will not be retried.', file=sys.stderr)
        raise


def main():
    if os.name != 'posix':
        raise ValueError('run owner creation from Linux/WSL')
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vm', help='project/zone/instance; creation is explicitly billable')
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--project')
    parser.add_argument('--dependency', action='append', default=[])
    create(parser.parse_args())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('ci-buddy worker create: ' + str(error)) from None
