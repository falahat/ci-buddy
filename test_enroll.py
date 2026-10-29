"""Coordinator setup uses the reviewed runtime installer and native command kit."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import enroll


@unittest.skipUnless(os.name == 'posix', 'coordinator setup needs Linux')
class Enrollment(unittest.TestCase):
    def script(self, path, body):
        path.write_text('#!' + shutil.which('bash') + '\nset -eu\n' + body)
        path.chmod(0o700)

    def runtime(self, link=False):
        for name in enroll.data.RUNTIME:
            (self.root / 'bin' / name).write_text('')
        (self.root / 'bin/units').mkdir(exist_ok=True)
        installer = Path(enroll.__file__).with_name('install.sh')
        self.script(self.root / 'bin/install.sh',
            'if [[ $3 == --worker ]]; then\nmkdir -p "$2/bin"\ncp "$(dirname "$0")/revision" "$2/bin/revision"\n' +
            'else exec bash ' + shlex.quote(str(installer)) + ' "$@"; fi\n')
        if link:
            (self.root / 'bin/fetch.sh').unlink()
            (self.root / 'bin/fetch.sh').symlink_to('../../escape')

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(os.chdir, Path.cwd())
        self.leaf = self.root / 'leaf'
        self.endpoint = 'local:' + str(self.leaf)
        for name in ('bin', 'dispatch', 'kit', 'tools', 'profiles'):
            (self.root / name).mkdir()
        self.conf = 'jobs=2\nslots=2\nmemory="1g"\ncpus="1"\nmin_free_gb=500\nslot_max_gb=5\nfetch_hosts=[]\n'
        (self.root / 'host.conf').write_text(self.conf)
        (self.root / 'worker.conf').write_text(self.conf)
        (self.root / 'bin/revision').write_text('a' * 40)
        self.runtime()
        record = dict(name='existing', host='local', kind='owned', online=True)
        # Unit jobs may be UID 0 inside a rootless container; simulate the host prerequisite.
        self.script(self.root / 'tools/id', 'test "$1" = -u\nprintf "1000\\n"\n')
        self.script(self.root / 'tools/tailworker', 'printf "%s\\n" ' + shlex.quote(json.dumps(record)) + '\n')
        info = dict(dispatcher='100.64.0.1:7300', cas='http://100.64.0.1:7301',
                    kit=str(self.root / 'kit'), runner=str(self.root / 'runner'), node='coordinator', signer='false')
        self.script(self.root / 'bin/plane.sh', 'printf "%s\\n" ' + shlex.quote(json.dumps(info)) + '\n')
        (self.root / 'kit/common.sh').write_text('remote() { test "$1" = local; shift; "$@"; }\n')
        self.script(self.root / 'tools/systemd-escape', 'printf \"fixture-root\\n\"\n')
        self.script(self.root / 'tools/systemctl', 'test "$2" = enable\n')
        events = shlex.quote(str(self.root / 'events'))
        self.script(self.root / 'kit/install.sh', 'test "$1" = --env\ncp "$2" ' +
                    shlex.quote(str(self.root / 'profiles')) + '/"$(basename "$2")"\nprintf "installed\\n" >>' + events + '\n')
        self.script(self.root / 'kit/enroll.sh', 'grep -Fq ' + shlex.quote(self.endpoint) + ' ' +
                    shlex.quote(str(self.root / 'host.conf')) + '\nprintf "start\\n" >>' + events +
                    '\ntest ! -e ' + shlex.quote(str(self.root / 'fail-start')) + '\n')
        self.enterContext(patch.dict(os.environ, PATH=str(self.root / 'tools') + os.pathsep + os.environ['PATH']))
        self.enterContext(patch.object(enroll.auth, 'read', return_value=b'fixture-token'))
        def token(*arguments):
            self.assertEqual(arguments, (self.endpoint, 'store', b'fixture-token'))
            with (self.root / 'events').open('a') as stream:
                stream.write('token\n')
        self.token = self.enterContext(patch.object(enroll.auth, 'rpc', side_effect=token))
        self.args = types.SimpleNamespace(name='existing', root=str(self.leaf), host_conf=str(self.root / 'worker.conf'))

    def test_runtime_token_roster_then_native_start(self):
        enroll.enroll(self.root, self.args)
        self.assertEqual((self.root / 'events').read_text(), 'installed\ntoken\nstart\n')
        self.assertEqual((self.leaf / 'bin/revision').read_text(), 'a' * 40)
        self.assertEqual(enroll.data.host_config(self.root)['fetch_hosts'], [self.endpoint])
        profiles = list((self.root / 'profiles').iterdir())
        self.assertEqual(len(profiles), 1)
        values = dict(shlex.split(line)[0].split('=', 1) for line in profiles[0].read_text().splitlines())
        self.assertEqual(values['COUNT'], '2')
        self.assertEqual(shlex.split(values['COMMAND']), [shutil.which('python3'), str(self.leaf / 'bin/execute.py'), str(self.leaf)])
        self.assertEqual(values['LANES'], 'ci-' + 'a' * 12)

    def test_partial_start_retains_fetch_host_for_rotation(self):
        (self.root / 'fail-start').touch()
        self.assertRaises(subprocess.CalledProcessError, enroll.enroll, self.root, self.args)
        self.assertEqual(enroll.data.host_config(self.root)['fetch_hosts'], [self.endpoint])
        self.assertEqual((self.root / 'events').read_text(), 'installed\ntoken\nstart\n')

    def test_token_failure_does_not_register_or_start(self):
        self.token.side_effect = ValueError('fixture token unavailable')
        self.assertRaisesRegex(ValueError, 'token unavailable', enroll.enroll, self.root, self.args)
        self.assertEqual((self.root / 'host.conf').read_text(), self.conf)
        self.assertEqual((self.root / 'events').read_text(), 'installed\n')

    def test_existing_root_and_archive_containment(self):
        self.leaf.mkdir(mode=0o700)
        (self.leaf / 'host.conf').write_text(self.conf)
        (self.leaf / 'bin').mkdir()
        (self.leaf / 'bin/revision').write_text('b' * 40)
        self.assertRaises(subprocess.CalledProcessError, enroll.enroll, self.root, self.args)
        self.assertEqual((self.leaf / 'bin/revision').read_text(), 'b' * 40)
        self.runtime(link=True)
        self.assertRaises(subprocess.CalledProcessError, enroll.enroll, self.root, self.args)


if __name__ == '__main__':
    unittest.main()
