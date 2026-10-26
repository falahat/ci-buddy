#!/bin/bash
set -euo pipefail
set +x
exec python3 - <<'PY'
import base64, ipaddress, json, os, pathlib, pwd, re, subprocess, sys


def run(*args):
    return subprocess.check_output(args, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


port = None
join = None
try:
    assert os.getuid() == 0
    release = pathlib.Path('/etc/os-release').read_text()
    assert 'ID=ubuntu' in release and 'VERSION_CODENAME=resolute' in release
    config = json.loads(base64.b64decode('@CONFIG@', validate=True))
    account, root = config['account'], pathlib.Path(config['root'])
    assert re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', account)
    assert root.is_absolute() and root.resolve() == root and len(root.parts) >= 3 and not root.exists()
    assert re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', config['hostname'])
    assert isinstance(config['host_conf'], str) and config['public_keys']
    keys = []
    for key in config['public_keys']:
        fields = key.split()
        assert len(key.splitlines()) == 1 and len(fields) >= 2 and fields[0] == 'ssh-ed25519'
        base64.b64decode(fields[1], validate=True)
        keys.append('restrict,from="100.64.0.0/10,fd7a:115c:a1e0::/48" ' + ' '.join(fields[:2]))
    os.umask(0o077)
    run('systemctl', 'mask', '--now', 'serial-getty@ttyS1.service')
    run('stty', '-F', '/dev/ttyS1', '-echo', '-echonl', '-icanon', 'min', '1', 'time', '0')
    port = open('/dev/ttyS1', 'r+b', buffering=0)
    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'podman', 'uidmap', 'passt', 'git', 'python3', 'openssh-server', 'ca-certificates', 'curl', 'zstd')
    run('curl', '-fsSLo', '/usr/share/keyrings/tailscale-archive-keyring.gpg', 'https://pkgs.tailscale.com/stable/ubuntu/resolute.noarmor.gpg')
    pathlib.Path('/usr/share/keyrings/tailscale-archive-keyring.gpg').chmod(0o644)
    run('curl', '-fsSLo', '/etc/apt/sources.list.d/tailscale.list', 'https://pkgs.tailscale.com/stable/ubuntu/resolute.tailscale-keyring.list')
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'tailscale')
    run('useradd', '--create-home', '--user-group', '--shell', '/bin/bash', account)
    run('loginctl', 'enable-linger', account)
    user = pwd.getpwnam(account)
    run('install', '-d', '-m', '755', str(root.parent))
    for path in (root, root / 'secrets'):
        path.mkdir(mode=0o700)
        os.chown(path, user.pw_uid, user.pw_gid)
    conf = root / 'host.conf'
    conf.write_text(config['host_conf'])
    os.chown(conf, user.pw_uid, user.pw_gid)
    pathlib.Path('/etc/ssh/ci-buddy-authorized-keys').write_text('\n'.join(keys) + '\n')
    pathlib.Path('/etc/ssh/ci-buddy-authorized-keys').chmod(0o644)
    ssh = f"""Port 22
AllowUsers {account}
AuthorizedKeysFile /etc/ssh/ci-buddy-authorized-keys
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
DisableForwarding yes
PermitTTY no
PermitUserRC no
PermitUserEnvironment no
UsePAM yes
"""
    pathlib.Path('/etc/ssh/sshd_config').write_text(ssh)
    run('ssh-keygen', '-A')
    run('/usr/sbin/sshd', '-t')
    run('systemctl', 'disable', '--now', 'ssh.socket')
    run('systemctl', 'enable', '--now', 'ssh.service', 'tailscaled.service')
    run('systemctl', 'restart', 'ssh.service')
    port.write(b'CI_BUDDY_BOOTSTRAP_READY\n')
    challenge = port.readline(129)
    assert re.fullmatch(rb'CI_BUDDY_CHALLENGE [0-9a-f]{32}\n', challenge)
    port.write(b'CI_BUDDY_KEY_READY ' + challenge.split()[1] + b'\n')
    secret = port.readline(4098)
    assert len(secret) <= 4097 and re.fullmatch(rb'tskey-auth-[A-Za-z0-9_-]+\n', secret)
    join = root / 'secrets/join'
    with join.open('xb') as output:
        output.write(secret[:-1])
    try:
        run('tailscale', 'up', '--auth-key=file:' + str(join), '--accept-routes=false', '--accept-dns=false',
            '--ssh=false', '--advertise-tags=tag:ci-worker', '--advertise-routes=', '--advertise-exit-node=false', '--hostname=' + config['hostname'])
    finally:
        join.unlink(missing_ok=True)
    status = json.loads(run('tailscale', 'status', '--json'))
    assert status['Self']['Tags'] == ['tag:ci-worker']
    address = next(value for value in status['Self']['TailscaleIPs'] if ipaddress.ip_address(value).version == 4)
    assert ipaddress.ip_address(address) in ipaddress.ip_network('100.64.0.0/10')
    hostkey = ' '.join(pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[:2])
    port.write(b'CI_BUDDY_READY ' + json.dumps({'ip': address, 'hostkey': hostkey}).encode() + b'\n')
except BaseException:
    if port is None:
        port = open('/dev/ttyS1', 'r+b', buffering=0)
    port.write(b'CI_BUDDY_FAILED\n')
    sys.exit(1)
finally:
    if join is not None:
        join.unlink(missing_ok=True)
PY
