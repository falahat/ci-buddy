"""CI policy at native submission, source and ordered-step boundaries."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import control
import execute

ID = 'j_' + '0' * 26


class NativeCI(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / 'bin').mkdir()
        (self.root / 'containers').mkdir()
        (self.root / 'bin/revision').write_text('a' * 40)
        (self.root / 'host.conf').write_text('jobs=1\nslots=1\nmemory="1g"\ncpus="1"\nmin_free_gb=500\nslot_max_gb=5\nfetch_hosts=[]\n')
        self.job = dict(kind='gate', key='a' * 64, rerun=False, queued_at=1, branch='topic', submitter='owner',
                        commit='b' * 40, base='c' * 40, tree='d' * 40,
                        steps=[dict(name='correctness', kind='gate', exclusive=False, argv=[]),
                               dict(name='profiling', kind='bench', exclusive=True, argv=[])])
        self.view = dict(job_id=ID, job_key='example/' + 'a' * 64, lane='b-' + 'a' * 12,
                         invocation_address='sha256:invocation', state='RUNNING', cancel_requested=False)
        self.spec = dict(kind='invocation/v0', op='command', op_version=1,
                         params=dict(project='example', revision='a' * 40, job=dict(self.job, queued_at='1')),
                         inputs={'source': {'address': 'sha256:source'}})
        self.queue = MagicMock()
        self.queue.__enter__.return_value = self.queue
        self.enterContext(patch.object(control, 'client', return_value=self.queue))
        self.enterContext(patch.object(control, 'blob', side_effect=lambda root, address=None, value=None:
            'sha256:new' if value is not None else self.spec))

    def test_lost_reply_uses_exact_submission_lookup_without_replay(self):
        self.queue.enqueue.side_effect = ConnectionError('reply lost')
        self.queue.lookup.return_value = self.view
        with patch.object(control.data, 'git'), patch.object(control.results, 'cas', return_value='sha256:source'):
            self.assertEqual(control.enqueue(self.root, 'example', '123', self.job), self.view)
        self.assertEqual(self.queue.enqueue.call_count, 1)
        self.queue.lookup.assert_called_once_with('ci/example/123')
        self.queue.jobs.assert_not_called()

    def test_native_identity_source_revision_and_lane_are_bound(self):
        self.assertEqual(control.context(self.root, self.view, 'example'), ('example', self.job))
        for key, value in (('job_id', '123'), ('job_key', 'other/key'), ('lane', 'x-' + 'a' * 12)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                control.context(self.root, self.view | {key: value}, 'example')
        self.spec['params']['revision'] = 'e' * 40
        with self.assertRaises(ValueError):
            control.context(self.root, self.view, 'example')

    def test_cancel_never_targets_another_project_or_confirms_failed_cleanup(self):
        self.queue.job.return_value = self.view
        argv = ['control.py', str(self.root), 'other', 'cancel', ID]
        with patch.dict(control.os.environ), patch('sys.argv', argv), self.assertRaises(ValueError):
            control.main()
        self.queue.cancel.assert_not_called()
        self.view['state'] = 'FAILED'
        self.queue.wait.return_value = self.view
        argv[2] = 'example'
        with patch.dict(control.os.environ), patch('sys.argv', argv), patch.object(control, 'settle'), self.assertRaisesRegex(ValueError, 'unconfirmed'):
            control.main()
        self.queue.cancel.assert_not_called()

    def test_status_uses_native_progress_without_workload_log_parsing(self):
        self.view['progress'] = dict(phase='profiling', correctness_exit=0)
        self.queue.jobs.return_value = dict(jobs=[self.view])
        with patch.dict(control.os.environ), patch('sys.argv', ['control.py', str(self.root), 'example', 'status']), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(control.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['jobs'][0]['progress'], self.view['progress'])
        self.queue.logs.assert_not_called()

    def test_unknown_execution_does_not_invent_a_command_exit(self):
        self.view.update(state='FAILED', reason='worker disconnected')
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(control.settle(self.root, self.view, 'example'), 1)
        row = control.history.run(self.root / 'projects/example', ID)
        self.assertIsNone(row['exit'])
        self.assertIsNone(row['ended_at'])

    def gate(self, first):
        work = self.root / 'containers'
        path = work / 'job.json'
        path.write_text(json.dumps(self.job))
        progress, output = [], io.StringIO()
        def step(root, project, identifier, work, job, index, slot, descriptor):
            progress.append(json.loads((work / 'invoke-progress.json').read_text()))
            if index:
                self.assertIn('correctness_exit=0', output.getvalue())
            if index or first:
                raise control.subprocess.CalledProcessError(1 if index else first, ['fixture-command'])
        slot = MagicMock()
        slot.__enter__.return_value = work, 3
        with patch('sys.stdout', output), patch.object(execute, 'CANCELLED', False), patch.object(execute, 'KILLED', False), \
                patch.object(execute, 'choose_slot', return_value=slot), patch.object(execute, 'run_step', side_effect=step), \
                patch.object(execute.time, 'time', side_effect=[4, 5, 7]):
            job = execute.pipeline(self.root, 'example', ID, work, self.job)
        return job, progress

    def test_correctness_is_visible_before_failed_profiling(self):
        job, progress = self.gate(0)
        self.assertEqual(progress, [dict(phase='correctness'), dict(phase='profiling', correctness_exit=0)])
        self.assertEqual((job['steps'][0]['exit'], job['steps'][0]['ended_at'], job['exit']), (0, 4, 1))

    def test_red_correctness_skips_all_later_steps(self):
        job, progress = self.gate(2)
        self.assertEqual(progress, [dict(phase='correctness')])
        self.assertEqual(job['exit'], 2)
        self.assertNotIn('exit', job['steps'][1])


if __name__ == '__main__':
    unittest.main()
