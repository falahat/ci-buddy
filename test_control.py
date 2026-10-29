"""Native submission recovery never replays execution or cancels a dedup twin."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import control


class Recovery(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ('bin', 'containers', 'projects/example/runs/101'):
            (self.root / name).mkdir(parents=True)
        (self.root / 'bin/revision').write_text('a' * 40)
        self.out = self.root / 'projects/example/runs/101'
        self.job = dict(kind='gate', key='same-tree', rerun=False, exclusive=False,
                        bench_required=False, commit='b' * 40, base='c' * 40, tree='d' * 40,
                        branch='fixture', submitter='owner', argv=[], queued_at=1)
        control.data.output(self.out, self.job)
        self.queue = MagicMock()
        self.queue.__enter__.return_value = self.queue
        self.enterContext(patch.object(control, 'client', return_value=self.queue))
        self.specs, self.rows = {}, {}
        self.queue.job.side_effect = lambda task: {key: value for key, value in self.rows[task].items() if key != 'lane'}
        self.queue.jobs.side_effect = lambda **args: dict(jobs=list(self.rows.values()))
        self.enterContext(patch.object(control, 'invocation', side_effect=self.invocation))
        self.enterContext(patch.object(control.results, 'cas', return_value='sha256:job'))
        self.ingest = self.enterContext(patch.object(control.results, 'collect', side_effect=self.ingest_result))
        self.stderr = self.enterContext(patch('sys.stderr', new_callable=io.StringIO))

    def invocation(self, root, address=None, value=None):
        if value is not None:
            self.specs['sha256:new'] = value
            return 'sha256:new'
        return copy.deepcopy(self.specs[address])

    def row(self, task='core', phase='core', run='101', node='node-a'):
        job = json.loads((self.out / 'job.json').read_text())
        address = 'sha256:' + task
        self.specs[address] = dict(kind='invocation/v0', op='command', op_version=1,
            params=dict(project='example', run=run, phase=phase, revision='a' * 40),
            inputs={'source': {'address': 'sha256:source'}})
        row = dict(job_id=task, job_key=control.job_key('example', run, job, phase),
                   lane=control.lane('a' * 40, phase == 'bench', node if phase == 'bench' else None),
                   invocation_address=address, state='PERSISTING', cancel_requested=False)
        self.rows[task] = row
        return row

    def completion(self, row):
        return dict(job_id=row['job_id'], job_key=row['job_key'], lane=row['lane'],
                    action_result=dict(kind='action_result/v0', invocation_address=row['invocation_address'],
                                       runner={'node': 'node-a'}, metrics={'exit': 0}))

    def ingest_result(self, root, project, run, action, phase, cancelled):
        job = json.loads((self.out / 'job.json').read_text())
        prefix = 'bench_' if phase == 'bench' else ''
        job.update({prefix + 'exit': 0, prefix + 'node': action['runner']['node']})
        control.data.write_file(self.out / 'job.json', json.dumps(job))
        return 0

    def benchmark_intent(self):
        self.job.update(bench_required=True, bench_submission_started=True, bench_queued_at=2, node='node-a')
        control.data.write_file(self.out / 'job.json', json.dumps(self.job))

    def test_lost_core_enqueue_reply_uses_exact_run_across_pages(self):
        other, own = self.row('twin', run='102'), self.row()
        self.queue.enqueue.side_effect = ConnectionError('accepted reply lost')
        self.queue.jobs.side_effect = [dict(jobs=[other], next_page=100), dict(jobs=[own])]
        self.assertEqual(control.enqueue(self.root, 'example', '101', source='sha256:source'), ('core', '101'))
        self.assertEqual(self.queue.enqueue.call_count, 1)
        self.assertEqual([call.kwargs['page'] for call in self.queue.jobs.call_args_list], [None, 100])

    def test_core_completion_adopts_empty_task_and_rejects_invalid_identity(self):
        bad, good = self.row('bad'), self.row()
        self.specs[bad['invocation_address']]['params']['revision'] = 'e' * 40
        self.queue.completions.return_value = iter(map(self.completion, (bad, good)))
        with patch.object(control, 'enqueue') as enqueue:
            control.collect(self.root)
        enqueue.assert_not_called()
        self.assertEqual((self.out / 'task').read_text(), 'core')
        self.assertEqual((self.out / 'exit').read_text(), '0')
        self.assertEqual([call.args[:2] for call in self.queue.ack.call_args_list], [('bad', 'rejected'), ('core', 'persisted')])
        self.assertEqual(self.ingest.call_count, 1)

    def test_submit_dedup_publishes_original_task_before_logs(self):
        def make_job(folder, run, *arguments):
            control.data.output(folder / 'runs' / run, self.job)
        def logs(root, task):
            self.assertEqual(task, 'core')
            self.assertEqual((self.out / 'task').read_text(), task)
        argv = ['control.py', str(self.root), 'example', 'submit', '102',
                'gate', 'b' * 40, 'c' * 40, 'fixture', 'owner']
        with patch.dict(control.os.environ), patch('sys.argv', argv), patch('sys.stdout', new_callable=io.StringIO), \
                patch.object(control.data, 'make_job', side_effect=make_job), \
                patch.object(control.data, 'reuse_finished', return_value=None), \
                patch.object(control.data, 'discard'), \
                patch.object(control, 'enqueue', return_value=('core', '101')), \
                patch.object(control, 'logs', side_effect=logs) as follow, \
                patch.object(control, 'wait_run', return_value=0):
            self.assertEqual(control.main(), 0)
        follow.assert_called_once()
        self.assertFalse(self.out.with_name('102').exists())

    def test_accepted_benchmark_is_adopted_without_enqueue(self):
        self.benchmark_intent()
        core, bench = self.row(), self.row('bench', phase='bench')
        self.queue.completions.return_value = iter([self.completion(core)])
        with patch.object(control, 'enqueue') as enqueue:
            control.collect(self.root)
        enqueue.assert_not_called()
        self.assertEqual((self.out / 'bench-task').read_text(), bench['job_id'])
        self.assertEqual((self.out / 'exit').read_text(), '0')
        self.assertEqual(self.queue.jobs.call_args.kwargs['filter'], {'job_key_prefix': 'example/101/bench'})
        bench['lane'] = control.lane('a' * 40, True, 'another-node')
        with self.assertRaisesRegex(ValueError, 'canonical run'):
            control.find_task(self.root, self.queue, 'example', '101', 'bench', self.job)

    def test_missing_or_ambiguous_benchmark_does_not_poison_later_completion(self):
        self.benchmark_intent()
        core, bench = self.row(), self.row('bench', phase='bench')
        self.queue.completions.return_value = iter(map(self.completion, (core, bench)))
        self.queue.jobs.return_value = dict(jobs=[])
        self.queue.jobs.side_effect = None
        with patch.object(control, 'enqueue') as enqueue:
            control.collect(self.root)
        enqueue.assert_not_called()
        self.assertEqual((self.out / 'exit').read_text(), '137')
        self.assertEqual((self.out / 'bench-task').read_text(), 'bench')
        self.assertEqual((self.out / 'bench-exit').read_text(), '0')
        self.assertEqual([call.args[:2] for call in self.queue.ack.call_args_list], [('core', 'rejected'), ('bench', 'persisted')])
        self.queue.jobs.return_value = dict(jobs=[bench, dict(bench, job_id='duplicate')])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            control.find_task(self.root, self.queue, 'example', '101', 'bench', self.job)

    def test_cancel_failed_or_transport_failure_is_unconfirmed(self):
        row = self.row()
        (self.out / 'task').write_text(row['job_id'])
        row['state'] = 'FAILED'
        self.queue.wait.return_value = row
        self.assertEqual(control.cancel(self.root, 'example', '101'), 1)
        self.assertEqual((self.out / 'exit').read_text(), '137')
        self.queue.wait.side_effect = ConnectionError('queue unavailable')
        self.assertEqual(control.cancel(self.root, 'example', '101'), 1)
        self.queue.cancel.assert_not_called()
        self.assertIn('cancellation unconfirmed', self.stderr.getvalue())
        self.queue.wait.side_effect = None
        row['state'] = 'CANCELLED'
        self.assertEqual(control.cancel(self.root, 'example', '101'), 0)

    def test_cancel_never_adopts_or_cancels_a_different_run_or_overwrites_task(self):
        self.row('twin', run='102')
        self.assertEqual(control.cancel(self.root, 'example', '101'), 1)
        self.queue.cancel.assert_not_called()
        self.queue.wait.assert_not_called()
        self.assertEqual((self.out / 'task').read_text(), '')
        self.assertEqual((self.out / 'exit').read_text(), '137')
        (self.out / 'task').write_text('existing')
        self.queue.completions.return_value = iter([self.completion(self.row())])
        control.collect(self.root)
        self.assertEqual((self.out / 'task').read_text(), 'existing')
        self.ingest.assert_not_called()
        self.assertEqual(self.queue.ack.call_args.args[:2], ('core', 'rejected'))


if __name__ == '__main__':
    unittest.main()
