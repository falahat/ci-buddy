"""Install the reviewed runner on an existing tailworker machine, then route work."""
import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib

import auth
import ssh


def runtime(source):
    if (source / 'revision').is_file():
        revision = (source / 'revision').read_text().strip()
        archive = (source / 'runtime.tar').read_bytes()
    else:
        subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--'], check=True)
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip()
        archive = subprocess.check_output(['git', '-C', str(source), 'archive', '--format=tar', 'HEAD'])
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('invalid reviewed runner revision')
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        if any(not member.isfile() and not member.isdir() for member in contents):
            raise ValueError('reviewed runner archive contains a link or special file')
    return revision, archive


def invoke(endpoint, request, archive):
    host, root = ssh.endpoint(endpoint)
    program = Path(__file__).with_name('enroll_remote.py').read_text()
    command = ['python3', '-I', '-c', program, root]
    argv = [sys.executable, '-I', '-c', program, root] if host == 'local' else ssh.command(host, shlex.join(command))
    header = json.dumps(request).encode()
    with tempfile.TemporaryFile() as payload:
        payload.write(len(header).to_bytes(8, 'big') + header + archive)
        payload.seek(0)
        return json.loads(ssh.capture(argv, 4096, payload=payload))


def enroll(args):
    conf = Path(args.host_conf).read_text()
    if len(conf.encode()) > 32768 or tomllib.loads(conf)['workers'] != []:
        raise ValueError('provide an explicit leaf host.conf with workers = []')
    root = PurePosixPath(args.root)
    if not root.is_absolute() or str(root) != args.root or '..' in root.parts or any(c in args.root for c in '\r\n'):
        raise ValueError('provide a normalized absolute runner root')
    source = Path(__file__).resolve().parent
    os.chdir(source)
    # Absolute owner PATH entries only; Windows must not search submitted cwd.
    for entry in os.get_exec_path():
        candidate = Path(entry) / 'tailworker'
        if Path(entry).is_absolute() and candidate.is_file():
            machine = json.loads(ssh.capture([sys.executable, str(candidate.resolve()), 'resolve', args.name], 16384))
            break
    else:
        raise ValueError('install the reviewed tailworker command on the owner PATH')
    if machine['name'] != args.name or machine['kind'] not in ('owned', 'dedicated') or machine['online'] is not True:
        raise ValueError('tailworker machine is not an existing online Linux runner')
    host = machine['host']
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host) or (host == 'local' and os.name != 'posix'):
        raise ValueError('local runners need Linux/WSL; remote machines need their pinned SSH alias')
    configured = subprocess.check_output(['git', 'config', '--global', '--get-all', 'ci-buddy.host']).decode().splitlines()
    coordinator = args.coordinator or configured[0]
    if coordinator not in configured:
        raise ValueError('coordinator is outside the owner-approved routing pool')
    revision, archive = runtime(source)
    endpoint = host + ':' + str(root)
    identity = invoke(endpoint, dict(mode='install', revision=revision, conf=conf), archive)
    added = endpoint not in configured
    if added:
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', endpoint], check=True)
    try:
        auth.client([endpoint, *configured])
        invoke(coordinator, dict(mode='register', revision=revision, endpoint=endpoint, identity=identity), archive)
    except BaseException:
        if added:
            subprocess.run(['git', 'config', '--global', '--fixed-value', '--unset-all', 'ci-buddy.host', endpoint], check=True)
        raise
    print('enrolled=' + args.name + ' endpoint=' + endpoint + ' coordinator=' + coordinator)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='existing logical tailworker machine name')
    parser.add_argument('--root', required=True)
    parser.add_argument('--host-conf', required=True)
    parser.add_argument('--coordinator', help='existing endpoint in owner ci-buddy.host configuration')
    try:
        enroll(parser.parse_args())
    except (OSError, ValueError, KeyError, IndexError, subprocess.CalledProcessError) as error:
        detail = 'native command failed' if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise SystemExit('ci-buddy enrollment failed; inspect the dedicated root and enrollment.log: ' + detail) from None
