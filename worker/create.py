"""Owner-only Google worker creation, using the ordinary container runner."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tomllib


def private(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('use an owned private regular secret file')
    return path


def runtime():
    source = Path(__file__).resolve().parent.parent
    if (source / 'revision').is_file():
        return (source / 'revision').read_text().strip(), (source / 'runtime.tar').read_bytes()
    subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD'], check=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    return revision, subprocess.check_output(['git', '-C', str(source), 'archive',
        '--add-virtual-file=revision:' + revision + '\n', 'HEAD'])


def exchange(command, key_file):
    # A public nonce proves echo is disabled now, rather than in replayed output.
    nonce = secrets.token_hex(16)
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            process.stdin.write(('CI_BUDDY_CHALLENGE ' + nonce + '\n').encode())
            process.stdin.flush()
            total, sent = 0, False
            while line := process.stdout.readline(8193):
                total += len(line)
                if len(line) > 8192 or total > 1024 * 1024:
                    raise ValueError('bootstrap output exceeds its byte limit')
                line = line.strip()
                if line == ('CI_BUDDY_KEY_READY ' + nonce).encode() and not sent:
                    try:
                        process.stdin.write(private(key_file).read_bytes().strip() + b'\n')
                        process.stdin.flush()
                    finally:
                        key_file.unlink()
                    sent = True
                if line == b'CI_BUDDY_FAILED':
                    raise ValueError('worker first boot failed')
                if sent and line.startswith(b'CI_BUDDY_READY '):
                    value = json.loads(line[15:])
                    try:
                        address = ipaddress.ip_address(value['ip'])
                    except (ValueError, TypeError, KeyError):
                        raise ValueError('invalid private worker identity') from None
                    if (address not in ipaddress.ip_network('100.64.0.0/10')
                            or not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,3}', value['hostkey'])):
                        raise ValueError('invalid private worker identity')
                    blob = base64.b64decode(value['hostkey'].split()[1], validate=True)
                    if len(blob) != 51 or blob[:19] != b'\0\0\0\x0bssh-ed25519\0\0\0\x20':
                        raise ValueError('invalid ed25519 host-key encoding')
                    return value
            raise ValueError('bootstrap connection ended; no retry')
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            process.wait()


def create(args):
    project, zone, name = args.vm.split('/')
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', item) for item in (project, zone, name)):
        raise ValueError('provide project/zone/instance')
    if not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise ValueError('use a valid Google instance name')
    if not args.root.startswith('/') or len(Path(args.root).parts) < 3 or '..' in Path(args.root).parts or str(Path(args.root)) != args.root or any(c.isspace() for c in args.root):
        raise ValueError('use a normalized absolute worker root without whitespace')
    key_file = private(args.auth_key_file.expanduser().absolute())
    if 'secrets' not in key_file.parts or key_file.parent.stat().st_mode & 0o077:
        raise ValueError('keep the join key in an owner-only secrets directory')
    if subprocess.run(['git', '-C', str(key_file.parent), 'rev-parse', '--git-dir'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError('secrets must stay outside Git repositories')
    if not re.fullmatch(rb'tskey-auth-[A-Za-z0-9_-]{1,4085}', key_file.read_bytes().strip()):
        raise ValueError('provide a single-use ephemeral pre-tagged auth key file')
    limits = tomllib.loads(args.host_conf.read_text())
    required = {'jobs', 'slots', 'memory', 'cpus', 'min_free_gb', 'slot_max_gb', 'workers'}
    if required - limits.keys() or limits['workers'] != [] or any(type(limits[n]) is not int or limits[n] <= 0 for n in ('jobs', 'slots', 'min_free_gb', 'slot_max_gb')):
        raise ValueError('provide valid complete leaf host.conf with workers = []')
    if args.disk_size_gb <= limits['min_free_gb'] + 20:
        raise ValueError('disk must exceed the free-space floor plus OS/cache space')
    revision, archive = runtime()
    gcloud = ['gcloud', '--quiet', '--project=' + project]
    if json.loads(subprocess.check_output(gcloud + ['compute', 'instances', 'list', '--filter=labels.ci-buddy-worker:*', '--format=json'])):
        raise ValueError('this project already has a CI VM; inspect it first')
    state = key_file.parent / ('ci-buddy-' + project)
    state.mkdir(mode=0o700)  # Retained single-project claim prevents ambiguous repeats.
    key = state / 'ssh'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    public = [key.with_suffix('.pub').read_text().strip()] + [p.read_text().strip() for p in args.pubkey_file]
    if any(not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?', p) for p in public):
        raise ValueError('provide ed25519 public SSH key files')
    config = dict(account='ci', root=args.root, hostname=name, host_conf=args.host_conf.read_text(), public_keys=public)
    startup = state / 'startup.sh'
    startup.write_text(Path(__file__).with_name('startup.sh').read_text().replace('@CONFIG@', base64.b64encode(json.dumps(config).encode()).decode()))
    try:
        subprocess.run(gcloud + ['compute', 'instances', 'create', name, '--zone=' + zone,
            '--machine-type=' + args.machine_type, '--subnet=' + args.subnet,
            '--image-project=ubuntu-os-cloud', '--image-family=ubuntu-2604-lts-amd64',
            '--boot-disk-size=' + str(args.disk_size_gb), '--no-address', '--no-service-account', '--no-scopes',
            '--labels=ci-buddy-worker=true', '--metadata=serial-port-enable=true,enable-oslogin=false',
            '--metadata-from-file=startup-script=' + str(startup)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready = exchange(gcloud + ['compute', 'connect-to-serial-port', name, '--zone=' + zone,
            '--port=2', '--ssh-key-file=' + str(key), '--extra-args=max-connections=1,replay-lines=50'], key_file)
        known = state / 'known_hosts'
        known.write_text(ready['ip'] + ' ' + ready['hostkey'] + '\n')
        block = ('Host ' + name + '\n HostName ' + ready['ip'] + '\n User ci\n IdentitiesOnly yes\n'
                 ' IdentityFile ' + json.dumps(str(key)) + '\n UserKnownHostsFile ' + json.dumps(str(known)) + '\n')
        (state / 'ssh_config').write_text(block)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ssh import OPTIONS
        program = '''import io,pathlib,subprocess,sys,tarfile
root = pathlib.Path(sys.argv[1]); stage = root / 'install' / sys.argv[2]; stage.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:archive.extractall(stage,filter='data')
subprocess.run(['bash',str(stage/'install.sh'),'--root',str(root)],check=True)
'''
        subprocess.run(['ssh', '-F', str(state / 'ssh_config'), *OPTIONS, name,
            shlex.join(['python3', '-c', program, args.root, revision])], input=archive, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(gcloud + ['compute', 'instances', 'remove-metadata', name, '--zone=' + zone,
            '--keys=serial-port-enable,startup-script'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('Worker ready: ' + name + ':' + args.root)
        print('Private SSH configuration: ' + str(state / 'ssh_config'))
        print('After network verification, add this endpoint and SSH configuration to the coordinator.')
    except BaseException:
        print('Instance ' + args.vm + ' may exist and incur charges. Inspect it; no VM was deleted or retried.', file=sys.stderr)
        raise


def main():
    if os.name != 'posix':
        raise ValueError('run owner setup from Linux/WSL; ordinary job clients support Windows')
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vm', help='project/zone/instance; this explicitly creates paid capacity')
    for name in ('root', 'machine-type', 'subnet'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--auth-key-file', type=Path, required=True)
    parser.add_argument('--host-conf', type=Path, required=True)
    parser.add_argument('--disk-size-gb', type=int, required=True)
    parser.add_argument('--pubkey-file', type=Path, action='append', default=[])
    create(parser.parse_args())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit('worker setup failed: ' + str(error)) from None
