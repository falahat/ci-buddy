"""Create an owner-reviewed native GCP profile without contacting any service."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tomllib

if __package__:
    from .policy import expected_policy, pairs
else:
    from policy import expected_policy, pairs


def public_key(path):
    value = Path(path).read_text().strip()
    fields = value.split()
    if (len(value.splitlines()) != 1 or len(fields) < 2
            or fields[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'sk-ssh-ed25519@openssh.com')):
        raise ValueError('provide a public SSH key file')
    subprocess.run(['ssh-keygen', '-l', '-f', str(path)], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=True)
    return ' '.join(fields[:2])


def write_private(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
        output.write(value if isinstance(value, bytes) else value.encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'owner', 'tailnet', 'account', 'root', 'host-conf', 'image', 'image-id',
                 'machine-type', 'subnetwork', 'firewalls', 'owner-addresses', 'coordinator-addresses',
                 'dns-addresses', 'coordinator', 'coordinator-key-file', 'policy-file'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--disk-size-gb', type=int, required=True)
    parser.add_argument('--credentials', action='store_true', help='prompt privately for owner API credentials')
    args = parser.parse_args()
    if not hasattr(os, 'getuid'):
        raise ValueError('profile ownership and secret permissions require POSIX or WSL')
    peers = (args.owner_addresses + ',' + args.coordinator_addresses).split(',')
    fragment = expected_policy(args.owner, peers)
    policy_bytes = Path(args.policy_file).read_bytes()
    policy = json.loads(policy_bytes, object_pairs_hook=pairs)
    if any(policy.get('tagOwners', {}).get(tag) != owners for tag, owners in fragment['tagOwners'].items()):
        raise ValueError('existing approved policy must assign the CI worker tag only to this owner')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', args.account):
        raise ValueError('invalid worker account')
    root = PurePosixPath(args.root)
    if (not root.is_absolute() or len(root.parts) < 3 or str(root) != args.root
            or any(c.isspace() for c in args.root)):
        raise ValueError('worker root must be a normalized absolute path without whitespace')
    if not args.image_id.isdecimal() or int(args.image_id) <= 0:
        raise ValueError('provide the approved numeric source image ID')
    for value, kind in ((args.image, r'global/images'), (args.subnetwork, r'regions/[A-Za-z0-9_-]+/subnetworks')):
        if not re.fullmatch(r'https://www\.googleapis\.com/compute/v1/projects/[A-Za-z0-9_-]+/' + kind + r'/[A-Za-z0-9_-]+', value):
            raise ValueError('provide immutable native image/subnetwork self-links, not image families')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.machine_type):
        raise ValueError('provide a native machine type name')
    if not args.tailnet or any(c.isspace() for c in args.tailnet):
        raise ValueError('provide the explicit tailnet name')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*:/[^\s]+', args.coordinator):
        raise ValueError('provide coordinator destination:absolute-root')
    networks = [ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7')]
    for name in ('owner_addresses', 'coordinator_addresses', 'dns_addresses'):
        addresses = [ipaddress.ip_address(value.strip()) for value in getattr(args, name).split(',')]
        if name != 'dns_addresses' and any(not any(address in network for network in networks) for address in addresses):
            raise ValueError('approved submitter addresses must be private')
        setattr(args, name, ','.join(map(str, addresses)))
    conf = Path(args.host_conf).read_text()
    limits = tomllib.loads(conf)
    required = {'jobs', 'slots', 'memory', 'cpus', 'min_free_gb', 'slot_max_gb', 'workers'}
    if required - limits.keys() or limits.keys() - required - {'keep_days'} or limits['workers'] != []:
        raise ValueError('provide complete leaf host.conf with workers = []')
    for name in ('jobs', 'slots', 'min_free_gb', 'slot_max_gb', 'keep_days'):
        value = limits.get(name, 90) if name == 'keep_days' else limits[name]
        if type(value) is not int or value <= 0:
            raise ValueError('host.conf counts and disk limits must be positive integers')
    if not isinstance(limits['memory'], str) or not limits['memory'].strip() or not isinstance(limits['cpus'], str):
        raise ValueError('host.conf memory and cpus must be strings; only cpus may be empty')
    if args.disk_size_gb <= limits['min_free_gb']:
        raise ValueError('boot disk must exceed the worker free-space floor; allow additional OS/cache space')
    firewalls = Path(args.firewalls).read_bytes()
    json.loads(firewalls, object_pairs_hook=pairs)
    coordinator_key = public_key(args.coordinator_key_file)
    credentials = {}
    if args.credentials:
        if not os.isatty(0):
            raise ValueError('credential prompts require a private interactive terminal')
        for name, prompt in (('tailscale-token', 'Tailscale owner API credential: '),
                             ('github-token', 'GitHub owner API token (optional): ')):
            value = getpass.getpass(prompt).strip()
            if not value and name == 'github-token':
                continue
            if not value or len(value) > 4096 or any(c.isspace() for c in value):
                raise ValueError('invalid credential format')
            credentials[name] = value
    output = Path(args.output).expanduser().absolute()
    if subprocess.run(['git', '-C', str(output.parent), 'rev-parse', '--git-dir'],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError('profile secrets must live outside Git repositories')
    if output.exists() or output.is_symlink():
        info = output.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or any(output.iterdir()):
            raise ValueError('output must be absent or an empty owned directory')
    else:
        output.mkdir(mode=0o700)
    output.chmod(0o700)
    secrets = output / 'secrets'
    secrets.mkdir(mode=0o700)
    key = secrets / 'owner-ssh'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'ci-buddy-owner', '-f', str(key)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    key.chmod(0o600)
    key.with_suffix('.pub').chmod(0o600)
    source = Path(__file__).resolve().parent
    values = {'startup-script': (source / 'bootstrap.sh').read_text(), 'enable-guest-attributes': 'TRUE',
              'block-project-ssh-keys': 'TRUE', 'enable-oslogin': 'FALSE'}
    public = {'account': args.account, 'root': args.root, 'host-conf': conf, 'image-id': args.image_id,
              'owner-key': public_key(key.with_suffix('.pub')), 'coordinator-key': coordinator_key,
              'owner-addresses': args.owner_addresses, 'coordinator-addresses': args.coordinator_addresses,
              'dns-addresses': args.dns_addresses, 'iap-management': 'true', 'tailnet': args.tailnet,
              'coordinator': args.coordinator, 'firewall-template': (source / 'firewall.nft.in').read_text()}
    values.update({'ci-buddy-' + name: value for name, value in public.items()})
    properties = {'machineType': args.machine_type, 'canIpForward': False, 'serviceAccounts': [],
                  'networkInterfaces': [{'subnetwork': args.subnetwork, 'stackType': 'IPV4_ONLY', 'accessConfigs': []}],
                  'disks': [{'boot': True, 'autoDelete': True, 'type': 'PERSISTENT',
                             'initializeParams': {'sourceImage': args.image, 'diskSizeGb': str(args.disk_size_gb)}}],
                  'tags': {'items': ['ci-buddy-worker']}, 'labels': {'ci-buddy-enrollment': 'ready'},
                  'metadata': {'items': [{'key': name, 'value': value} for name, value in values.items()]}}
    write_private(output / 'instance-template.json', json.dumps({'properties': properties}, indent=2) + '\n')
    write_private(output / 'tailnet-policy.json', policy_bytes)
    write_private(output / 'ci-policy-fragment.json', json.dumps(fragment, indent=2) + '\n')
    write_private(output / 'firewalls.json', firewalls)
    for name, value in credentials.items():
        write_private(secrets / name, value + '\n')
    print('Created owner profile:', output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
