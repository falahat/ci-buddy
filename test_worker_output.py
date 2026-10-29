"""Production raw parsing, summary validation and native-identity persistence."""
from contextlib import closing
import copy
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))
import control
import history
import results
import safe_output


class WorkerOutput(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.identifier = 'j_' + '0' * 26
        self.artifacts = self.root / 'artifacts'
        self.step = self.artifacts / 'steps/correctness'
        self.out = self.artifacts / 'out'
        for path in (self.step, self.out, self.root / 'containers'):
            path.mkdir(parents=True)
        self.job = dict(kind='gate', key='a' * 64, tree='a' * 40, commit='b' * 40, base='c' * 40,
                        branch='topic', submitter='owner', rerun=False, queued_at=1,
                        steps=[dict(name='correctness', kind='gate', exclusive=False, argv=[],
                                    queued_at=1, started_at=2, ended_at=4, exit=0, phases=[['command', 2]])], exit=0)
        for name in ('fetch-stats', 'stats'):
            (self.step / name).write_text('usage_usec 750000\nmemory_peak_bytes=1024\noom_kill 0\n')

    def xml(self, duration='0.002', body=''):
        return f'<testsuite><testcase classname="example" name="test" time="{duration}">{body}</testcase></testsuite>'

    def normalize(self):
        return results.normalize(self.artifacts, self.job, self.identifier, 1)

    def collect(self, value):
        payload = json.dumps(value).encode()
        action = dict(outputs={name: dict(address='sha256:' + digest * 64, size=size)
                               for name, digest, size in (('results', 'a', len(payload)), ('logs', 'b', 6))},
                      metrics={'exit': value['exit']}, runner={'node': 'native-worker'})
        with patch.object(results, 'cas', side_effect=lambda root, operation, address, path, **options:
                          path.write_bytes(payload if address.endswith('a' * 64) else b'opaque')) as download:
            status = results.collect(self.root, 'example', self.identifier, action, self.job)
        self.assertEqual(download.call_count, 2)
        self.assertEqual((self.root / 'projects/example/logs' / (self.identifier + '.tar.zst')).read_bytes(), b'opaque')
        return status

    def test_raw_to_history_preserves_source_identity_and_failure_excerpt(self):
        (self.step / 'junit.xml').write_text(self.xml())
        (self.out / 'job.json').write_text('{"commit":"forged","kind":"run"}')
        (self.out / 'report.txt').write_text('\x1b[31mreport\n')
        (self.out / 'phases.tsv').write_text('fmt.all\t0.25\t1\n')
        (self.out / 'fmt.all.log').write_text('x' * 5000)
        value = self.normalize()
        self.assertTrue(set(results.PLAN[1:]).isdisjoint(value['steps'][0]))
        self.assertEqual(self.collect(value), 0)
        folder = self.root / 'projects/example'
        with closing(history.db(folder)) as database:
            row = dict(database.execute('SELECT * FROM runs').fetchone())
            self.assertEqual((row['id'], row['commit'], row['kind'], row['cpu_s']), (self.identifier, self.job['commit'], 'gate', 1.5))
            self.assertEqual(database.execute("SELECT status,ms FROM results JOIN tests ON tests.id=results.test WHERE name='example::test'").fetchone()[:], ('passed', 2))
            self.assertEqual(len(database.execute("SELECT excerpt FROM results JOIN tests ON tests.id=results.test WHERE name='phase/fmt.all'").fetchone()[0]), 4096)
        self.assertEqual(history.find(folder, 'key=?', self.job['key'])['id'], self.identifier)
        self.assertEqual(history.run(folder, self.identifier)['exit'], 0)
        self.assertIn('[31mreport', value['report'])
        self.assertNotIn('\x1b', value['report'])

    def test_profiling_failure_keeps_correctness_accounting(self):
        self.job['steps'].append(dict(name='profiling', kind='bench', exclusive=True, argv=[],
                                      queued_at=4, started_at=5, ended_at=7, exit=1, phases=[]))
        self.job['exit'] = 1
        value = self.normalize()
        self.assertEqual(self.collect(value), 1)
        self.assertEqual((value['steps'][0]['exit'], value['steps'][0]['ended_at'], value['steps'][0]['cpu_s']), (0, 4, 1.5))
        with closing(history.db(self.root / 'projects/example')) as database:
            self.assertEqual(database.execute('SELECT exit FROM runs').fetchone()[0], 1)
            phases = dict(database.execute('SELECT name,status FROM results JOIN tests ON tests.id=results.test'))
            self.assertEqual((phases['phase/correctness'], phases['phase/profiling']), ('passed', 'failed'))

    def test_cancelled_admission_has_no_fabricated_command_exit(self):
        self.job.update(exit=137, steps=[self.job['steps'][0] | dict(exit=None, started_at=None, phases=[])])
        for path in self.step.iterdir():
            path.unlink()
        value = self.normalize()
        self.assertEqual(self.collect(value), 137)
        self.assertIsNone(value['steps'][0]['exit'])
        self.assertIsNone(value['steps'][0]['cpu_s'])
        self.assertIn('ended before start', value['report'])
        self.assertRaises(ValueError, results.validate, value | dict(exit=0), self.job, self.identifier, 0)

    def test_canonical_spec_types_arguments_and_budget(self):
        job = dict(self.job, queued_at='1.0', steps=[{key: self.job['steps'][0][key] for key in results.PLAN}])
        del job['exit']
        params = dict(project='example', revision='d' * 40, job=job)
        name, decoded = results.spec(params, 'd' * 40, 'example')
        self.assertEqual((name, decoded['queued_at'], job['queued_at']), ('example', 1.0, '1.0'))
        self.assertRaises(ValueError, results.spec, params, 'd' * 40, 'another-project')
        for field, value in (('queued_at', 1), ('queued_at', 'nan'), ('rerun', 0), ('branch', 'x' * 65537)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                results.spec(params | dict(job=job | {field: value}), 'd' * 40)
        job.update(kind='run', key='')
        job['steps'][0]['kind'] = 'run'
        for argv in ([], [False], ['nul\0value'], ['x' * 65537], ['x'] * 4097):
            job['steps'][0]['argv'] = argv
            self.assertRaises(ValueError, results.spec, params, 'd' * 40)
        job['steps'][0]['argv'] = ['x' * 65536]
        self.assertEqual(results.spec(params, 'd' * 40)[1]['steps'][0]['argv'], job['steps'][0]['argv'])
        with patch.object(results, 'HISTORY_LIMIT', len(json.dumps(params).encode()) - 1), self.assertRaisesRegex(ValueError, 'metadata limit'):
            results.spec(params, 'd' * 40)

    def test_raw_links_types_paths_and_bounds_are_rejected(self):
        report = self.out / 'report.txt'
        donor = self.root / 'private'
        donor.write_text('private')
        for create in (lambda: report.symlink_to(donor), lambda: os.link(donor, report),
                       lambda: os.mkfifo(report), lambda: report.mkdir()):
            with self.subTest(create=create), self.assertRaises((OSError, ValueError)):
                create()
                self.normalize()
            report.rmdir() if report.is_dir() else report.unlink()
        report.write_text('executable')
        report.chmod(0o700)
        self.assertRaises(ValueError, self.normalize)
        report.unlink()
        self.assertRaises(ValueError, results.raw, self.artifacts, self.artifacts / '../private')
        for limit in ('OUTPUT_LIMIT', 'ARCHIVE_LIMIT', 'FILE_LIMIT'):
            with self.subTest(limit=limit), patch.object(results, limit, 1), self.assertRaises(ValueError):
                self.normalize()
        (self.out / 'bad\x1bname').write_text('unsafe')
        self.assertRaises(ValueError, self.normalize)
        (self.out / 'link').symlink_to(donor)
        (self.out / 'tool').write_text('executable')
        (self.out / 'tool').chmod(0o700)
        (self.out / '-C').write_text('literal')
        archive = self.root / 'logs.tar.zst'
        subprocess.run(['bash', str(Path(__file__).with_name('archive.sh')), str(self.artifacts), str(archive)], check=True)
        names = subprocess.check_output(['tar', '-I', 'zstd', '-tf', str(archive)], text=True).splitlines()
        self.assertIn('./out/-C', names)
        self.assertTrue({'./out/link', './out/tool'}.isdisjoint(names))

    def test_junit_phases_and_normalized_rows_keep_all_numeric_guards(self):
        path = self.step / 'junit.xml'
        cases = [self.xml(value) for value in ('-1', 'inf', 'nan', '1e308')]
        cases += ['<!DOCTYPE testsuite []>' + self.xml(), '<broken', self.xml().replace('name="test"', 'name="' + 'x' * 4097 + '"')]
        for text in cases:
            path.write_text(text)
            os.utime(path, (0, 0))
            with self.subTest(text=text[:40]), self.assertRaises((ValueError, ET.ParseError)):
                self.normalize()
        path.unlink()
        for text in ('../escape\t1\t1\n', 'test\tnan\t0\n'):
            (self.out / 'phases.tsv').write_text(text)
            self.assertRaises(ValueError, self.normalize)
        (self.out / 'phases.tsv').unlink()
        value = self.normalize()
        for field, replacement in (('job_id', 'another'), ('commit', 'e' * 40), ('slot', True), ('exit', True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                results.validate(value | {field: replacement}, self.job, self.identifier, 0)
        for field, replacement in (('ended_at', float('nan')), ('started_at', 5), ('cpu_s', -1), ('peak_bytes', 2.5), ('cpu_s', 10**400), ('exclusive', 0)):
            forged = copy.deepcopy(value)
            forged['steps'][0][field] = replacement
            self.assertRaises(ValueError, results.validate, forged, self.job, self.identifier, 0)
        with patch.object(results, 'TEST_LIMIT', 0), self.assertRaises(ValueError):
            results.validate(value, self.job, self.identifier, 0)
        forged = value | dict(tests=value['tests'] * 2)
        self.assertRaises(ValueError, results.validate, forged, self.job, self.identifier, 0)

    def test_stream_limit_and_terminal_controls(self):
        raw, output = b'hello\x1b[31m\x07\r\bworld\n', io.BytesIO()
        from unittest.mock import MagicMock
        queue = MagicMock()
        queue.__enter__.return_value = queue
        queue.logs.return_value = dict(entries=[dict(offset=0, address='sha256:' + 'a' * 64, size=len(raw))], next_offset=len(raw), done=True)
        with patch.object(control, 'client', return_value=queue), patch.object(results, 'cas', side_effect=lambda root, operation, address, path, **options: path.write_bytes(raw)), patch('sys.stdout', types.SimpleNamespace(buffer=output)):
            control.logs(self.root, 'task')
            with patch.object(results, 'OUTPUT_LIMIT', 2), self.assertRaises(ValueError):
                control.logs(self.root, 'task')
        self.assertEqual(output.getvalue(), raw)
        filtered = io.StringIO()
        safe_output.stream(io.BufferedReader(io.BytesIO(raw + '\u202e'.encode())), filtered)
        self.assertEqual(filtered.getvalue(), 'hello[31mworld\n')


if __name__ == '__main__':
    unittest.main()
