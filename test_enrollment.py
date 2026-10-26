"""Enrollment boundary checks using production policy code and mocked HTTP only."""
import base64
import io
import json
import os
import stat
import types
import unittest
from unittest import mock
import urllib.error

from worker import policy


PEERS = ['100.64.1.10', '100.64.1.11']


class Response(io.BytesIO):
    def __init__(self, value=b'', status=200):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status


class Token(io.BytesIO):
    def fileno(self):
        return 123


def client(token='tskey-api-fixture', mode=0o600, uid=42):
    info = types.SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid)
    with mock.patch.object(policy.os, 'getuid', return_value=42, create=True), \
            mock.patch.object(policy.os, 'O_NOFOLLOW', 0x20000, create=True), \
            mock.patch.object(policy.os, 'O_NONBLOCK', 0x800, create=True), \
            mock.patch.object(policy.os, 'open', return_value=123), \
            mock.patch.object(policy.os, 'fdopen', return_value=Token(token.encode())), \
            mock.patch.object(policy.os, 'fstat', return_value=info):
        value = policy.Policy('owned-token', 'example.test', PEERS)
    value.opener = mock.Mock()
    value.existing = set()
    return value


def device(tags=None):
    return {'id': 'legacy-node', 'nodeId': 'node-one', 'addresses': ['100.64.1.2'],
            'tags': [policy.WORKER] if tags is None else tags, 'authorized': True, 'isExternal': False}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = client()
        self.approved = policy.expected_policy('owner@example.test', PEERS)

    def test_approved_snapshot_and_native_tests_are_both_required(self):
        self.policy.opener.open.side_effect = [Response(self.approved), Response(), Response(self.approved)]
        self.policy.verify(self.approved)
        requests = [call.args[0] for call in self.policy.opener.open.call_args_list]
        self.assertEqual([request.method for request in requests], ['GET', 'POST', 'GET'])
        self.assertEqual(json.loads(requests[1].data), self.approved['tests'])
        self.assertEqual(self.approved['tagOwners'], {policy.WORKER: ['owner@example.test']})
        self.assertEqual(self.approved['grants'], [{'src': PEERS, 'dst': [policy.WORKER], 'ip': ['tcp:22']}])

    def test_peers_are_explicit_and_ipv6_test_destinations_are_bracketed(self):
        for peers in ([], None, '100.64.1.10', ['not-an-ip']):
            with self.subTest(peers=peers), self.assertRaises(ValueError):
                policy.expected_policy('owner@example.test', peers)
        fragment = policy.expected_policy('owner@example.test', ['fd00:0::1', 'fd00::1', PEERS[0]])
        self.assertEqual(fragment['grants'][0]['src'], ['fd00::1', PEERS[0]])
        self.assertEqual(fragment['tests'][0], {'src': 'fd00::1', 'proto': 'tcp',
                                               'accept': [policy.WORKER + ':22']})
        self.assertIn('[fd00::1]:443', fragment['tests'][-1]['deny'])

    def test_native_http_200_failure_is_not_success(self):
        for raw in (b'null', b'{}', b' ', b'{"message":"test(s) failed","data":[{"errors":["unexpected allow"]}]}'):
            with self.subTest(raw=raw):
                value = client()
                value.opener.open.side_effect = [Response(self.approved), Response(raw)]
                with self.assertRaisesRegex(ValueError, 'native Tailscale policy tests failed'):
                    value.verify(self.approved)

    def test_unrelated_approved_policy_is_preserved_and_tests_deduplicated(self):
        approved = json.loads(json.dumps(self.approved))
        approved['groups'] = {'group:app-owners': ['owner@example.test']}
        approved['tagOwners']['tag:app-server'] = ['group:app-owners']
        approved['grants'].append({'src': ['group:app-owners'], 'dst': ['tag:app-server'], 'ip': ['tcp:443']})
        approved['ssh'] = [{'src': ['group:app-owners'], 'dst': ['tag:app-server'],
                            'users': ['operator'], 'action': 'accept'}]
        app_test = {'src': 'owner@example.test', 'proto': 'tcp', 'accept': ['tag:app-server:443']}
        approved['tests'] = [app_test, app_test]
        before = json.dumps(approved, sort_keys=True)
        self.policy.opener.open.side_effect = [Response(approved), Response(), Response(approved)]
        self.policy.verify(approved)
        self.assertEqual(json.dumps(approved, sort_keys=True), before)
        requests = [call.args[0] for call in self.policy.opener.open.call_args_list]
        self.assertEqual([request.method for request in requests], ['GET', 'POST', 'GET'])
        self.assertTrue(requests[1].full_url.endswith('/acl/validate'))
        self.assertEqual(json.loads(requests[1].data), self.approved['tests'] + [app_test])
        self.assertEqual(self.policy.approved, approved)

    def test_full_policy_drift_is_rejected_before_and_after_native_tests(self):
        changed = json.loads(json.dumps(self.approved))
        changed['grants'].append({'src': ['*'], 'dst': ['*'], 'ip': ['*']})
        for responses in ([Response(changed)], [Response(self.approved), Response(), Response(changed)]):
            with self.subTest(calls=len(responses)):
                value = client()
                value.opener.open.side_effect = responses
                with self.assertRaisesRegex(ValueError, 'effective Tailscale policy differs'):
                    value.verify(self.approved)
                self.assertIsNone(value.approved)

    def test_worker_tag_ownership_cannot_be_weakened(self):
        for owners in ([], ['group:admins'], ['owner@example.test', 'other@example.test']):
            with self.subTest(owners=owners):
                approved = json.loads(json.dumps(self.approved))
                approved['tagOwners'][policy.WORKER] = owners
                with self.assertRaises(ValueError):
                    self.policy.verify(approved)
                self.policy.opener.open.assert_not_called()

    def test_duplicate_json_fields_are_rejected(self):
        self.policy.opener.open.return_value = Response(b'{"grants":[],"grants":[{}]}')
        with self.assertRaisesRegex(ValueError, 'invalid Tailscale API response'):
            self.policy.request('GET', '/tailnet/example.test/acl')

    def test_redirects_fail_without_exposing_response_or_token(self):
        self.assertIsNone(policy.NoRedirect().redirect_request(None, None, None, None, None, None))
        self.policy.opener.open.side_effect = urllib.error.HTTPError(
            'https://api.tailscale.com/', 302, 'redirect', {}, io.BytesIO(b'tskey-auth-do-not-print'))
        with self.assertRaises(ValueError) as caught:
            self.policy.request('GET', '/tailnet/example.test/acl')
        self.assertNotIn('tskey-', str(caught.exception))

    def test_private_owner_token_and_authentication_kinds(self):
        for mode, uid in ((0o644, 42), (0o600, 43)):
            with self.subTest(mode=mode, uid=uid), self.assertRaises(ValueError):
                client(mode=mode, uid=uid)
        expected = 'Basic ' + base64.b64encode(b'tskey-api-fixture:').decode()
        self.assertEqual(self.policy.authorization, expected)
        self.assertEqual(client('oauth-access-fixture').authorization, 'Bearer oauth-access-fixture')

    def test_wrong_tags_still_leave_bound_identity_for_rollback(self):
        self.policy.opener.open.side_effect = [
            Response({'devices': [device([policy.WORKER, 'tag:other-role'])]}), Response()]
        with self.assertRaises(ValueError):
            self.policy.device('100.64.1.2')
        self.assertEqual(self.policy.abort(), [])
        deleted = self.policy.opener.open.call_args.args[0]
        self.assertEqual((deleted.method, deleted.full_url.rsplit('/', 1)[-1]), ('DELETE', 'node-one'))

    def test_routes_must_explicitly_be_empty(self):
        self.policy.opener.open.side_effect = [Response({'devices': [device()]}), Response({})]
        with self.assertRaisesRegex(ValueError, 'routes must be explicitly absent'):
            self.policy.device('100.64.1.2')
        self.assertEqual(set(self.policy.bound), {'legacy-node', 'node-one'})

    def test_single_key_attempt_and_orphan_cleanup_order(self):
        self.policy.approved = self.approved
        self.policy.verify = mock.Mock()
        capability = {'devices': {'create': {'reusable': False, 'ephemeral': True,
                                           'preauthorized': True, 'tags': [policy.WORKER]}}}
        self.policy.opener.open.side_effect = [
            Response({'devices': []}),
            Response({'id': 'enrollment-key', 'key': 'tskey-auth-fixture', 'capabilities': capability}),
            Response({'devices': [device()]}), Response({'advertisedRoutes': [], 'enabledRoutes': []}),
            Response(), Response()]
        self.assertEqual(self.policy.new_key(), ('enrollment-key', 'tskey-auth-fixture'))
        submitted = self.policy.opener.open.call_args.args[0]
        self.assertEqual(json.loads(submitted.data)['capabilities'], capability)
        with self.assertRaisesRegex(ValueError, 'never retried'):
            self.policy.new_key()
        self.policy.device('100.64.1.2')
        self.assertEqual(self.policy.abort(), [])
        deletes = [call.args[0].full_url for call in self.policy.opener.open.call_args_list
                   if call.args[0].method == 'DELETE']
        self.assertTrue(deletes[0].endswith('/device/node-one'))
        self.assertTrue(deletes[1].endswith('/keys/enrollment-key'))
        with self.assertRaises(ValueError):
            self.policy.remove_device('unbound-node')

    def test_preexisting_node_cannot_be_bound_or_deleted(self):
        self.policy.approved = self.approved
        self.policy.verify = mock.Mock()
        existing = device()
        existing['hostname'] = 'ci-buddy-target'
        capability = {'devices': {'create': {'reusable': False, 'ephemeral': True,
                                           'preauthorized': True, 'tags': [policy.WORKER]}}}
        self.policy.opener.open.side_effect = [
            Response({'devices': [existing]}),
            Response({'id': 'enrollment-key', 'key': 'tskey-auth-fixture', 'capabilities': capability}),
            Response({'devices': [existing]}), Response()]
        self.policy.new_key()
        with self.assertRaisesRegex(ValueError, 'preexisting device'):
            self.policy.device('100.64.1.2', 'ci-buddy-target')
        self.assertEqual(self.policy.abort(), [])
        deleted = [call.args[0].full_url for call in self.policy.opener.open.call_args_list
                   if call.args[0].method == 'DELETE']
        self.assertEqual(deleted, ['https://api.tailscale.com/api/v2/tailnet/example.test/keys/enrollment-key'])

    def test_unrelated_new_hostname_cannot_be_bound_or_deleted(self):
        unrelated = device()
        unrelated['hostname'] = 'ci-buddy-other'
        self.policy.opener.open.return_value = Response({'devices': [unrelated]})
        with self.assertRaisesRegex(ValueError, 'hostname differs'):
            self.policy.device('100.64.1.2', 'ci-buddy-target')
        self.assertEqual(self.policy.abort(), [])
        self.assertEqual(self.policy.opener.open.call_count, 1)


class Admission(unittest.TestCase):
    def test_offline_profile_preserves_policy_and_exports_private_native_files(self):
        import tempfile
        from pathlib import Path
        from worker import profile
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output = folder / 'profile'
            conf = folder / 'host.conf'
            conf.write_text('jobs=1\nslots=1\nmemory="1g"\ncpus="1"\nmin_free_gb=50\nslot_max_gb=100\nworkers=[]\n')
            firewalls = folder / 'firewalls.json'
            firewalls.write_text('[]')
            approved = {'tagOwners': {'tag:ci-worker': ['owner@example.test']},
                        'groups': {'group:existing': ['owner@example.test']}}
            policy = folder / 'policy.json'
            policy.write_text(json.dumps(approved))
            arguments = ['profile', '--output', str(output), '--owner', 'owner@example.test',
                '--tailnet', 'example.test', '--account', 'runner', '--root', '/work/worker',
                '--host-conf', str(conf), '--image', 'https://www.googleapis.com/compute/v1/projects/example/global/images/pinned',
                '--image-id', '123', '--machine-type', 'example-size', '--disk-size-gb', '200',
                '--subnetwork', 'https://www.googleapis.com/compute/v1/projects/example/regions/region/subnetworks/private',
                '--firewalls', str(firewalls), '--policy-file', str(policy),
                '--owner-addresses', '100.64.0.1', '--coordinator-addresses', '100.64.0.2',
                '--dns-addresses', '1.1.1.1', '--coordinator', 'local:/work/coordinator',
                '--coordinator-key-file', str(folder / 'coordinator.pub')]
            def command(values, **kwargs):
                if values[0] == 'git':
                    return types.SimpleNamespace(returncode=1)
                self.assertEqual(values[0], 'ssh-keygen')
                key = Path(values[-1])
                key.write_text('fixture-only-private-material')
                key.with_suffix('.pub').write_text('ssh-ed25519 fixture-public')
                return types.SimpleNamespace(returncode=0)
            with mock.patch('sys.argv', arguments), mock.patch.object(profile.subprocess, 'run', side_effect=command), \
                 mock.patch.object(profile, 'public_key', return_value='ssh-ed25519 fixture-public'):
                profile.main()
            self.assertEqual((output / 'tailnet-policy.json').read_bytes(), policy.read_bytes())
            self.assertEqual(json.loads((output / 'ci-policy-fragment.json').read_text())['tagOwners'], approved['tagOwners'])
            template = json.loads((output / 'instance-template.json').read_text())['properties']
            self.assertEqual(template['serviceAccounts'], [])
            self.assertEqual(template['networkInterfaces'][0]['accessConfigs'], [])
            self.assertNotIn('fixture-only-private-material', json.dumps(template))
            for path in (output, output / 'secrets', output / 'secrets/owner-ssh', output / 'instance-template.json'):
                self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_valid_fresh_instance_and_each_rejected_capability(self):
        import copy
        import hashlib
        from worker import enroll
        script = 'reviewed bootstrap'
        digest = hashlib.sha256(script.encode()).hexdigest()
        values = {'enable-guest-attributes': 'TRUE', 'block-project-ssh-keys': 'TRUE',
                  'enable-oslogin': 'FALSE', 'startup-script': script}
        valid = dict(status='RUNNING', canIpForward=False, serviceAccounts=[],
            labels={'ci-buddy-enrollment': 'ready'}, networkInterfaces=[{'networkIP': '10.0.0.2'}],
            disks=[{'boot': True}], metadata={'items': [{'key': k, 'value': v} for k, v in values.items()]})
        enroll.validate_instance(valid, {'sourceImageId': '123'}, '123', digest)
        mutations = [lambda x: x.update(canIpForward=True),
            lambda x: x.update(serviceAccounts=[{'email': 'unexpected'}]),
            lambda x: x['networkInterfaces'][0].update(accessConfigs=[{'natIP': '8.8.8.8'}]),
            lambda x: x['networkInterfaces'][0].update(ipv6AccessConfigs=[{}]),
            lambda x: x['networkInterfaces'][0].update(externalIpv6='2001:4860::1'),
            lambda x: x['networkInterfaces'].append({'networkIP': '10.0.0.3'}),
            lambda x: x['disks'].append({'boot': False}),
            lambda x: x['labels'].update({'ci-buddy-enrollment': 'enrolled'}),
            lambda x: x['metadata']['items'].append({'key': 'startup-script-url', 'value': 'unapproved'})]
        for change in mutations:
            with self.subTest(change=change):
                candidate = copy.deepcopy(valid)
                change(candidate)
                with self.assertRaises(ValueError):
                    enroll.validate_instance(candidate, {'sourceImageId': '123'}, '123', digest)
        with self.assertRaises(ValueError):
            enroll.validate_instance(valid, {'sourceImageId': '456'}, '123', digest)
        with self.assertRaises(ValueError):
            enroll.validate_instance(valid, {'sourceImageId': '123'}, '123', 'wrong hash')

    def test_transmitted_programs_compile_including_nested_probe(self):
        import ast
        import inspect
        from worker import enroll
        programs = []
        for node in ast.walk(ast.parse(inspect.getsource(enroll))):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'program' for t in node.targets):
                programs.append(ast.literal_eval(node.value))
        self.assertGreaterEqual(len(programs), 4)
        for program in programs:
            syntax = ast.parse(program)
            for node in ast.walk(syntax):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'probe' for t in node.targets):
                    ast.parse(ast.literal_eval(node.value))

    def test_bootstrap_embedded_python_compiles(self):
        import ast
        import re
        from pathlib import Path
        source = (Path(__file__).parent / 'worker/bootstrap.sh').read_text()
        programs = re.findall(r"<<'PY'\n(.*?)\nPY", source, re.S)
        self.assertGreaterEqual(len(programs), 4)
        for program in programs:
            ast.parse(program)

    def test_failed_admission_never_registers_and_revokes(self):
        import tempfile
        from pathlib import Path
        from worker import enroll
        for failure in ('install', 'probe', 'final-policy'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                profile = Path(directory)
                (profile / 'tailnet-policy.json').write_text('{}')
                cloud = mock.Mock(profile=profile, secrets=profile, dependency_keys=[], registration_started=False,
                                  address='100.64.1.2', identifier='123', values={'ci-buddy-tailnet': 'example.test',
                                  'ci-buddy-owner-addresses': '100.64.0.1', 'ci-buddy-coordinator-addresses': '100.64.0.2'})
                cloud.inspect.return_value = {'networkInterfaces': [{'networkIP': '10.0.0.2'}]}
                client = mock.Mock()
                if failure == 'final-policy':
                    client.verify.side_effect = [None, ValueError('policy changed')]
                args = types.SimpleNamespace(profile=profile, vm='p/z/i', project='example', dependency=[])
                with mock.patch.object(enroll, 'Cloud', return_value=cloud), \
                     mock.patch.object(enroll, 'Policy', return_value=client), \
                     mock.patch.object(enroll, 'source_archive', return_value=('a' * 40, b'archive')), \
                     mock.patch.object(enroll, 'install_worker', side_effect=ValueError('install') if failure == 'install' else None,
                                       return_value='worker:/work') as install, \
                     mock.patch.object(enroll, 'dependencies'), \
                     mock.patch.object(enroll, 'verify_worker', side_effect=ValueError('probe') if failure == 'probe' else None), \
                     mock.patch.object(enroll, 'register_worker') as register:
                    client.new_key.return_value = ('key-id', 'fixture-not-a-real-key')
                    client.abort.return_value = []
                    with self.assertRaises(ValueError):
                        enroll.enroll(args)
                    register.assert_not_called()
                    client.abort.assert_called_once()
                    self.assertIn(mock.call('failed'), cloud.label.call_args_list)

    def test_success_registers_only_after_probe_and_final_policy(self):
        import tempfile
        from pathlib import Path
        from worker import enroll
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / 'tailnet-policy.json').write_text('{}')
            cloud = mock.Mock(profile=profile, secrets=profile, dependency_keys=[], registration_started=False, address='100.64.1.2',
                              identifier='123', values={'ci-buddy-tailnet': 'example.test',
                              'ci-buddy-owner-addresses': '100.64.0.1', 'ci-buddy-coordinator-addresses': '100.64.0.2'})
            cloud.inspect.return_value = {'networkInterfaces': [{'networkIP': '10.0.0.2'}]}
            client = mock.Mock()
            client.new_key.return_value = ('key-id', 'fixture-not-a-real-key')
            events = []
            client.verify.side_effect = lambda value: events.append('policy')
            args = types.SimpleNamespace(profile=profile, vm='p/z/i', project='example', dependency=[])
            with mock.patch.object(enroll, 'Cloud', return_value=cloud), \
                 mock.patch.object(enroll, 'Policy', return_value=client), \
                 mock.patch.object(enroll, 'source_archive', return_value=('a' * 40, b'archive')), \
                 mock.patch.object(enroll, 'install_worker', return_value='worker:/work'), \
                 mock.patch.object(enroll, 'dependencies'), \
                 mock.patch.object(enroll, 'verify_worker', side_effect=lambda *a: events.append('probe')), \
                 mock.patch.object(enroll, 'register_worker', side_effect=lambda *a: events.append('register')):
                enroll.enroll(args)
            self.assertEqual(events, ['policy', 'probe', 'policy', 'register'])
            client.revoke.assert_called_once_with('key-id')
            client.abort.assert_not_called()

    def test_creation_claim_cannot_be_reused(self):
        import tempfile
        from pathlib import Path
        from worker import create
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claim.json'
            create.claim(path, {'project': 'example'})
            with self.assertRaises(FileExistsError):
                create.claim(path, {'project': 'another'})
            self.assertEqual(json.loads(path.read_text()), {'project': 'example'})

    def test_creation_requires_policy_and_stops_after_ambiguous_native_failure(self):
        import tempfile
        from pathlib import Path
        from worker import create
        for stage in ('policy', 'existing', 'create'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                profile = Path(directory)
                (profile / 'tailnet-policy.json').write_text('{}')
                cloud = mock.Mock(profile=profile, secrets=profile, project='example', zone='zone',
                    instance='worker', values={'ci-buddy-tailnet': 'example.test', 'public': 'no credentials',
                    'ci-buddy-owner-addresses': '100.64.0.1', 'ci-buddy-coordinator-addresses': '100.64.0.2'},
                    template={'machineType': 'approved-size'})
                cloud.json.return_value = [{}] if stage == 'existing' else []
                cloud.command.side_effect = ValueError('ambiguous creation')
                client = mock.Mock()
                if stage == 'policy':
                    client.verify.side_effect = ValueError('policy not approved')
                args = types.SimpleNamespace(vm='example/zone/worker', profile=profile)
                with mock.patch.object(create.enroll, 'Cloud', return_value=cloud), \
                     mock.patch.object(create, 'Policy', return_value=client), \
                     mock.patch.object(create, 'approved', return_value=({'subnetwork': 'approved-subnet'},
                        {'sourceImage': 'approved-image', 'diskSizeGb': '200'})), \
                     mock.patch.object(create.enroll, 'source_archive'), \
                     mock.patch.object(create, 'claim') as claim, \
                     mock.patch.object(create, 'wait_bootstrap') as wait, \
                     mock.patch.object(create.enroll, 'enroll') as enrollment:
                    with self.assertRaises(ValueError):
                        create.create(args)
                    if stage == 'create':
                        self.assertEqual(claim.call_count, 2)
                        cloud.command.assert_called_once()
                        arguments = cloud.command.call_args.args
                        self.assertIn('--no-address', arguments)
                        self.assertIn('--no-service-account', arguments)
                        self.assertTrue(any(value.startswith('--metadata-from-file=') for value in arguments))
                        self.assertNotIn('no credentials', str(arguments))
                    else:
                        claim.assert_not_called()
                        cloud.command.assert_not_called()
                    wait.assert_not_called()
                    enrollment.assert_not_called()

    def test_runtime_bundle_upload_keeps_join_key_out_of_argv(self):
        from pathlib import Path
        from worker import enroll
        cloud = mock.Mock(root='/work', identifier='123', account='worker')
        cloud.ssh.return_value = b'100.64.1.2\n'
        marker = 'fixture-not-an-operational-key'
        endpoint = enroll.install_worker(cloud, 'a' * 40, b'archive', marker)
        command, payload = cloud.ssh.call_args.args
        self.assertNotIn(marker, command)
        length = int.from_bytes(payload[:8], 'big')
        self.assertEqual(payload[8:8+length].decode(), marker)
        self.assertEqual(payload[8+length:], b'archive')
        self.assertEqual(endpoint, 'worker@100.64.1.2:/work')


if __name__ == '__main__':
    unittest.main()
