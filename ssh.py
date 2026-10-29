"""Pinned private SSH argv belongs to the installed tailworker transport."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def endpoint(value):
    host, separator, root = value.partition(':')
    root = root if separator else '.ci-buddy'
    if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host)
            or not (root == '.ci-buddy' or root.startswith('/')) or any(c in root for c in '\r\n')):
        raise ValueError('invalid approved runner endpoint')
    if host == 'local' and root == '.ci-buddy':
        root = str(Path.home() / root)
    return host, root


def command(destination, remote):
    arguments = ['bash', '-c', 'exec tailworker ssh-argv "$@"', '_', destination]
    configuration = Path(__file__).resolve().parent.parent / 'ssh_config'
    if configuration.exists() or configuration.is_symlink():
        arguments += ['--config', str(configuration)]
    argv = json.loads(subprocess.check_output(arguments, text=True))
    if not isinstance(argv, list) or not argv or any(not isinstance(part, str) for part in argv):
        raise ValueError('invalid private SSH argv')
    return argv + [remote]


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('usage: ssh.py <destination> <remote-command|--argv>')
        remote = sys.argv[2]
        if remote == '--argv':
            payload = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
            if len(payload) > 16 * 1024 * 1024 or not payload.endswith(b'\0'):
                raise ValueError('invalid remote argv payload')
            remote = shlex.join(payload[:-1].decode().split('\0'))
        argv = command(sys.argv[1], remote)
        if os.name == 'nt':
            sys.exit(subprocess.call(argv))
        os.execvp(argv[0], argv)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f'ci-buddy private SSH: {error}', file=sys.stderr)
        sys.exit(255)
