"""Offline SDK/serial fixtures; these never provision or contact a machine."""
import argparse
import ast
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from unittest import mock
from worker import create

PUBLIC = 'ssh-ed25519 ' + base64.b64encode(b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'\x01' * 32).decode()
NONCE = 'a' * 32
BOOT = b'CI_BUDDY_BOOTSTRAP_READY\n'
READY = b'CI_BUDDY_READY ' + json.dumps(dict(ip='100.64.0.7', hostkey=PUBLIC)).encode() + b'\n'


class Process:
    def __init__(self, output, fail_write=False, observe=None):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO(output)
        if fail_write:
            original = self.stdin.write
            def write(value):
                if not value.startswith(b'CI_BUDDY_CHALLENGE '):
                    raise BrokenPipeError()
                return original(value)
            self.stdin.write = write
        if observe:
            original_read = self.stdout.readline
            def read(size):
                line = original_read(size)
                observe(line, self.stdin.getvalue())
                return line
            self.stdout.readline = read
    def __enter__(self):
        return self
    def __exit__(self, *error):
        return False
    def poll(self):
        return 0
    def wait(self):
        return 0


@unittest.skipUnless(os.name == 'posix', 'owner setup requires POSIX private-file ownership')
class WorkerCreate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'secrets'
        self.folder.mkdir(mode=0o700)
        self.key = self.folder / 'join'
        self.secret = ('tskey-auth-' + secrets.token_hex(24)).encode()
        self.write_key()
        self.conf = self.root / 'host.conf'
        self.conf.write_text('jobs=1\nslots=2\nmemory="1g"\ncpus="1"\nmin_free_gb=500\nslot_max_gb=100\nworkers=[]\n')
        self.args = argparse.Namespace(vm='project/zone/worker', root='/work/worker', auth_key_file=self.key,
            host_conf=self.conf, disk_size_gb=600, machine_type='example-machine', subnet='example-subnet', pubkey_file=[])
    def write_key(self):
        self.key.write_bytes(self.secret)
        self.key.chmod(0o600)
    def exchange(self, output, **options):
        process = Process(output, **options)
        with mock.patch.object(create.secrets, 'token_hex', return_value=NONCE), mock.patch.object(create.subprocess, 'Popen', return_value=process) as launch:
            result = create.exchange(['gcloud', 'serial', '--ssh-key-file=' + str(self.folder / 'ssh')], self.key)
        self.assertNotIn(self.secret, repr(launch.call_args).encode())
        return result, process

    def test_fresh_challenge_ignores_replay_and_only_stdin_gets_secret(self):
        marker = ('CI_BUDDY_KEY_READY ' + NONCE + '\n').encode()
        def observe(line, written):
            if line == BOOT:
                self.assertEqual(written, b'')
            if line == marker:
                self.assertNotIn(self.secret, written)
        output = b'CI_BUDDY_KEY_READY old\n' + READY + BOOT + marker + READY
        result, process = self.exchange(output, observe=observe)
        self.assertEqual(result['ip'], '100.64.0.7')
        self.assertEqual(process.stdin.getvalue(), ('CI_BUDDY_CHALLENGE ' + NONCE + '\n').encode() + self.secret + b'\n')
        self.assertFalse(self.key.exists())

    def test_attempted_send_failure_consumes_key_but_early_eof_does_not(self):
        with self.assertRaises(BrokenPipeError):
            self.exchange(BOOT + ('CI_BUDDY_KEY_READY ' + NONCE + '\n').encode(), fail_write=True)
        self.assertFalse(self.key.exists())
        self.write_key()
        with self.assertRaisesRegex(ValueError, 'no retry'):
            self.exchange(b'CI_BUDDY_KEY_READY stale\n')
        self.assertTrue(self.key.exists())

    def test_malformed_ready_identity_and_output_bounds(self):
        bad = [b'not-json', json.dumps(dict(ip='10.0.0.7', hostkey=PUBLIC)).encode(),
            json.dumps(dict(ip='8.8.8.8', hostkey=PUBLIC)).encode(),
            json.dumps(dict(ip='100.64.0.7', hostkey='ssh-ed25519 A')).encode(),
            json.dumps(dict(ip='100.64.0.7', hostkey='ssh-rsa AAAA')).encode()]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.write_key()
                self.exchange(BOOT + ('CI_BUDDY_KEY_READY ' + NONCE + '\n').encode() + b'CI_BUDDY_READY ' + value + b'\n')
            self.assertFalse(self.key.exists())
        for output in (b'x' * 8193, (b'x' * 8191 + b'\n') * 129):
            with self.subTest(bound=len(output)), self.assertRaisesRegex(ValueError, 'byte limit'):
                self.write_key()
                self.exchange(output)

    def sdk(self, ambiguous=False):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == 'git':
                return subprocess.CompletedProcess(command, 128)
            if command[0] == 'ssh-keygen':
                path = Path(command[command.index('-f') + 1])
                path.write_bytes(b'nonoperative-owner-key-fixture')
                path.chmod(0o600)
                path.with_suffix('.pub').write_text(PUBLIC + '\n')
            if ambiguous and command[0] == 'gcloud' and 'create' in command:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        return calls, run

    def test_one_public_create_and_reviewed_installer_stdin(self):
        calls, run = self.sdk()
        archive = b'reviewed-runtime-archive-fixture'
        with mock.patch.object(create, 'runtime', return_value=('a' * 40, archive)), mock.patch.object(create, 'exchange', return_value=json.loads(READY[15:])), mock.patch.object(create.subprocess, 'run', side_effect=run), mock.patch.object(create.subprocess, 'check_output', return_value=b'[]'), contextlib.redirect_stdout(io.StringIO()):
            create.create(self.args)
        creation = [command for command, _ in calls if command[0] == 'gcloud' and 'create' in command]
        self.assertEqual(len(creation), 1)
        for flag in ('--no-address', '--no-service-account', '--no-scopes'):
            self.assertIn(flag, creation[0])
        startup = Path(next(value.split('=', 2)[2] for value in creation[0] if value.startswith('--metadata-from-file=')))
        self.assertNotIn(self.secret, startup.read_bytes())
        source = startup.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        encoded = next(node.args[0].value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'b64decode' and isinstance(node.args[0], ast.Constant))
        self.assertNotIn(self.secret, base64.b64decode(encoded))
        self.assertNotIn(self.secret, repr(calls).encode())
        installer = [(command, options) for command, options in calls if command[0] == 'ssh']
        self.assertEqual(len(installer), 1)
        self.assertEqual(installer[0][1]['input'], archive)
        self.assertIn('install.sh', installer[0][0][-1])

    def test_ambiguous_create_retains_claim_without_repeat_or_delete(self):
        calls, run = self.sdk(ambiguous=True)
        with mock.patch.object(create, 'runtime', return_value=('a' * 40, b'archive')), mock.patch.object(create.subprocess, 'run', side_effect=run), mock.patch.object(create.subprocess, 'check_output', return_value=b'[]'), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(subprocess.CalledProcessError):
                create.create(self.args)
            with self.assertRaises(FileExistsError):
                create.create(self.args)
        self.assertTrue((self.folder / 'ci-buddy-project').is_dir())
        self.assertEqual(sum(command[0] == 'gcloud' and 'create' in command for command, _ in calls), 1)
        self.assertFalse(any('delete' in command for command, _ in calls))
        self.assertIn('may exist and incur charges', errors.getvalue())


class StartupSyntax(unittest.TestCase):
    def test_production_bash_and_embedded_python(self):
        path = Path(create.__file__).with_name('startup.sh')
        source = path.read_text()
        ast.parse(source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0])
        subprocess.run(['bash', '-n', str(path)], check=True)


if __name__ == '__main__':
    unittest.main()
