"""Owner-enrolled GitHub credentials; transport payloads never enter job data."""
import getpass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
import warnings
import ssh


def validate(token):
    if not re.fullmatch(rb'github_pat_[A-Za-z0-9_]{10,255}', token):
        raise ValueError('enter one fine-grained GitHub token')
    return token


def read(root):
    try:
        descriptor = os.open(root / 'github-token', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b''
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('GitHub token requires an owned private regular file')
        return validate(stream.read(512))


def store(root, token):
    validate(token)
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:  # creates mode 0600
        temporary = Path(stream.name)
        try:
            stream.write(token)
            stream.flush()
            temporary.replace(root / 'github-token')
        finally:
            temporary.unlink(missing_ok=True)


def rpc(endpoint, mode, token=b''):
    host, root = ssh.endpoint(endpoint)
    command = ['python3', str(PurePosixPath(root, 'bin', 'auth.py')), mode]
    argv = [sys.executable, str(Path(root) / 'bin/auth.py'), mode] if host == 'local' else ssh.command(host, shlex.join(command))
    return ssh.capture(argv, 512, payload=token)


def install(root, token):
    store(root, token)
    failed = False
    for endpoint in tomllib.loads((root / 'host.conf').read_text())['workers']:
        try:
            rpc(endpoint, 'store', token)
        except (OSError, ValueError, subprocess.CalledProcessError):
            print('credential unavailable: ' + endpoint, file=sys.stderr)
            failed = True
    if failed:
        raise ValueError('some workers are offline; dispatch will provision them when available')


def client(arguments):
    endpoints = subprocess.check_output(['git', 'config', '--global', '--get-all', 'ci-buddy.host']).decode().splitlines()
    if arguments:
        if set(arguments) - set(endpoints):
            raise ValueError('credential destination is outside the approved host pool')
        selected, *available = arguments
        expected = subprocess.run(['git', 'config', '--global', '--get', 'ci-buddy.token'],
                                  stdout=subprocess.PIPE, check=False).stdout.strip()
        if not expected:
            return
        for endpoint in dict.fromkeys([selected, *available]):
            try:
                token = rpc(endpoint, 'read')
            except (OSError, ValueError, subprocess.CalledProcessError):
                continue
            if token and hashlib.sha256(token).hexdigest().encode() == expected:
                if endpoint != selected:
                    rpc(selected, 'install', token)
                return
        raise ValueError('enrolled token unavailable on approved hosts; run ci-buddy auth')
    warnings.simplefilter('error', getpass.GetPassWarning)
    token = validate(getpass.getpass('GitHub token (selected repositories, Contents: read-only): ').encode())
    subprocess.run(['git', 'config', '--global', 'ci-buddy.token', hashlib.sha256(token).hexdigest()], check=True)
    failed = False
    for endpoint in dict.fromkeys(endpoints):
        try:
            rpc(endpoint, 'install', token)
            print('credentials installed: ' + endpoint)
        except (OSError, ValueError, subprocess.CalledProcessError):
            print('credential unavailable: ' + endpoint, file=sys.stderr)
            failed = True
    if failed:
        raise ValueError('incomplete distribution; available hosts can provision peers before submission')


if __name__ == '__main__':
    try:
        mode, *arguments = sys.argv[1:]
        root = Path(__file__).resolve().parent.parent
        if mode == 'read':
            sys.stdout.buffer.write(read(root))
        elif mode == 'check':
            read(root)
        elif mode in ('store', 'install'):
            token = validate(sys.stdin.buffer.read(512))
            (store if mode == 'store' else install)(root, token)
        elif mode == 'client':
            client(arguments)
        else:
            raise ValueError('unknown credential operation')
    except (OSError, ValueError, subprocess.CalledProcessError, getpass.GetPassWarning):
        sys.exit('ci-buddy credentials: operation failed; check approved hosts and private credential files')
