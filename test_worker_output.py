"""Small adversarial checks for the actual worker-return validators."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

# Return parsing is platform-independent; Unix lock routing is not used here.
if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))
import workers
import safe_output


class WorkerOutput(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job = dict(kind='test', key='key', tree='a' * 40, commit='b' * 40,
                        branch='topic', submitter='owner', argv=[], queued_at=1)
        row = dict.fromkeys(workers.RUN_COLUMNS)
        row.update({key: self.job[key] for key in ('kind', 'key', 'tree', 'commit', 'branch', 'submitter')})
        row.update(id=7, argv='[]', queued_at=2, started_at=3, ended_at=4, exit=0,
                   cpu_s=1.5, peak_bytes=1024, slot=1)
        self.payload = [row, [['example::test', 'passed', 2, None]]]

    def archive(self, entries):
        path = self.root / 'return.tar'
        with tarfile.open(path, 'w') as archive:
            for name, mode, kind, size in entries:
                item = tarfile.TarInfo(name)
                item.mode, item.type, item.size = mode, kind, size
                archive.addfile(item, io.BytesIO(b'x' * size) if kind == tarfile.REGTYPE else None)
        destination = self.root / 'results'
        destination.mkdir(exist_ok=True)
        workers.unpack_results(path, destination)
        return destination

    def test_protocol_only_and_no_metadata_overwrite(self):
        destination = self.archive([(name, 0o600, tarfile.REGTYPE, 1)
                                    for name in ('report.txt', 'job.json', 'task', 'worker-exit', 'tool.py')])
        self.assertEqual([p.name for p in destination.iterdir()], ['report.txt'])

    def test_unsafe_archive_entries(self):
        bad = [('../escape', 0o600, tarfile.REGTYPE, 1),
               ('/tmp/escape', 0o600, tarfile.REGTYPE, 1),
               ('./report.txt', 0o600, tarfile.REGTYPE, 1),
               ('report.txt', 0o700, tarfile.REGTYPE, 1),
               ('report.txt', 0o600, tarfile.SYMTYPE, 0),
               ('report.txt', 0o600, tarfile.LNKTYPE, 0),
               ('report.txt', 0o600, tarfile.FIFOTYPE, 0)]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.archive([entry])
        with self.assertRaises(ValueError):
            self.archive([('extra.txt', 0o600, tarfile.REGTYPE, 1)] * 2)
        with mock.patch.object(workers, 'OUTPUT_LIMIT', 1), self.assertRaises(ValueError):
            self.archive([('extra.txt', 0o600, tarfile.REGTYPE, 2)])

    def test_canonical_identity_and_rejected_history(self):
        row, _ = workers.validate_history(copy.deepcopy(self.payload), self.job, '7', 0)
        self.assertEqual(row['queued_at'], self.job['queued_at'])
        changes = [('id', 8), ('id', True), ('commit', 'c' * 40), ('tree', 'c' * 40),
                   ('branch', 'other'), ('argv', '["other"]'), ('exit', 1),
                   ('cpu_s', float('nan')), ('slot', 0), ('peak_bytes', 2.5)]
        for key, value in changes:
            payload = copy.deepcopy(self.payload)
            payload[0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                workers.validate_history(payload, self.job, '7', 0)
        payload = copy.deepcopy(self.payload)
        payload[0]['injected_column'] = 'bad'
        with self.assertRaises(ValueError):
            workers.validate_history(payload, self.job, '7', 0)
        for result in [['phase/../escape', 'failed', 1, None], ['test', 'passed', -1, None],
                       ['test', 'passed', float('inf'), None], ['test', 'unknown', 1, None],
                       ['test', 'failed', 1, 'x' * 4097]]:
            with self.subTest(result=result), self.assertRaises(ValueError):
                workers.validate_history([self.payload[0], [result]], self.job, '7', 0)
        with self.assertRaises(ValueError):
            workers.validate_history([self.payload[0], self.payload[1] * 2], self.job, '7', 0)

    def test_stream_limit_and_terminal_controls(self):
        raw = b'hello\x1b[31m\x07\r\bworld\n'
        output = io.BytesIO()
        command = [sys.executable, '-c', 'import sys;sys.stdout.buffer.write(' + repr(raw) + ')']
        with mock.patch('sys.stdout', new_callable=io.StringIO) as displayed:
            self.assertEqual(workers.receive(command, output, 128, display=True), 0)
            self.assertEqual(displayed.getvalue(), 'hello[31mworld\n')
        self.assertEqual(output.getvalue(), raw)
        with self.assertRaises(ValueError):
            workers.receive(command, io.BytesIO(), 2)
        filtered = io.StringIO()
        safe_output.stream(io.BufferedReader(io.BytesIO(raw + '\u202e'.encode())), filtered)
        self.assertEqual(filtered.getvalue(), 'hello[31mworld\n')


if __name__ == '__main__':
    unittest.main()
