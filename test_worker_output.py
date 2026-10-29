"""Adversarial checks through raw worker accounting and history ingestion."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))
import data
import history
import results
import safe_output
import control
import subprocess


class WorkerOutput(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)))
        self.folder = self.root / 'projects/example'
        self.out = self.folder / 'runs/7'
        self.out.mkdir(parents=True)
        (self.root / 'containers').mkdir()
        self.job = dict(kind='test', key='key', tree='a' * 40, commit='b' * 40,
                        branch='topic', submitter='owner', argv=[], queued_at=1)
        self.account = dict(queued_at=2, started_at=3, ended_at=4, exit=0, overall_exit=0,
                            cpu_s=1.5, peak_bytes=1024)
        (self.out / 'job.json').write_text(json.dumps(self.job))
        for name in ('slot', 'exit'):
            (self.out / name).write_text('')
        (self.out / 'task').write_text('owner-task')
        (self.out / 'runner.log').write_text('coordinator output\n')

    def archive(self, entries, unpack=True):
        path = self.root / 'return.tar'
        with tarfile.open(path, 'w') as archive:
            for name, mode, kind, body in entries:
                body = b'x' * body if isinstance(body, int) else body
                item = tarfile.TarInfo(name)
                item.mode, item.type, item.size = mode, kind, len(body)
                archive.addfile(item, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
        if not unpack:
            return path
        destination = self.root / 'results'
        destination.mkdir(exist_ok=True)
        results.unpack_results(path, destination)
        return destination

    def xml(self, duration='0.002', body=''):
        return f'<testsuite><testcase classname="example" name="test" time="{duration}">{body}</testcase></testsuite>'

    def collect_output(self, contents, status=0):
        archive = self.archive([(name, 0o600, tarfile.REGTYPE, body.encode())
                                for name, body in contents.items()], unpack=False)
        compressed = subprocess.check_output(['zstd', '-q', '-c', str(archive)])
        def transfer(root, operation, address, destination, **options):
            self.assertEqual(operation, 'get')
            destination.write_bytes(compressed)
        action = dict(outputs={'run': dict(address='sha256:' + 'a' * 64, size=len(compressed))},
                      metrics={'exit': status}, runner={'node': 'native-worker'})
        with mock.patch.object(results, 'cas', side_effect=transfer):
            return results.collect(self.root, 'example', '7', action)

    def test_protocol_only_and_no_metadata_overwrite(self):
        forged = self.account | dict(id=True, kind='run', key='forged', tree='c' * 40,
                                      commit='c' * 40, branch='other', submitter='other',
                                      argv=['other'], injected_column='bad')
        contents = {'job.json': json.dumps(forged), 'slot': '1', 'report.txt': '\x1b[31mreport\n',
                    'runner.log': 'forged output', 'task': 'forged-task', 'tool.py': 'raise RuntimeError',
                    'phases.tsv': 'check\t0.25\t1\n', 'check.log': 'phase witness',
                    'junit/ci/junit.xml': self.xml()}
        self.assertEqual(self.collect_output(contents), 0)
        with closing(history.db(self.folder)) as database:
            row = dict(database.execute('SELECT * FROM runs').fetchone())
            expected = self.job | dict(id=7, argv='[]')
            self.assertEqual({key: row[key] for key in expected}, expected)
            self.assertEqual(database.execute('SELECT status,ms FROM results JOIN tests ON tests.id=results.test WHERE name=?',
                                               ('example::test',)).fetchone()[:], ('passed', 2))
            self.assertEqual(database.execute("SELECT excerpt FROM results JOIN tests ON tests.id=results.test WHERE name='phase/check'").fetchone()[0],
                             'phase witness')
        self.assertEqual((self.out / 'task').read_text(), 'owner-task')
        self.assertEqual((self.out / 'runner.log').read_text(), 'coordinator output\n')
        self.assertEqual((self.out / 'report.txt').read_text(), '[31mreport\n')
        self.assertFalse((self.out / 'tool.py').exists())

    def test_failed_profiling_preserves_successful_correctness_accounting(self):
        self.job.update(kind='gate', bench_required=True)
        (self.out / 'job.json').write_text(json.dumps(self.job))
        payload = self.account | dict(overall_exit=1, bench_queued_at=4, bench_started_at=5,
                                      bench_ended_at=7, bench_exit=1, bench_cpu_s=2.5, bench_peak_bytes=2048)
        self.assertEqual(self.collect_output({'job.json': json.dumps(payload), 'slot': '1',
                                             'report.txt': 'correctness green\nprofiling failed\n'}, status=1), 1)
        saved = json.loads((self.out / 'job.json').read_text())
        self.assertEqual((saved['exit'], saved['overall_exit'], saved['ended_at'], saved['cpu_s']), (0, 1, 4, 1.5))
        with closing(history.db(self.folder)) as database:
            self.assertEqual(database.execute('SELECT exit FROM runs').fetchone()[0], 1)
            phases = dict((name, (status, ms)) for name, status, ms in database.execute(
                'SELECT name,status,ms FROM results JOIN tests ON tests.id=results.test'))
            self.assertEqual(phases['phase/correctness'], ('passed', 1000))
            self.assertEqual(phases['phase/bench'], ('failed', 2000))
        for field, value in (('overall_exit', 0), ('bench_exit', True), ('bench_started_at', 3),
                             ('bench_ended_at', float('nan')), ('bench_cpu_s', -1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                results.accounting(payload | {field: value}, 1, 1, True)
        self.assertRaises(ValueError, results.accounting, payload, 1, 1, False)
        self.assertRaises(ValueError, results.accounting, self.account, 1, 0, True)

    def test_unsafe_archive_entries(self):
        bad = [('../escape', 0o600, tarfile.REGTYPE, 1),
               ('/tmp/escape', 0o600, tarfile.REGTYPE, 1),
               ('./report.txt', 0o600, tarfile.REGTYPE, 1),
               ('report.txt', 0o700, tarfile.REGTYPE, 1),
               ('report.txt', 0o600, tarfile.SYMTYPE, 0),
               ('report.txt', 0o600, tarfile.LNKTYPE, 0),
               ('report.txt', 0o600, tarfile.FIFOTYPE, 0),
               ('bad\x1bname', 0o600, tarfile.REGTYPE, 1),
               ('C:escape', 0o600, tarfile.REGTYPE, 1)]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.archive([entry])
        self.assertRaises(ValueError, self.archive, [('extra.txt', 0o600, tarfile.REGTYPE, 1)] * 2)
        for limit, name in (('OUTPUT_LIMIT', 'extra.txt'), ('ARCHIVE_LIMIT', 'extra.txt'),
                            ('HISTORY_LIMIT', 'report.txt')):
            with self.subTest(limit=limit), mock.patch.object(results, limit, 1), self.assertRaises(ValueError):
                self.archive([(name, 0o600, tarfile.REGTYPE, 2)])

    def test_canonical_identity_and_rejected_history(self):
        self.assertNotIn('queued_at', results.accounting(self.account, 1, 0))
        for field, value in (('exit', True), ('exit', 1), ('cpu_s', float('nan')),
                             ('cpu_s', -1), ('peak_bytes', 2.5), ('queued_at', None),
                             ('started_at', 5), ('ended_at', float('inf'))):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                results.accounting(self.account | {field: value}, 1, 0)
        for slot in (0, True, 1.5):
            self.assertRaises(ValueError, results.accounting, self.account, slot, 0)
        cases = [('phases.tsv', '../escape\t1\t1\n'), ('phases.tsv', 'test\tnan\t0\n')]
        cases += [('junit/ci/junit.xml', self.xml(value)) for value in ('-1', 'inf', 'nan')]
        cases += [('junit/ci/junit.xml', '<!DOCTYPE testsuite []>' + self.xml()),
                  ('junit/ci/junit.xml', '<broken'),
                  ('junit/ci/junit.xml', self.xml().replace('name="test"', 'name="' + 'x' * 4097 + '"'))]
        (self.out / 'exit').write_text('0')
        for name, body in cases:
            path = self.out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            os.utime(path, (0, 0))
            with self.subTest(name=name, body=body[:80]), self.assertRaises((ValueError, ET.ParseError)):
                data.record(self.out)
            path.unlink()
        path.write_text(self.xml(body='<failure>' + 'x' * 5000 + '</failure>'))
        data.record(self.out)
        with closing(history.db(self.folder)) as database:
            excerpt = database.execute("SELECT excerpt FROM results WHERE status='failed'").fetchone()[0]
            self.assertLessEqual(len(excerpt.encode()), 4096)

    def test_stream_limit_and_terminal_controls(self):
        raw = b'hello\x1b[31m\x07\r\bworld\n'
        output = io.BytesIO()
        queue = mock.MagicMock()
        queue.__enter__.return_value = queue
        queue.logs.return_value = dict(entries=[dict(offset=0, address='sha256:' + 'a' * 64, size=len(raw))],
                                       next_offset=len(raw), done=True)
        def download(root, operation, address, path, **options):
            path.write_bytes(raw)
        with mock.patch.object(control, 'client', return_value=queue), mock.patch.object(results, 'cas', side_effect=download), \
                mock.patch('sys.stdout', types.SimpleNamespace(buffer=output)):
            control.logs(self.root, 'task')
            with mock.patch.object(results, 'OUTPUT_LIMIT', 2), self.assertRaises(ValueError):
                control.logs(self.root, 'task')
        self.assertEqual(output.getvalue(), raw)
        filtered = io.StringIO()
        safe_output.stream(io.BufferedReader(io.BytesIO(raw + '\u202e'.encode())), filtered)
        self.assertEqual(filtered.getvalue(), 'hello[31mworld\n')


if __name__ == '__main__':
    unittest.main()
