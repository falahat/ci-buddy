import contextlib
import errno
import io
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import termios
from unittest.mock import patch
import auth


class Credentials(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(self.root / 'owner.gitconfig'),
                                     GIT_CONFIG_NOSYSTEM='1'))
        self.token = ('github_pat_' + secrets.token_hex(40)).encode()

    def host(self, name, fetch_hosts=()):
        root = self.root / name
        (root / 'bin').mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        for source in ('auth.py', 'ssh.py', 'safe_output.py'):
            shutil.copyfile(Path(__file__).with_name(source), root / 'bin' / source)
        (root / 'host.conf').write_text('fetch_hosts = ' + repr(list(fetch_hosts)) + '\n')
        return root

    def test_enroll_rotate_and_provision_new_endpoint(self):
        worker = self.host('worker')
        coordinator = self.host('coordinator', ['local:' + str(worker)])
        endpoint = 'local:' + str(coordinator)
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', endpoint], check=True)
        output = io.StringIO()
        with patch('getpass.getpass', return_value=self.token.decode()), contextlib.redirect_stdout(output):
            auth.client([])
        for root in (coordinator, worker):
            self.assertEqual(auth.read(root), self.token)
            self.assertEqual(stat.S_IMODE((root / 'github-token').stat().st_mode), 0o600)
        self.assertNotIn(self.token.decode(), output.getvalue())
        self.assertNotIn(self.token, (self.root / 'owner.gitconfig').read_bytes())
        new = self.host('new')
        self.assertRaises(ValueError, auth.client, ['local:' + str(new), endpoint])
        self.assertFalse((new / 'github-token').exists())
        (coordinator / 'host.conf').write_text('fetch_hosts = ' + repr(['local:' + str(worker), 'local:' + str(new)]) + '\n')
        auth.install(coordinator, self.token)
        self.assertEqual(auth.read(new), self.token)
        self.token = ('github_pat_' + secrets.token_hex(40)).encode()
        with patch('getpass.getpass', return_value=self.token.decode()), contextlib.redirect_stdout(output):
            auth.client([])
        self.assertEqual(auth.read(new), self.token)
        offline = 'local:' + str(self.root / 'offline')
        (coordinator / 'host.conf').write_text('fetch_hosts = ' + repr([offline]) + '\n')
        with self.assertRaisesRegex(ValueError, 'incomplete distribution'), contextlib.redirect_stderr(output):
            auth.install(coordinator, self.token)
        self.assertIn('credential unavailable: ' + offline, output.getvalue())

    def test_private_files_and_invalid_input(self):
        root = self.host('coordinator')
        self.assertRaises(ValueError, auth.store, root, self.token + b'\npassword=unexpected')
        self.assertFalse((root / 'github-token').exists())
        auth.store(root, self.token)
        (root / 'github-token').chmod(0o644)
        self.assertRaises(ValueError, auth.read, root)
        (root / 'github-token').unlink()
        (root / 'github-token').symlink_to(self.root / 'owner.gitconfig')
        self.assertRaises(OSError, auth.read, root)
        for endpoint in ('public:/work/path\nbad', 'local:relative', '-option:/tmp'):
            self.assertRaises(ValueError, auth.rpc, endpoint, 'store', self.token)
        owned = self.host('.ci-buddy')
        submitted = self.root / 'submitted'
        malicious = submitted / '.ci-buddy' / 'bin'
        malicious.mkdir(parents=True)
        marker = self.root / 'executed'
        (malicious / 'auth.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        with patch('auth.Path.home', return_value=self.root), contextlib.chdir(submitted):
            auth.rpc('local', 'store', self.token)
        self.assertEqual(auth.read(owned), self.token)
        self.assertFalse(marker.exists(), 'submitted source executed during credential sync')

    def test_real_prompt_disables_echo(self):
        root = self.host('coordinator')
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', 'local:' + str(root)], check=True)
        master_fd, terminal_fd = os.openpty()
        with os.fdopen(master_fd, 'rb', buffering=0) as master, os.fdopen(terminal_fd, 'wb', buffering=0) as terminal:
            process = subprocess.Popen(['bash', str(Path(__file__).with_name('ci-buddy')), 'auth'],
                                       stdin=terminal, stdout=terminal, stderr=terminal, start_new_session=True)
            transcript = b''
            try:
                while not transcript.endswith(b'read-only): '):
                    transcript += master.read(4096)
                self.assertFalse(termios.tcgetattr(terminal)[3] & termios.ECHO)
                os.write(master.fileno(), self.token + b'\n')
                terminal.close()
                try:
                    while block := master.read(4096):
                        transcript += block
                except OSError as error:
                    self.assertEqual(error.errno, errno.EIO)
                self.assertEqual(process.wait(), 0)
                self.assertNotIn(self.token, transcript)
                self.assertEqual(auth.read(root), self.token)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait()

    def test_native_git_helper_preserves_other_providers(self):
        root = self.host('fetch')
        auth.store(root, self.token)
        config = root / 'gitconfig'
        config.write_text('''[credential]
 helper = "!f() { printf 'username=legacy\\\\npassword=other-provider\\\\n'; }; f"
[url "ssh://git@github.com/"]
 insteadOf = https://github.com/
[url "ssh://git@code.example/"]
 insteadOf = https://code.example/
''')
        prefix = Path(__file__).with_name('fetch.sh').read_text().split("<<'PY'\n", 1)[1].split('\n\ndef checked', 1)[0]
        prefix = prefix.replace('/work/token', str(root / 'github-token')).replace('/run/secrets/gitconfig', str(config))
        namespace = {}
        with patch.object(sys, 'argv', ['fetch', '']):
            exec(compile(prefix, 'fetch.sh', 'exec'), namespace)
        env = namespace['env']
        result = subprocess.check_output(['git', 'credential', 'fill'],
            input=b'protocol=https\nhost=github.com\npath=owner/private.git\n\n', env=env)
        values = dict(line.split(b'=', 1) for line in result.splitlines())
        self.assertTrue(values[b'password'] == self.token, 'GitHub helper did not supply enrolled token')
        result = subprocess.check_output(['git', 'credential', 'fill'],
            input=b'protocol=https\nhost=code.example\n\n', env=env)
        self.assertIn(b'password=other-provider', result)
        for source in ('https://github.com/owner/private', 'git@github.com:owner/private',
                       'ssh://git@github.com/owner/private'):
            rewritten = subprocess.check_output(['git', 'ls-remote', '--get-url', source], env=env)
            self.assertEqual(rewritten.strip(), b'https://github.com/owner/private')
        rewritten = subprocess.check_output(['git', 'ls-remote', '--get-url', 'https://code.example/repo'], env=env)
        self.assertEqual(rewritten.strip(), b'ssh://git@code.example/repo')


if __name__ == '__main__':
    unittest.main()
