"""Real enrollment payload/registration paths with isolated native installer fixtures."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

import enroll


@unittest.skipUnless(os.name == 'posix', 'the local runner fixture needs Linux')
class Enrollment(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(os.chdir, Path.cwd())
        tools = self.root / 'tools'
        tools.mkdir()
        record = dict(name='existing', host='local', kind='owned', online=True)
        (tools / 'tailworker').write_text('import json\nprint(' + repr(json.dumps(record)) + ')\n')
        self.enterContext(patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                                     GIT_CONFIG_GLOBAL=str(self.root / 'owner.gitconfig'), GIT_CONFIG_NOSYSTEM='1'))
        self.revision = 'a' * 40
        script = r'''set -eu
python3 - "$2" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1]); binary=root/'bin'; binary.mkdir(exist_ok=True)
if (root/'fail-install').exists(): raise SystemExit(7)
revision='a'*40
(binary/'revision').write_text(revision+'\n')
(binary/'ci-buddy').write_text("printf '%s\\n' "+revision+"\n")
(binary/'pueue').write_text('#!'+sys.executable+'\n'); (binary/'pueue').chmod(0o700)
with (root/'installs').open('a') as output: output.write('installed\n')
PY
'''
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as bundle:
            body = script.encode()
            member = tarfile.TarInfo('install.sh')
            member.size = len(body)
            bundle.addfile(member, io.BytesIO(body))
        self.archive = archive.getvalue()
        self.conf = 'jobs = 1\nslots = 1\nmemory = "1g"\ncpus = "1"\nmin_free_gb = 500\nslot_max_gb = 5\nworkers = []\n'
        config = self.root / 'leaf.conf'
        config.write_text(self.conf)
        self.leaf = self.root / 'leaf'
        self.coordinator = 'local:' + str(self.root / 'coordinator')
        enroll.invoke(self.coordinator, dict(mode='install', revision=self.revision, conf=self.conf), self.archive)
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', self.coordinator], check=True)
        self.args = types.SimpleNamespace(name='existing', root=str(self.leaf), host_conf=str(config), coordinator=None)
        self.enterContext(patch.object(enroll, 'runtime', return_value=(self.revision, self.archive)))
        self.auth = self.enterContext(patch.object(enroll.auth, 'client'))

    def routes(self):
        return subprocess.check_output(['git', 'config', '--global', '--get-all', 'ci-buddy.host']).decode().splitlines()

    def test_install_then_auth_then_active_registration_and_idempotence(self):
        endpoint = 'local:' + str(self.leaf)
        def sync(arguments):
            self.assertIn(endpoint, self.routes())
            self.assertTrue((self.leaf / 'bin/revision').is_file())
            self.assertNotIn(endpoint, (self.root / 'coordinator/host.conf').read_text())
        self.auth.side_effect = sync
        enroll.enroll(self.args)
        self.assertIn(endpoint, (self.root / 'coordinator/host.conf').read_text())
        self.assertEqual((self.leaf / 'installs').read_text(), 'installed\n')
        self.auth.side_effect = None
        enroll.enroll(self.args)
        self.assertEqual((self.leaf / 'installs').read_text(), 'installed\n')
        self.assertEqual(self.routes().count(endpoint), 1)

    def test_registration_failure_restores_only_new_route_and_configuration(self):
        (self.root / 'coordinator/fail-install').touch()
        with self.assertRaises(subprocess.CalledProcessError):
            enroll.enroll(self.args)
        self.assertEqual(self.routes(), [self.coordinator])
        self.assertEqual((self.root / 'coordinator/host.conf').read_text(), self.conf)
        self.assertTrue((self.leaf / 'bin/revision').exists())

    def test_existing_route_survives_auth_failure(self):
        endpoint = 'local:' + str(self.leaf)
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', endpoint], check=True)
        self.auth.side_effect = ValueError('fixture credentials unavailable')
        with self.assertRaisesRegex(ValueError, 'credentials unavailable'):
            enroll.enroll(self.args)
        self.assertIn(endpoint, self.routes())
        self.assertEqual((self.root / 'coordinator/host.conf').read_text(), self.conf)

    def test_self_dispatch_is_rejected_without_changing_configuration(self):
        self.args.root = str(self.root / 'coordinator')
        with self.assertRaises(subprocess.CalledProcessError):
            enroll.enroll(self.args)
        self.assertEqual(self.routes(), [self.coordinator])
        self.assertEqual((self.root / 'coordinator/host.conf').read_text(), self.conf)


if __name__ == '__main__':
    unittest.main()
