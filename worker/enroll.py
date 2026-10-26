"""Owner-side enrollment of an existing private VM from an approved profile."""
import argparse
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request

if __package__:
    from .policy import NoRedirect, Policy
else:
    from policy import NoRedirect, Policy


def metadata(instance):
    rows = instance.get('metadata', {}).get('items', [])
    values = {row['key']: row['value'] for row in rows}
    if len(values) != len(rows):
        raise ValueError('duplicate instance metadata')
    return values


def peer_addresses(values):
    return sorted({str(ipaddress.ip_address(item.strip())) for name in
        ('ci-buddy-owner-addresses', 'ci-buddy-coordinator-addresses')
        for item in values[name].split(',')})


def validate_instance(instance, disk, image_id, startup_hash):
    """Provider data is checked before trusting a newly booted worker."""
    values = metadata(instance)
    if (instance.get('status') != 'RUNNING' or instance.get('canIpForward') is not False
            or instance.get('serviceAccounts') or disk.get('sourceImageId') != image_id
            or instance.get('labels', {}).get('ci-buddy-enrollment') != 'ready'
            or values.get('enable-guest-attributes', '').lower() != 'true'
            or values.get('block-project-ssh-keys', '').lower() != 'true'
            or values.get('enable-oslogin', '').lower() != 'false'
            or 'startup-script-url' in values
            or hashlib.sha256(values.get('startup-script', '').encode()).hexdigest() != startup_hash):
        raise ValueError('instance is not a fresh approved private worker')
    interfaces = instance.get('networkInterfaces', [])
    disks = instance.get('disks', [])
    if len(interfaces) != 1 or len(disks) != 1 or disks[0].get('boot') is not True:
        raise ValueError('worker needs one private NIC and its approved boot disk')
    interface = interfaces[0]
    if (interface.get('accessConfigs') or interface.get('ipv6AccessConfigs')
            or interface.get('externalIpv6') or interface.get('aliasIpRanges')
            or not ipaddress.ip_address(interface['networkIP']).is_private):
        raise ValueError('public or additional worker networking is forbidden')


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError('credential must be an owned private regular file')
    return path


def capture(command, *, secret_dir, data=b'', limit=2**20):
    # A regular input file avoids pipe deadlock and stays inside host secrets.
    with tempfile.TemporaryFile(dir=secret_dir) as source:
        source.write(data)
        source.seek(0)
        with subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, start_new_session=True) as process:
            try:
                output = process.stdout.read(limit + 1)
                if len(output) > limit:
                    raise ValueError('enrollment response exceeds limit')
                status = process.wait()
                if status:
                    raise ValueError('enrollment operation failed; remote output withheld')
                return output
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait()
                raise


class Cloud:
    def __init__(self, args):
        self.args = args
        pieces = args.vm.split('/')
        if len(pieces) != 3 or any(not re.fullmatch(r'[A-Za-z0-9_-]+', value) for value in pieces):
            raise ValueError('provide project/zone/instance')
        self.project, self.zone, self.instance = pieces
        self.profile = Path(args.profile).resolve()
        if subprocess.run(['git', '-C', str(self.profile), 'rev-parse', '--git-dir'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise ValueError('worker profiles containing secrets must stay outside Git repositories')
        self.secrets = self.profile / 'secrets'
        self.secrets.mkdir(mode=0o700, exist_ok=True)
        info = self.secrets.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('profile secrets directory must be private and owned')
        self.key = private_file(self.secrets / 'owner-ssh')
        self.dependency_keys = []
        self.registration_started = False
        self.template = json.loads((self.profile / 'instance-template.json').read_text())['properties']
        self.values = metadata(self.template)
        self.account = self.values['ci-buddy-account']
        self.root = self.values['ci-buddy-root']
        if not re.fullmatch(r'[a-z_][a-z0-9_-]*', self.account):
            raise ValueError('invalid runner account')
        if not self.root.startswith('/') or any(part in ('', '.', '..') for part in self.root[1:].split('/')) or any(c.isspace() for c in self.root):
            raise ValueError('root must be a normalized absolute path without whitespace')

    def json(self, *arguments):
        return json.loads(self.command(*arguments))

    def command(self, *arguments):
        return capture(['gcloud', '--quiet', '--project', self.project,
            '--format=json', *arguments], secret_dir=self.secrets)

    def inspect(self):
        instance = self.json('compute', 'instances', 'describe', self.instance, '--zone', self.zone)
        disk = self.json('compute', 'disks', 'describe', instance['disks'][0]['source'].rsplit('/', 1)[-1], '--zone', self.zone)
        digest = hashlib.sha256(self.values['startup-script'].encode()).hexdigest()
        validate_instance(instance, disk, self.values['ci-buddy-image-id'], digest)
        if (metadata(instance) != self.values or instance['machineType'].rsplit('/', 1)[-1] != self.template['machineType'].rsplit('/', 1)[-1]
                or instance.get('tags', {}).get('items', []) != self.template.get('tags', {}).get('items', [])
                or instance['networkInterfaces'][0]['subnetwork'] != self.template['networkInterfaces'][0]['subnetwork']
                or instance['networkInterfaces'][0].get('stackType') != self.template['networkInterfaces'][0]['stackType']):
            raise ValueError('instance differs from the approved native template')
        project = self.json('compute', 'project-info', 'describe')
        zonal = self.json('compute', 'project-zonal-metadata', 'describe', '--zone', self.zone)
        for inherited in (project.get('commonInstanceMetadata', {}), zonal):
            keys = {row['key'] for row in inherited.get('items', [])}
            if keys & {'startup-script', 'startup-script-url', 'user-data'}:
                raise ValueError('inherited startup overrides are not approved')
        for field, name in (('startup-script', 'bootstrap.sh'),
                            ('ci-buddy-firewall-template', 'firewall.nft.in')):
            if self.values[field] != (Path(__file__).parent / name).read_text():
                raise ValueError('profile must use the reviewed bootstrap and firewall verbatim')
        effective = self.json('compute', 'instances', 'network-interfaces', 'get-effective-firewalls',
            self.instance, '--zone', self.zone, '--network-interface', 'nic0')
        if effective != json.loads((self.profile / 'firewalls.json').read_text()):
            raise ValueError('effective cloud firewall differs from owner-approved rules')
        self.identifier = str(instance['id'])
        if not self.identifier.isdecimal():
            raise ValueError('invalid native instance identity')
        try:
            ready = self.json('compute', 'instances', 'get-guest-attributes', self.instance,
                             '--zone', self.zone, '--query-path', 'ci-buddy/')
        except ValueError:
            raise ValueError('bootstrap readiness evidence unavailable; enrollment has not started') from None
        if not any(row.get('key') == 'bootstrap' and row.get('value') == 'ready'
                   for row in ready.get('queryValue', {}).get('items', [])):
            raise ValueError('worker bootstrap is not ready; enrollment has not started')
        host = self.json('compute', 'instances', 'get-guest-attributes', self.instance,
                         '--zone', self.zone, '--query-path', 'hostkeys/')
        keys = [row for row in host.get('queryValue', {}).get('items', []) if row.get('key') == 'ssh-ed25519']
        if len(keys) != 1 or not re.fullmatch(r'[A-Za-z0-9+/]+={0,3}', keys[0]['value']):
            raise ValueError('fresh worker has no independent host-key pin')
        self.known = self.secrets / ('known-hosts-' + self.identifier)
        self.known.write_text('ci-buddy-' + self.identifier + ' ssh-ed25519 ' + keys[0]['value'] + '\n')
        self.known.chmod(0o600)
        self.network_ip = instance['networkInterfaces'][0]['networkIP']
        return instance

    def ssh(self, command, data=b''):
        parent = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(parent))
        from ssh import OPTIONS
        config = self.secrets / 'ssh-empty-config'
        config.touch(mode=0o600, exist_ok=True)
        proxy = shlex.join(['gcloud', '--quiet', '--project', self.project, 'compute',
            'start-iap-tunnel', self.instance, '22', '--zone', self.zone, '--listen-on-stdin'])
        # This reviewed owner-only bootstrap channel is never a runtime job proxy.
        return capture(['ssh', '-F', str(config), '-o', 'ProxyCommand=' + proxy, *OPTIONS,
            '-o', 'GlobalKnownHostsFile=none', '-o', 'UserKnownHostsFile=' + str(self.known),
            '-o', 'HostKeyAlias=ci-buddy-' + self.identifier, '-i', str(self.key),
            self.account + '@' + self.network_ip, command], secret_dir=self.secrets, data=data)

    def label(self, value):
        self.command('compute', 'instances', 'add-labels', self.instance, '--zone', self.zone,
                     '--labels', 'ci-buddy-enrollment=' + value)


def source_archive():
    source = Path(__file__).resolve().parent.parent
    if (source / 'revision').is_file():
        return (source / 'revision').read_text().strip(), (source / 'runtime.tar').read_bytes()
    subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', '--quiet', 'HEAD'], check=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    archive = subprocess.check_output(['git', '-C', str(source), 'archive', '--format=tar',
                                      '--add-virtual-file=revision:' + revision + '\n', 'HEAD'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        if any(not entry.isfile() and not entry.isdir() for entry in bundle):
            raise ValueError('reviewed runtime archive contains a link or special file')
    return revision, archive


def install_worker(cloud, revision, archive, auth_key):
    # The only secret crossing bootstrap is a one-use worker key on protected stdin.
    program = '''import io,json,os,pathlib,subprocess,sys,tarfile
os.umask(0o077)
root=pathlib.Path(sys.argv[1]); revision=sys.argv[2]
assert root.resolve()==root and root.stat().st_uid==os.getuid()
stage = root / 'install' / revision
assert not stage.exists(), 'already staged; inspect the failed enrollment'
stage.mkdir(parents=True)
payload=sys.stdin.buffer.read()
length=int.from_bytes(payload[:8],'big')
assert 0<length<4096
secret=root/'secrets'/'tailscale-enrollment'
secret.parent.mkdir(mode=0o700,exist_ok=True)
with secret.open('xb') as output: output.write(payload[8:8+length])
try:
 subprocess.run(['tailscale','up','--auth-key=file:'+str(secret),'--advertise-tags=tag:ci-worker',
  '--accept-routes=false','--accept-dns=false','--ssh=false','--hostname=ci-buddy-'+sys.argv[3]],check=True,
  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
finally: secret.unlink(missing_ok=True)
with tarfile.open(fileobj=io.BytesIO(payload[8+length:])) as bundle:
 bundle.extractall(stage,filter='data')
subprocess.run(['bash',str(stage/'install.sh'),'--root',str(root)],check=True,
 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
assert (root/'bin/revision').read_text().strip()==revision
print(subprocess.check_output(['tailscale','ip','-4'],text=True).strip())
'''
    secret = auth_key.encode()
    address = cloud.ssh(shlex.join(['python3', '-c', program, cloud.root, revision, cloud.identifier]),
                        len(secret).to_bytes(8, 'big') + secret + archive).decode().strip()
    if ipaddress.ip_address(address) not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('worker did not return a tailnet IPv4 address')
    cloud.address = address
    return cloud.account + '@' + address + ':' + cloud.root


def github(cloud, method, path, body=None):
    token = private_file(cloud.secrets / 'github-token').read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('invalid owner GitHub token')
    request = urllib.request.Request('https://api.github.com' + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request) as response:
            raw = response.read(2**20 + 1)
    except urllib.error.HTTPError as error:
        raise ValueError(f'owner GitHub request rejected: HTTP {error.code}') from None
    if len(raw) > 2**20:
        raise ValueError('GitHub response exceeds limit')
    return json.loads(raw) if raw else None


def dependencies(cloud, project, repositories):
    created = cloud.dependency_keys
    for repository in repositories:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('dependency must be an explicit GitHub owner/repository')
        name = hashlib.sha256(repository.encode()).hexdigest()[:16]
        directory = str(Path(cloud.root, 'projects', project, 'secrets'))
        program = '''import json,os,pathlib,subprocess,sys
os.umask(0o077)
root=pathlib.Path(sys.argv[1]); name=sys.argv[2]
root.mkdir(mode=0o700,parents=True,exist_ok=True)
ssh=root/'ssh';ssh.mkdir(mode=0o700,exist_ok=True)
key=ssh/name
subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)],check=True)
key.with_suffix('.pub').chmod(0o600)
print(key.with_suffix('.pub').read_text().strip())
'''
        public = cloud.ssh(shlex.join(['python3', '-c', program, directory, name])).decode().strip()
        response = github(cloud, 'POST', '/repos/' + repository + '/keys',
                          {'title': 'ci-buddy-' + cloud.identifier, 'key': public, 'read_only': True})
        created.append((repository, response['id']))
        if response.get('read_only') is not True:
            raise ValueError('dependency key was not registered read-only')
        keys = github(cloud, 'GET', '/meta')['ssh_keys']
        config = ('Host dependency-' + name + '\n HostName github.com\n User git\n'
                  ' IdentitiesOnly yes\n StrictHostKeyChecking yes\n'
                  ' UserKnownHostsFile /run/secrets/ssh/known_hosts\n'
                  ' IdentityFile /run/secrets/ssh/' + name + '\n')
        rewrite = '[url "ssh://git@dependency-' + name + '/' + repository + '"]\n insteadOf = https://github.com/' + repository + '\n'
        program = '''import json,os,pathlib,sys
os.umask(0o077)
root=pathlib.Path(sys.argv[1]);value=json.load(sys.stdin)
with (root/'ssh'/'config').open('a') as output: output.write(value['config'])
(root/'ssh'/'known_hosts').write_text(''.join('github.com '+key+'\\n' for key in value['keys']))
with (root/'gitconfig').open('a') as output: output.write(value['rewrite'])
'''
        cloud.ssh(shlex.join(['python3', '-c', program, directory]),
                  json.dumps(dict(config=config, rewrite=rewrite, keys=keys)).encode())
    return created


def private_ssh(cloud, command, key=None):
    parent = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(parent))
    from ssh import OPTIONS
    config = cloud.secrets / 'ssh-empty-config'
    config.touch(mode=0o600, exist_ok=True)
    return capture(['ssh', '-F', str(config), *OPTIONS, '-o', 'IdentityAgent=none',
        '-o', 'GlobalKnownHostsFile=none', '-o', 'UserKnownHostsFile=' + str(cloud.known),
        '-o', 'HostKeyAlias=ci-buddy-' + cloud.identifier, '-i', str(key or cloud.key),
        cloud.account + '@' + cloud.address, command], secret_dir=cloud.secrets)


def verify_worker(cloud, args, endpoint):
    command = shlex.join(['bash', str(Path(cloud.root, 'bin/ci-buddy')), '--server', 'example', 'version'])
    if private_ssh(cloud, command).decode().strip() != args.revision:
        raise ValueError('private runtime identity or readiness differs')
    wrong = cloud.secrets / ('wrong-key-' + cloud.identifier)
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(wrong)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        try:
            private_ssh(cloud, 'true', wrong)
        except ValueError:
            pass
        else:
            raise ValueError('worker accepted the wrong SSH key')
        private_ssh(cloud, command)
    finally:
        wrong.unlink(missing_ok=True)
        wrong.with_suffix('.pub').unlink(missing_ok=True)
    # Test a listening host service, not an unused port that would fail anyway.
    program = '''import json,socket,subprocess,sys
info=json.loads(subprocess.check_output(['podman','info','--format=json']))
assert info['host']['security']['rootless'] is True
assert info['host']['cgroupVersion']=='v2'
blocked=json.loads(sys.argv[1])
with socket.socket() as listener:
 listener.bind(('0.0.0.0',0));listener.listen()
 try: socket.create_connection((sys.argv[2],listener.getsockname()[1])).close()
 except OSError: pass
 else: raise AssertionError('listening host service reachable')
 # TCP waits for a connection result; a UDP enqueue says nothing about pasta egress.
 probe="import json,socket,sys,urllib.request\\nif sys.argv[2]=='online': urllib.request.urlopen('https://api.github.com/meta').read(1)\\nfor address,port in json.loads(sys.argv[1]):\\n try: socket.create_connection((address,port)).close()\\n except OSError: pass\\n else: raise AssertionError('prohibited service reachable')"
 subprocess.run(['podman','run','--rm','--network=pasta','--memory=256m','--memory-swap=256m',
  '--pids-limit=64','docker.io/library/python:3.13-slim','python3','-c',probe,json.dumps(blocked),'online'],check=True,
  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 subprocess.run(['podman','run','--rm','--network=none','--memory=256m','--memory-swap=256m',
  '--pids-limit=64','docker.io/library/python:3.13-slim','python3','-c',probe,
  json.dumps([['1.1.1.1',443]]),'offline'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print('admission probes passed')
'''
    addresses = cloud.values['ci-buddy-owner-addresses'] + ',' + cloud.values['ci-buddy-coordinator-addresses']
    blocked = [[str(ipaddress.ip_address(value.strip())), 22] for value in addresses.split(',')]
    blocked += [['169.254.169.254', 80]]
    command = shlex.join(['python3', '-c', program, json.dumps(blocked), cloud.network_ip])
    if private_ssh(cloud, command).decode().strip() != 'admission probes passed':
        raise ValueError('rootless network admission failed')


def ssh_block(cloud, key, known):
    return ('Host ci-buddy-' + cloud.identifier + '\n HostName ' + cloud.address + '\n User ' + cloud.account
            + '\n IdentityFile "' + str(key) + '"\n UserKnownHostsFile "' + str(known)
            + '"\n HostKeyAlias ci-buddy-' + cloud.identifier + '\n IdentitiesOnly yes\n\n')


def register_worker(args, endpoint, cloud):
    # Registration is last. Only the owner writes host pools, never submitted source.
    alias = 'ci-buddy-' + cloud.identifier
    endpoint = alias + ':' + cloud.root
    client = Path.home() / '.local/lib/ci-buddy/ssh_config'
    client.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if client.exists():
        private_file(client)
    before = client.read_text() if client.exists() else ''
    block = ssh_block(cloud, cloud.key, cloud.known)
    cloud.registration_started = True
    coordinator = cloud.values.get('ci-buddy-coordinator', '')
    if coordinator:
        if ':' not in coordinator:
            raise ValueError('coordinator must include its absolute root')
        destination, root = coordinator.split(':', 1)
        if not root.startswith('/'):
            raise ValueError('coordinator root must be absolute')
        program = '''import fcntl,json,os,pathlib,subprocess,sys,tomllib
os.umask(0o077)
root=pathlib.Path(sys.argv[1]); value=json.load(sys.stdin)
with (root/'enrollment.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 assert (root/'bin/revision').read_text().strip()==value['revision']
 key=root/'secrets/worker-transport'
 public=subprocess.check_output(['ssh-keygen','-y','-f',str(key)],text=True).strip()
 assert public==' '.join(value['coordinator_key'].split()[:2]), 'unapproved coordinator key'
 config=root/'ssh_config';known=root/'secrets'/('known-hosts-'+value['identifier'])
 for path in (config,known):
  assert not path.is_symlink()
  if path.exists(): assert path.stat().st_uid==os.getuid() and path.stat().st_mode&0o077==0
 conf=root/'host.conf';before=conf.read_text();old=config.read_text() if config.exists() else ''
 limits=tomllib.loads(before)
 assert value['endpoint'] not in limits['workers'], 'already registered'
 limits['workers'].append(value['endpoint'])
 body='\\n'.join(name+' = '+json.dumps(item) for name,item in limits.items())+'\\n'
 block=value['block'].replace('@KEY@',str(key)).replace('@KNOWN@',str(known))
 try:
  known.write_text(value['pin']);known.chmod(0o600)
  config.write_text(block+old);config.chmod(0o600)
  conf.write_text(body)
  subprocess.run([str(root/'bin/pueue'),'--config',str(root/'pueue.yml'),'parallel','--group','jobs',
   str(limits['jobs']*(1+len(limits['workers'])))],check=True,stdout=subprocess.DEVNULL)
 except BaseException:
  conf.write_text(before)
  if old: config.write_text(old)
  else: config.unlink(missing_ok=True)
  known.unlink(missing_ok=True)
  raise
'''
        parent = Path(__file__).resolve().parent.parent
        payload = dict(revision=args.revision, endpoint=endpoint, identifier=cloud.identifier,
            coordinator_key=cloud.values['ci-buddy-coordinator-key'],
            block=ssh_block(cloud, '@KEY@', '@KNOWN@'), pin=cloud.known.read_text())
        command = shlex.join(['python3', '-c', program, root])
        invocation = (['python3', '-c', program, root] if destination == 'local' else
                      [sys.executable, str(parent / 'ssh.py'), destination, command])
        capture(invocation,
                secret_dir=cloud.secrets, data=json.dumps(payload).encode())
    client.write_text(block + before)
    client.chmod(0o600)
    subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', endpoint], check=True)


def enroll(args):
    cloud = Cloud(args)
    approved = json.loads((cloud.profile / 'tailnet-policy.json').read_text())
    policy = Policy(cloud.secrets / 'tailscale-token', cloud.values['ci-buddy-tailnet'], peer_addresses(cloud.values))
    registered = False
    claimed = False
    try:
        print('Checking independent provider and network policy', flush=True)
        instance = cloud.inspect()
        cloud.network_ip = instance['networkInterfaces'][0]['networkIP']
        policy.verify(approved)
        args.revision, archive = source_archive()
        project = args.project
        if project is None and Path('ci-buddy.toml').exists():
            project = tomllib.loads(Path('ci-buddy.toml').read_text())['name']
        if args.dependency and (not project or not re.fullmatch(r'[A-Za-z0-9_-]+', project)):
            raise ValueError('private dependencies require a valid explicit project')
        if args.dependency:
            private_file(cloud.secrets / 'github-token')
        cloud.label('pending')
        claimed = True
        key_id, key = policy.new_key()
        print('Installing reviewed runtime and worker identity', flush=True)
        endpoint = install_worker(cloud, args.revision, archive, key)
        policy.device(cloud.address, expected_hostname='ci-buddy-' + cloud.identifier)
        dependencies(cloud, project, args.dependency)
        print('Checking private access and rootless network denials', flush=True)
        verify_worker(cloud, args, endpoint)
        policy.verify(approved)
        policy.revoke(key_id)
        cloud.label('enrolled')
        register_worker(args, endpoint, cloud)
        registered = True
        print('Enrolled ci-buddy-' + cloud.identifier + '; normal jobs may now use its warm slots', flush=True)
    finally:
        if not registered:
            unresolved = policy.abort()
            for repository, identifier in cloud.dependency_keys:
                try:
                    github(cloud, 'DELETE', '/repos/' + repository + '/keys/' + str(identifier))
                except (OSError, ValueError):
                    unresolved.append('dependency-key:' + str(identifier))
            if claimed:
                try:
                    cloud.label('failed')
                except (OSError, ValueError):
                    unresolved.append('provider enrollment label')
            if cloud.registration_started:
                unresolved.append('possible partial host registration; inspect owner/coordinator configuration')
            if unresolved:
                print('Enrollment disabled; owner cleanup required for ' + ', '.join(unresolved), file=sys.stderr)


def main():
    if os.name != 'posix':
        raise ValueError('run owner enrollment from Linux/WSL; the normal job client also supports Windows')
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vm', help='project/zone/instance created from the approved native template')
    parser.add_argument('--profile', type=Path, required=True, help='owner-approved native exports and local secrets')
    parser.add_argument('--project', help='Rust project name; defaults to the current ci-buddy.toml')
    parser.add_argument('--dependency', action='append', default=[], help='explicit private GitHub owner/repository')
    enroll(parser.parse_args())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print('ci-buddy enrollment: ' + str(error), file=sys.stderr)
        sys.exit(1)
