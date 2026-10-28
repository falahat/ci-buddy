"""Real enrollment payload/registration paths with isolated native installer fixtures."""
import io
import json
import os
from pathlib import Path
import shutil
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
        commands = {'systemd-escape': "printf 'fixture\\n'\n",
                    'systemctl': 'printf "%s\\n" "$*" >>"' + str(self.root / 'reloads') + '"\n'
                                 'find "' + str(self.root) + '" -name fail-reload | grep -q . && exit 7\nexit 0\n'}
        for name, body in commands.items():
            service = tools / name
            service.write_text('#!' + shutil.which('sh') + '\n' + body)
            service.chmod(0o700)
        self.enterContext(patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                                     GIT_CONFIG_GLOBAL=str(self.root / 'owner.gitconfig'), GIT_CONFIG_NOSYSTEM='1'))
        self.revision = 'a' * 40
        script = r'''set -eu
test ! -e "$2/fail-install" || exit 7
mkdir -p "$2/bin"
printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa >"$2/bin/revision"
printf 'cat "$(dirname "$0")/revision"\n' >"$2/bin/ci-buddy"
printf '1\n' >"$2/capacity"
test "$3" = --worker
printf 'installed\n' >>"$2/installs"
'''
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as bundle:
            body = script.encode()
            member = tarfile.TarInfo('install.sh')
            member.size = len(body)
            bundle.addfile(member, io.BytesIO(body))
        self.archive = archive.getvalue()
        self.conf = '''jobs = 1
slots = 1
memory = "1g"
cpus = "1"
min_free_gb = 500
slot_max_gb = 5
workers = []
'''
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

    def reject(self, error, routes, message=''):
        self.assertRaisesRegex(error, message, enroll.enroll, self.args)
        self.assertEqual(self.routes(), routes)
        self.assertEqual((self.root / 'coordinator/host.conf').read_text(), self.conf)

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
        self.assertNotIn(endpoint, self.routes())
        self.assertEqual((self.root / 'coordinator/capacity').read_text(), '2\n')
        self.assertEqual((self.root / 'reloads').read_text(), '--user reload ci-buddy-dispatch@fixture.service\n')
        for root in (self.leaf, self.root / 'coordinator'):
            self.assertEqual((root / 'installs').read_text(), 'installed\n')
            self.assertFalse(list(root.glob('enroll-*')))

    def test_registration_failure_restores_only_new_route_and_configuration(self):
        (self.root / 'coordinator/fail-reload').touch()
        self.reject(subprocess.CalledProcessError, [self.coordinator])
        self.assertTrue((self.leaf / 'bin/revision').exists())
        self.assertEqual((self.root / 'coordinator/capacity').read_text(), '1\n')
        self.assertFalse(list((self.root / 'coordinator').glob('enroll-*')))

    def test_existing_route_survives_auth_failure(self):
        endpoint = 'local:' + str(self.leaf)
        subprocess.run(['git', 'config', '--global', '--add', 'ci-buddy.host', endpoint], check=True)
        self.auth.side_effect = ValueError('fixture credentials unavailable')
        self.reject(ValueError, [self.coordinator, endpoint], 'credentials unavailable')

    def test_self_dispatch_is_rejected_without_changing_configuration(self):
        self.args.root = str(self.root / 'coordinator')
        self.reject(subprocess.CalledProcessError, [self.coordinator])


if __name__ == '__main__':
    unittest.main()
