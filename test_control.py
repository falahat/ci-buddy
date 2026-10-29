"""CI policy at native submission, source and cancellation boundaries."""
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
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
        self.job = dict(key='a' * 64, rerun=False, queued_at=1, branch='topic', submitter='owner',
                        commit='b' * 40, base='c' * 40, tree='d' * 40,
                        steps=[dict(kind='gate', exclusive=False, argv=[]),
                               dict(kind='bench', exclusive=True, argv=[])])
        self.view = dict(job_id=ID, job_key='example/' + 'a' * 64, lane='ci-' + 'a' * 12,
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
        for key, value in (('job_id', '123'), ('job_key', 'other/key'), ('lane', 'ci-' + 'e' * 12)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                control.context(self.root, self.view | {key: value}, 'example')
        self.spec['params']['revision'] = 'e' * 40
        self.view['lane'] = 'ci-' + 'e' * 12
        self.assertEqual(control.context(self.root, self.view, 'example'), ('example', self.job))
        self.assertRaises(ValueError, control.settle, self.root, self.view, 'example')
        self.assertRaises(ValueError, control.results.spec, self.spec['params'], 'a' * 40)
        self.assertRaises(ValueError, control.results.spec, self.spec['params'] | dict(revision=1), 1)

    def test_cancel_never_targets_another_project_or_confirms_failed_cleanup(self):
        self.queue.job.return_value = self.view
        argv = ['control.py', str(self.root), 'other', 'cancel', ID]
        with patch('sys.argv', argv), self.assertRaises(ValueError):
            control.main()
        self.queue.cancel.assert_not_called()
        self.view['state'] = 'FAILED'
        self.queue.wait.return_value = self.view
        argv[2] = 'example'
        with patch('sys.argv', argv), self.assertRaisesRegex(ValueError, 'unconfirmed'):
            control.main()
        self.queue.cancel.assert_not_called()

    def test_unknown_execution_does_not_invent_a_command_exit(self):
        self.view.update(state='FAILED', reason='worker disconnected')
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(control.settle(self.root, self.view, 'example'), 1)
        row = control.history.run(self.root / 'projects/example', ID)
        self.assertIsNone(row['exit'])
        self.assertIsNone(row['ended_at'])

    def test_signal_handler_never_reaps_the_owned_child(self):
        previous = signal.signal(signal.SIGTERM, execute.cancelled)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], start_new_session=True) as child:
            with patch.multiple(execute, CHILD=child, CONTAINER=None, CANCELLED=False, KILLED=False), \
                    patch.object(child, 'wait', wraps=child.wait) as wait:
                os.kill(os.getpid(), signal.SIGTERM)
                wait.assert_not_called()
                self.assertTrue(execute.CANCELLED and execute.KILLED)
                self.assertEqual(child.wait(), -signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
