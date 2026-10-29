"""Prepare an existing tailworker on its installed coordinator, using the shared kit."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

import auth
import data
from control import lane


def enroll(root, args):
    os.umask(0o077)
    os.chdir(root)
    if not shutil.which('tailworker'):
        os.environ['PATH'] = str(Path.home() / '.local/bin') + os.pathsep + os.environ['PATH']
    worker_root = PurePosixPath(args.root)
    if not worker_root.is_absolute() or str(worker_root) != args.root or '..' in worker_root.parts or any(c in args.root for c in '\r\n%'):
        raise ValueError('provide a normalized absolute worker root without systemd specifiers')
    conf = sys.stdin.read(32769) if args.host_conf == '-' else Path(args.host_conf).read_text()
    if len(conf.encode()) > 32768:
        raise ValueError('public host.conf exceeds size limit')
    info = json.loads(subprocess.check_output(['bash', str(root / 'bin/plane.sh'), str(root), 'info']))
    machine = json.loads(subprocess.check_output(['tailworker', 'resolve', args.name]))
    if machine['kind'] not in ('owned', 'dedicated') or machine['online'] is not True or machine['name'] != args.name:
        raise ValueError('choose an existing online owned worker')
    kit = Path(info['kit'])
    revision = data.read_file(root / 'bin/revision').strip()
    node = args.name + '-' + hashlib.sha256(args.root.encode()).hexdigest()[:12]
    name = 'ci-' + hashlib.sha256((str(root) + '\0' + args.root).encode()).hexdigest()[:12]
    remote = ['bash', '-c', 'source "$1/common.sh"; shift; remote "$@"', '_', str(kit), machine['host']]
    python = subprocess.check_output(remote + ['bash', '-c', 'command -v python3'], text=True).strip()
    if not PurePosixPath(python).is_absolute() or any(c in python for c in '\r\n'):
        raise ValueError('worker did not resolve an absolute Python executable')
    with tempfile.TemporaryDirectory(dir=root / 'dispatch') as directory:
        stage = Path(directory)
        (stage / 'host.conf').write_text(conf)
        limits = data.host_config(stage)
        if limits['fetch_hosts']:
            raise ValueError('a leaf host.conf requires fetch_hosts = []')
        runtime = root / 'bin/runtime.tar'
        (stage / 'revision').write_text(revision)
        with tempfile.TemporaryFile() as payload:
            with tarfile.open(runtime) as original, tarfile.open(fileobj=payload, mode='w') as archive:
                for entry in original:
                    if not entry.isdir() and PurePosixPath(entry.name).as_posix() not in {'host.conf', 'revision'}:
                        archive.addfile(entry, original.extractfile(entry) if entry.isfile() else None)
                for filename in ('host.conf', 'revision'):
                    archive.add(stage / filename, arcname=filename, recursive=False)
            payload.seek(0)
            subprocess.run(remote + ['bash', '-c', data.read_file(root / 'bin/install.sh'), '_',
                                     '--root', args.root, '--archive'], stdin=payload, check=True)
        profiles = []
        for exclusive in (False, True):
            identifier = name + ('-x' if exclusive else '-b')
            lanes = lane(revision, exclusive)
            public = dict(NAME=identifier, DISPATCHER_ADDR=info['dispatcher'], CAS_URL=info['cas'], LANES=lanes,
                          RUNNER_NODE=node, WORK_ROOT=str(worker_root / 'dispatch/work'),
                          COUNT='1' if exclusive else str(limits['jobs']), NO_DEADLINE='1',
                          COMMAND=shlex.join([python, str(worker_root / 'bin/execute.py'), args.root]))
            environment = stage / (identifier + '.env')
            environment.write_text(''.join(key + '=' + shlex.quote(value) + '\n' for key, value in public.items()))
            subprocess.run(['bash', str(kit / 'install.sh'), '--env', str(environment),
                            '--machine', args.name, '--runner', info['runner']], check=True)
            profiles.append((identifier, lanes))
        endpoint = machine['host'] + ':' + args.root
        with data.open_file(root / 'dispatch/enroll.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            token = auth.read(root)
            if token:
                auth.rpc(endpoint, 'store', token)
            config = data.host_config(root)
            if endpoint not in config['fetch_hosts']:
                config['fetch_hosts'].append(endpoint)
                data.write_file(root / 'host.conf', ''.join(key + ' = ' + json.dumps(value) + '\n' for key, value in config.items()))
        for identifier, lanes in profiles:
            signer = info['signer'] + ' --lanes ' + shlex.quote(lanes)
            subprocess.run(['bash', str(kit / 'enroll.sh'), '--name', identifier, '--machine', args.name,
                            '--signer', signer, '--note', identifier + ' ' + args.name], check=True)
    instance = subprocess.check_output(['systemd-escape', '--path', str(root)], text=True).strip()
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'ci-buddy-collect@' + instance + '.service'], check=True)
    print('enrolled=' + args.name + ' node=' + node)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('coordinator_root', type=Path)
    parser.add_argument('name')
    parser.add_argument('--root', required=True)
    parser.add_argument('--host-conf', required=True)
    try:
        arguments = parser.parse_args()
        enroll(arguments.coordinator_root.resolve(), arguments)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        detail = 'shared command failed' if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise SystemExit('ci-buddy setup incomplete; inspect existing units and credentials before repeating: ' + detail) from None
