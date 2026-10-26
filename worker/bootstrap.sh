#!/usr/bin/env bash
# Root first boot on a fresh dedicated Ubuntu image. Never run this on a shared host.
set +x
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { echo 'worker bootstrap requires image first-boot root' >&2; exit 2; }
script_dir=$(cd "$(dirname "$0")" && pwd -P)
if (($# == 0)); then
    # These metadata values are public policy, never auth/deploy keys or tokens.
    exec python3 - "$0" <<'PY'
from pathlib import Path
import shutil, subprocess, sys, tempfile, urllib.request
def failed(kind, error, traceback):
    print('ci-buddy-bootstrap-failed', flush=True)
    sys.__excepthook__(kind, error, traceback)
sys.excepthook = failed
base = 'http://169.254.169.254/computeMetadata/v1/instance/attributes/ci-buddy-'
def public(name):
    request = urllib.request.Request(base + name, headers={'Metadata-Flavor': 'Google'})
    with urllib.request.urlopen(request) as response:
        value = response.read(65537)
    if len(value) > 65536:
        raise ValueError('oversized bootstrap metadata')
    return value.decode('utf-8')
with tempfile.TemporaryDirectory(prefix='ci-buddy-bootstrap-', dir='/run') as directory:
    folder = Path(directory)
    script = folder / 'bootstrap.sh'
    shutil.copyfile(sys.argv[1], script)
    (folder / 'firewall.nft.in').write_text(public('firewall-template'))
    arguments = []
    for name in ('account', 'root', 'owner-addresses', 'coordinator-addresses', 'dns-addresses'):
        arguments += ['--' + name, public(name)]
    for name, option in (('host-conf', 'host-conf'), ('owner-key', 'owner-key-file'), ('coordinator-key', 'coordinator-key-file')):
        path = folder / name
        path.write_text(public(name))
        arguments += ['--' + option, str(path)]
    management = public('iap-management').strip()
    if management not in ('true', 'false'):
        raise ValueError('iap-management must be true or false')
    if management == 'true':
        arguments += ['--iap-management']
    subprocess.run(['bash', str(script), *arguments], check=True)
    request = urllib.request.Request('http://169.254.169.254/computeMetadata/v1/instance/guest-attributes/ci-buddy/bootstrap',
        data=b'ready', method='PUT', headers={'Metadata-Flavor': 'Google'})
    with urllib.request.urlopen(request) as response:
        response.read()
    print('ci-buddy-bootstrap-ready', flush=True)
PY
fi
iap_management=false
while (($#)); do
    case $1 in
        --iap-management) iap_management=true; shift ;;
        --account|--root|--host-conf|--owner-key-file|--coordinator-key-file|--owner-addresses|--coordinator-addresses|--dns-addresses)
            (($# >= 2)) || exit 2
            name=${1#--}; name=${name//-/_}; printf -v "$name" '%s' "$2"; shift 2 ;;
        *) echo 'unknown worker bootstrap option' >&2; exit 2 ;;
    esac
done
for name in account root host_conf owner_key_file coordinator_key_file owner_addresses coordinator_addresses dns_addresses; do
    [[ -n ${!name:-} ]] || { echo "missing bootstrap option: $name" >&2; exit 2; }
done
. /etc/os-release
[[ $ID == ubuntu && $VERSION_CODENAME == resolute ]] || { echo 'reviewed worker bootstrap requires Ubuntu 26.04' >&2; exit 2; }
fingerprint=$(python3 - "$0" "$script_dir/firewall.nft.in" "$account" "$root" "$host_conf" "$owner_key_file" "$coordinator_key_file" "$owner_addresses" "$coordinator_addresses" "$dns_addresses" "$iap_management" <<'PY'
import hashlib, sys
from pathlib import Path
values = sys.argv[1:]
content = [Path(values[index]).read_bytes() for index in (0, 1, 4, 5, 6)]
content += [values[index].encode() for index in (2, 3, 7, 8, 9, 10)]
print(hashlib.sha256(b'\0'.join(content)).hexdigest())
PY
)
stamp=/etc/ci-buddy-worker.bootstrap
if [[ -e $stamp || -L $stamp ]]; then
    [[ -f $stamp && ! -L $stamp && $(stat -c %u "$stamp") == 0 && $(<"$stamp") == "$fingerprint" ]] || {
        echo 'existing bootstrap differs from the approved first boot' >&2; exit 2;
    }
    id "$account" >/dev/null
    [[ -d $root && -f /etc/ssh/ci-buddy-worker.conf && -f /etc/ci-buddy-worker.nft ]]
    systemctl is-active --quiet ci-buddy-worker-firewall.service tailscaled.service ssh.service
    exit 0
fi
python3 - "$account" "$root" "$host_conf" "$owner_key_file" "$coordinator_key_file" "$owner_addresses" "$coordinator_addresses" "$dns_addresses" <<'PY'
import ipaddress, os, pwd, re, stat, subprocess, sys, tomllib
from pathlib import Path
account, root, conf, owner, coordinator, *lists = sys.argv[1:]
if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', account):
    raise ValueError('invalid worker account')
try:
    pwd.getpwnam(account)
except KeyError:
    pass
else:
    raise ValueError('worker account already exists; use a fresh dedicated image')
path = Path(root)
if not path.is_absolute() or len(path.parts) < 3 or path.exists() or path.is_symlink():
    raise ValueError('worker root must be a new absolute directory')
for filename in (conf, owner, coordinator):
    info = Path(filename).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise ValueError('bootstrap input must be a root-owned regular file')
limits = tomllib.loads(Path(conf).read_text())
if limits.get('workers') != []:
    raise ValueError('a new leaf worker must have workers = []')
networks = [ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7')]
for index, value in enumerate(lists):
    addresses = [ipaddress.ip_address(item.strip()) for item in value.split(',')]
    if index < 2 and any(not any(address in network for network in networks) for address in addresses):
        raise ValueError('submitter addresses must be private addresses')
for filename in (owner, coordinator):
    fields = Path(filename).read_text().strip().split()
    if len(Path(filename).read_text().strip().splitlines()) != 1 or len(fields) < 2 or fields[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'sk-ssh-ed25519@openssh.com'):
        raise ValueError('bootstrap accepts public SSH keys only')
    subprocess.run(['ssh-keygen', '-l', '-f', filename], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
allowed = {'apparmor', 'apport', 'atd', 'chrony', 'cloud-config', 'cloud-final', 'cloud-init', 'cron', 'dbus', 'irqbalance', 'multipathd', 'rng-tools-debian', 'udisks2', 'networkd-dispatcher', 'polkit', 'rsyslog', 'snapd', 'ssh', 'unattended-upgrades', 'ci-buddy-worker-bootstrap'}
units = subprocess.check_output(['systemctl', 'list-units', '--type=service', '--state=running', '--plain', '--no-legend', '--no-pager'], text=True)
for line in units.splitlines():
    unit = line.split()[0].removesuffix('.service')
    if unit not in allowed and not unit.startswith(('systemd-', 'google-', 'getty@', 'serial-getty@', 'user@')):
        raise ValueError('unrelated active service: ' + unit)
if any(1000 <= int(uid) < 65534 for uid in subprocess.check_output(['ps', '-eo', 'uid='], text=True).split()):
    raise ValueError('active non-system user; use a fresh dedicated image')
if Path('/etc/ssh/ci-buddy-worker.conf').exists() or Path('/var/lib/tailscale/tailscaled.state').exists():
    raise ValueError('worker bootstrap or tailnet state already exists')
PY
if command -v nft >/dev/null; then
    [[ -z $(nft list tables) ]] || { echo 'existing firewall policy; use a fresh dedicated image' >&2; exit 2; }
fi
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl nftables openssh-server podman uidmap passt slirp4netns fuse-overlayfs git python3 zstd dbus-user-session
key=/usr/share/keyrings/ci-buddy-tailscale.gpg
curl --proto '=https' --tlsv1.2 -fsSL https://pkgs.tailscale.com/stable/ubuntu/resolute.noarmor.gpg -o "$key"
printf '%s  %s\n' 3e03dacf222698c60b8e2f990b809ca1b3e104de127767864284e6c228f1fb39 "$key" | sha256sum -c -
chmod 644 "$key"
printf 'deb [signed-by=%s] https://pkgs.tailscale.com/stable/ubuntu resolute main\n' "$key" >/etc/apt/sources.list.d/ci-buddy-tailscale.list
apt-get update
apt-get install -y --no-install-recommends tailscale
useradd --create-home --user-group --shell /bin/bash "$account"
install -d -m 700 -o "$account" -g "$account" "$root" "$root/secrets"
install -m 600 -o "$account" -g "$account" "$host_conf" "$root/host.conf"
loginctl enable-linger "$account"
python3 - "$account" "$script_dir/firewall.nft.in" "$owner_key_file" "$coordinator_key_file" "$owner_addresses" "$coordinator_addresses" "$dns_addresses" "$iap_management" <<'PY'
import ipaddress, pwd, sys
from pathlib import Path
account, template, owner_key, coordinator_key, owner_csv, coordinator_csv, dns_csv, iap = sys.argv[1:]
owner = [str(ipaddress.ip_address(value.strip())) for value in owner_csv.split(',')]
coordinator = [str(ipaddress.ip_address(value.strip())) for value in coordinator_csv.split(',')]
approved = set(owner + coordinator)
dns = {str(ipaddress.ip_address(value.strip())) for value in dns_csv.split(',')} | {'127.0.0.53', '127.0.0.54', '::1'}
policy = Path(template).read_text()
for name, values in (('APPROVED', approved), ('DNS', dns)):
    for version in (4, 6):
        policy = policy.replace('@' + name + str(version) + '@', ', '.join(sorted(value for value in values if ipaddress.ip_address(value).version == version)))
policy = policy.replace('@RUNNER_UID@', str(pwd.getpwnam(account).pw_uid)).replace('@IAP_RULE@', 'ip saddr 35.235.240.0/20 tcp dport 22 accept' if iap == 'true' else '# IAP disabled')
Path('/etc/ci-buddy-worker.nft').write_text(policy)
keys = []
for filename, addresses in ((owner_key, owner + (['35.235.240.0/20'] if iap == 'true' else [])), (coordinator_key, coordinator)):
    key = ' '.join(Path(filename).read_text().split()[:2])
    keys.append('restrict,from="' + ','.join(addresses) + '" ' + key)
Path('/etc/ssh/ci-buddy-authorized-keys').write_text('\n'.join(keys) + '\n')
Path('/etc/ssh/ci-buddy-worker.conf').write_text(f"""Port 22
ListenAddress 0.0.0.0
ListenAddress ::
AllowUsers {account}
AuthorizedKeysFile /etc/ssh/ci-buddy-authorized-keys
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
DisableForwarding yes
X11Forwarding no
PermitTTY no
PermitUserRC no
PermitUserEnvironment no
UsePAM yes
""")
unit = Path('/etc/systemd/system/ci-buddy-worker-firewall.service')
unit.write_text('[Unit]\nDescription=Private worker firewall\nDefaultDependencies=no\nBefore=network-pre.target ssh.service tailscaled.service\nWants=network-pre.target\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/sbin/nft -f /etc/ci-buddy-worker.nft\n[Install]\nWantedBy=multi-user.target\n')
dropin = Path('/etc/systemd/system/ssh.service.d')
dropin.mkdir(exist_ok=True)
(dropin / 'ci-buddy-worker.conf').write_text('[Service]\nExecStart=\nExecStart=/usr/sbin/sshd -D -f /etc/ssh/ci-buddy-worker.conf\n')
PY
chmod 644 /etc/ssh/ci-buddy-authorized-keys /etc/ssh/ci-buddy-worker.conf /etc/ci-buddy-worker.nft
ssh-keygen -A
/usr/sbin/sshd -t -f /etc/ssh/ci-buddy-worker.conf
nft --check -f /etc/ci-buddy-worker.nft
systemctl daemon-reload
if systemctl list-unit-files ssh.socket --no-legend | grep -q '^ssh.socket'; then
    systemctl disable --now ssh.socket
fi
systemctl enable --now ci-buddy-worker-firewall.service tailscaled.service
systemctl enable ssh.service
systemctl restart ssh.service
tailscale set --operator="$account" --ssh=false --accept-routes=false --accept-dns=false
printf '%s\n' "$fingerprint" >"$stamp"
chmod 644 "$stamp"
printf 'worker image bootstrap ready; no tailnet auth key, project secret or worker registration installed\n'
