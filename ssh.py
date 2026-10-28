"""Private SSH transport. Never impose a deadline on a submitted command."""
import codecs
import io
import ipaddress
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import threading
import safe_output


OPTIONS = ['-q', '-T', '-x', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'PreferredAuthentications=publickey', '-o', 'IdentitiesOnly=yes',
           '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
           '-o', 'PermitLocalCommand=no', '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none',
           '-o', 'ControlPath=none', '-o', 'CanonicalizeHostname=no',
           '-o', 'ServerAliveInterval=0', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1']
NETWORKS = tuple(ipaddress.ip_network((address, bits)) for address, bits in (
    ('127.0.0.0', 8), ('10.0.0.0', 8), ('172.16.0.0', 12),
    ('192.168.0.0', 16), ('100.64.0.0', 10), ('::1', 128), ('fc00::', 7)))
ACTIVE = set()
STOP_INPUT = False


def endpoint(value):
    host, separator, root = value.partition(':')
    root = root if separator else '.ci-buddy'
    if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host)
            or not (root == '.ci-buddy' or root.startswith('/')) or any(c in root for c in '\r\n')):
        raise ValueError('invalid approved runner endpoint')
    if host == 'local' and root == '.ci-buddy':
        root = str(Path.home() / root)
    return host, root


def configuration():
    path = Path(__file__).resolve().parent.parent / 'ssh_config'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    # Native Windows mode bits cannot establish this POSIX ownership contract.
    uid = os.getuid() if hasattr(os, 'getuid') else None
    if uid is None or not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError('ssh_config must be an owned private regular file, without a symlink')
    return ['-F', str(path)]


def private_address(hostname, port):
    addresses = []
    for result in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        address = getattr(address, 'ipv4_mapped', None) or address
        if not any(address in network for network in NETWORKS):
            raise ValueError('destination resolves outside private address ranges')
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError('destination has no private address')
    # Prefer IPv4 when both exist, including local SSH bound only on IPv4.
    return str(next((address for address in addresses if address.version == 4), addresses[0]))


def command(destination, remote):
    if not destination or destination.startswith('-') or any(c.isspace() for c in destination):
        raise ValueError('invalid SSH destination')
    selected = configuration()
    effective = subprocess.check_output(['ssh', *selected, '-G', *OPTIONS, destination], text=True)
    config = {}
    identities = []
    for line in effective.splitlines():
        key, _, value = line.partition(' ')
        if key == 'identityfile':
            identities.append(value)
        else:
            config[key] = value
    hostname, port = config['hostname'], int(config['port'])
    if not 0 < port < 65536:
        raise ValueError('invalid SSH port')
    address = private_address(hostname, port)
    alias = config.get('hostkeyalias', hostname)
    if alias == 'none':
        alias = hostname
    # Keep Host matching and owner-configured identities while pinning DNS to
    # the checked address. HostKeyAlias retains the original known-host key.
    identities = [part for identity in identities
                  for part in ('-i', identity.replace('%h', hostname))]
    user = ['-l', config['user']] if 'user' in config else []
    return ['ssh', *selected, *OPTIONS, '-p', str(port), *user, '-o', 'HostName=' + address, '-o', 'HostKeyAlias=' + alias,
            *identities, destination, remote]


def receive(command, destination, limit, display=False, payload=None, keep_input=False):
    # Bound bytes, not execution time. No transport failure resubmits a job.
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    with subprocess.Popen(command, stdin=subprocess.PIPE if isinstance(payload, bytes) else payload,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        if isinstance(payload, bytes):
            process.input_lock = threading.Lock()
            process.input_finished = False
            process.close_input = STOP_INPUT
            process.keep_input = keep_input
        ACTIVE.add(process)
        if isinstance(payload, bytes):
            process.close_input = process.close_input or STOP_INPUT
            def feed():
                try:
                    process.stdin.write(payload)
                    process.stdin.flush()
                except (OSError, ValueError):
                    pass
                with process.input_lock:
                    process.input_finished = True
                    if not keep_input or process.close_input:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
            if keep_input:
                process.input_thread = threading.Thread(target=feed, daemon=True)
                process.input_thread.start()
            else:
                feed()
        try:
            total = 0
            while block := process.stdout.read1(min(65536, limit - total + 1)):
                total += len(block)
                if total > limit:
                    raise ValueError('worker output exceeds size limit')
                destination.write(block)
                if display:
                    print(safe_output.clean(decoder.decode(block)), end='', flush=True)
            if display:
                print(safe_output.clean(decoder.decode(b'', final=True)), end='', flush=True)
        except BaseException:
            process.terminate()
            raise
        finally:
            ACTIVE.discard(process)
            process.stdout.close()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        return process.wait()


def capture(command, limit=4096, payload=None):
    output = io.BytesIO()
    status = receive(command, output, limit, payload=payload)
    if status:
        raise subprocess.CalledProcessError(status, command)
    return output.getvalue()


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('usage: ssh.py <destination> <remote-command>')
        argv = command(*sys.argv[1:])
        if os.name == 'nt':
            sys.exit(subprocess.call(argv))
        os.execvp(argv[0], argv)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print(f'ci-buddy private SSH: {error}', file=sys.stderr)
        sys.exit(255)
