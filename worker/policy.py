"""Owner-side policy proof and one-use enrollment keys; never worker credentials."""
import base64
import ipaddress
import json
import os
import secrets
import stat
import urllib.error
import urllib.parse
import urllib.request


WORKER = 'tag:ci-worker'
LIMIT = 1024 * 1024


def peer_addresses(peers):
    if not isinstance(peers, (list, tuple)) or not peers:
        raise ValueError('provide the explicit approved submitter IP addresses')
    return list(dict.fromkeys(str(ipaddress.ip_address(peer)) for peer in peers))


def expected_policy(owner, peers):
    if not isinstance(owner, str) or not owner or ':' in owner or any(c.isspace() for c in owner):
        raise ValueError('policy requires one explicit owner login')
    peers = peer_addresses(peers)
    destinations = ['[' + peer + ']' if ':' in peer else peer for peer in peers]
    return {
        'tagOwners': {WORKER: [owner]},
        'grants': [{'src': peers, 'dst': [WORKER], 'ip': ['tcp:22']}],
        'tests': [{'src': peer, 'proto': 'tcp', 'accept': [WORKER + ':22']} for peer in peers]
                 + [{'src': WORKER, 'proto': 'tcp',
                     'deny': [peer + ':' + str(port) for peer in destinations for port in (22, 80, 443)]}],
    }


def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate API or policy field')
        value[key] = item
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Policy:
    def __init__(self, token_file, tailnet, peers):
        self.peers = peer_addresses(peers)
        if not hasattr(os, 'getuid'):
            raise ValueError('owner token permission checks require POSIX or WSL')
        with os.fdopen(os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('API token must be an owned private regular file')
            token = source.read(4097)
        if len(token) > 4096:
            raise ValueError('API token exceeds size limit')
        self.token = token.decode('ascii').strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise ValueError('invalid API token format')
        self.authorization = ('Basic ' + base64.b64encode((self.token + ':').encode()).decode()
                              if self.token.startswith('tskey-api-') else 'Bearer ' + self.token)
        self.tailnet = '/tailnet/' + urllib.parse.quote(tailnet, safe='')
        self.opener = urllib.request.build_opener(NoRedirect)
        self.approved = None
        self.created = set()
        self.bound = {}
        self.existing = None
        self.key_requested = False
        self.description = 'ci-buddy worker enrollment ' + secrets.token_hex(8)

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request('https://api.tailscale.com/api/v2' + path, data=data, method=method,
            headers={'Authorization': self.authorization, 'Accept': 'application/json',
                     'Content-Type': 'application/json'})
        try:
            with self.opener.open(request) as response:
                raw = response.read(LIMIT + 1)
                if path.endswith('/acl/validate') and (response.status != 200 or raw):
                    raise ValueError('native Tailscale policy tests failed')
        except urllib.error.HTTPError as error:
            if method == 'DELETE' and error.code == 404:
                return None
            raise ValueError(f'Tailscale API request rejected: HTTP {error.code}') from None
        except (OSError, urllib.error.URLError):
            raise ValueError('Tailscale API request failed; enrollment was not registered') from None
        if len(raw) > LIMIT:
            raise ValueError('Tailscale API response exceeds size limit')
        if not raw:
            return None
        try:
            return json.loads(raw, object_pairs_hook=pairs)
        except (UnicodeError, ValueError, RecursionError):
            raise ValueError('invalid Tailscale API response') from None

    def verify(self, approved_policy):
        try:
            owner = approved_policy['tagOwners'][WORKER][0]
        except (KeyError, IndexError, TypeError):
            raise ValueError('missing approved owner policy') from None
        required = expected_policy(owner, self.peers)
        if any(approved_policy['tagOwners'].get(tag) != owners
               for tag, owners in required['tagOwners'].items()):
            raise ValueError('the CI worker tag must belong only to the explicit owner')
        native = approved_policy.get('tests', [])
        if not isinstance(native, list) or any(not isinstance(test, dict) for test in native):
            raise ValueError('approved native policy tests must be an array of objects')
        tests = list(required['tests'])
        for test in native:
            if test not in tests:
                tests.append(test)
        # The owner approves the full existing tailnet; unrelated policy stays intact.
        for checked in (False, True):
            if checked and self.request('POST', self.tailnet + '/acl/validate', tests) is not None:
                raise ValueError('native Tailscale policy tests failed')
            if self.request('GET', self.tailnet + '/acl') != approved_policy:
                raise ValueError('effective Tailscale policy differs from approved policy')
        self.approved = approved_policy

    def new_key(self):
        if self.approved is None or self.key_requested:
            raise ValueError('verify policy first; an enrollment key request is never retried')
        self.verify(self.approved)
        self.existing = {item[key] for item in self.devices() for key in ('id', 'nodeId')}
        self.key_requested = True
        capability = {'devices': {'create': {'reusable': False, 'ephemeral': True,
                                           'preauthorized': True, 'tags': [WORKER]}}}
        value = self.request('POST', self.tailnet + '/keys',
                             {'capabilities': capability, 'expirySeconds': 86400, 'description': self.description})
        if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
            raise ValueError('missing enrollment key identity')
        self.created.add(value['id'])
        if (json.dumps(value.get('capabilities'), sort_keys=True) != json.dumps(capability, sort_keys=True)
                or not isinstance(value.get('key'), str)
                or not value['key'].startswith('tskey-auth-')):
            raise ValueError('unexpected enrollment key capabilities')
        return value['id'], value['key']

    def revoke(self, key_id):
        if key_id not in self.created:
            raise ValueError('refusing to revoke a key not generated by this enrollment')
        self.request('DELETE', self.tailnet + '/keys/' + urllib.parse.quote(key_id, safe=''))
        self.created.remove(key_id)

    def abort(self):
        for device_id in set(self.bound.values()):
            try:
                self.remove_device(device_id)
            except (OSError, ValueError):
                pass
        for key_id in tuple(self.created):
            try:
                self.revoke(key_id)
            except (OSError, ValueError):
                pass
        return sorted({'device:' + value for value in self.bound.values()} | {'key:' + value for value in self.created})

    def remove_device(self, device_id):
        if device_id not in self.bound:
            raise ValueError('refusing to remove a device not bound by this enrollment')
        canonical = self.bound[device_id]
        self.request('DELETE', '/device/' + urllib.parse.quote(canonical, safe=''))
        self.bound = {alias: value for alias, value in self.bound.items() if value != canonical}

    def devices(self):
        value = self.request('GET', self.tailnet + '/devices?fields=all')
        if (not isinstance(value, dict) or not isinstance(value.get('devices'), list)
                or any(not isinstance(item, dict) or not isinstance(item.get('addresses'), list)
                       for item in value['devices'])):
            raise ValueError('invalid native device listing')
        if any(not isinstance(item.get(key), str) or not item[key]
               for item in value['devices'] for key in ('id', 'nodeId')):
            raise ValueError('missing native device identity')
        return value['devices']

    def device(self, ip, expected_hostname=None):
        if self.existing is None:
            raise ValueError('snapshot devices before creating an enrollment key')
        ip = str(ipaddress.ip_address(ip))
        devices = [item for item in self.devices() if ip in item['addresses']]
        if len(devices) != 1:
            raise ValueError('worker address does not identify exactly one device')
        device = devices[0]
        if any(device[key] in self.existing for key in ('id', 'nodeId')):
            raise ValueError('worker address belongs to a preexisting device')
        if expected_hostname is not None and device.get('hostname') != expected_hostname:
            raise ValueError('worker hostname differs from the expected provider identity')
        canonical = device['nodeId']
        for key in ('id', 'nodeId'):
            if isinstance(device.get(key), str) and device[key]:
                self.bound[device[key]] = canonical
        if (device.get('tags') != [WORKER] or device.get('authorized') is not True
                or device.get('isExternal') is not False):
            raise ValueError('worker address does not identify exactly one approved worker-tag device')
        routes = self.request('GET', '/device/' + urllib.parse.quote(canonical, safe='') + '/routes')
        if routes != {'advertisedRoutes': [], 'enabledRoutes': []}:
            raise ValueError('worker routes must be explicitly absent in the native API')
        return device
