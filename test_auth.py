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

    def host(self, name, workers=()):
        root = self.root / name
        (root / 'bin').mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        for source in ('auth.py', 'ssh.py', 'safe_output.py'):
            shutil.copyfile(Path(__file__).with_name(source), root / 'bin' / source)
        (root / 'host.conf').write_text('workers = ' + repr(list(workers)) + '\n')
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
        with self.assertRaises(ValueError):
            auth.client(['local:' + str(new), endpoint])
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', 'local:' + str(new)], check=True)
        auth.client(['local:' + str(new), endpoint])
        self.assertEqual(auth.read(new), self.token)
        self.token = ('github_pat_' + secrets.token_hex(40)).encode()
        with patch('getpass.getpass', return_value=self.token.decode()), contextlib.redirect_stdout(output):
            auth.client([])
        auth.client(['local:' + str(new), endpoint])
        self.assertEqual(auth.read(new), self.token)

    def test_private_files_and_invalid_input(self):
        root = self.host('coordinator')
        with self.assertRaises(ValueError):
            auth.store(root, self.token + b'\npassword=unexpected')
        self.assertFalse((root / 'github-token').exists())
        auth.store(root, self.token)
        (root / 'github-token').chmod(0o644)
        with self.assertRaises(ValueError):
            auth.read(root)
        (root / 'github-token').unlink()
        (root / 'github-token').symlink_to(self.root / 'owner.gitconfig')
        with self.assertRaises(OSError):
            auth.read(root)
        for endpoint in ('public:/work/path\nbad', 'local:relative', '-option:/tmp'):
            with self.assertRaises(ValueError):
                auth.rpc(endpoint, 'store', self.token)
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
        master, terminal = os.openpty()
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('auth.py')), 'client'],
                                   stdin=terminal, stdout=terminal, stderr=terminal, start_new_session=True)
        transcript = b''
        try:
            while not transcript.endswith(b'read-only): '):
                transcript += os.read(master, 4096)
            self.assertFalse(termios.tcgetattr(terminal)[3] & termios.ECHO)
            os.write(master, self.token + b'\n')
            os.close(terminal)
            terminal = None
            while True:
                try:
                    block = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not block:
                    break
                transcript += block
            self.assertEqual(process.wait(), 0)
            self.assertNotIn(self.token, transcript)
            self.assertEqual(auth.read(root), self.token)
        finally:
            if terminal is not None:
                os.close(terminal)
            os.close(master)
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
