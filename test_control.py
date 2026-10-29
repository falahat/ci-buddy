"""Native submission recovery never replays execution or cancels a dedup twin."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import control
import execute


class Fixture(unittest.TestCase):
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


class Recovery(Fixture):
    def setUp(self):
        super().setUp()
        self.queue = MagicMock()
        self.queue.__enter__.return_value = self.queue
        self.enterContext(patch.object(control, 'client', return_value=self.queue))
        self.specs, self.rows = {}, {}
        self.queue.job.side_effect = lambda task: {key: value for key, value in self.rows[task].items() if key != 'lane'}
        self.queue.jobs.side_effect = lambda **args: dict(jobs=list(self.rows.values()))
        self.enterContext(patch.object(control, 'invocation', side_effect=
            lambda root, address=None, value=None: 'sha256:new' if value is not None else self.specs[address]))
        self.enterContext(patch.object(control.results, 'cas', return_value='sha256:job'))
        self.ingest = self.enterContext(patch.object(control.results, 'collect', return_value=0))
        self.stderr = self.enterContext(patch('sys.stderr', new_callable=io.StringIO))

    def row(self, task='core', run='101'):
        job = json.loads((self.out / 'job.json').read_text())
        address = 'sha256:' + task
        self.specs[address] = dict(kind='invocation/v0', op='command', op_version=1,
            params=dict(project='example', run=run, revision='a' * 40),
            inputs={'source': {'address': 'sha256:source'}})
        row = dict(job_id=task, job_key=control.job_key('example', run, job),
                   lane=control.lane('a' * 40),
                   invocation_address=address, state='PERSISTING', cancel_requested=False)
        self.rows[task] = row
        return row

    def completion(self, row):
        return dict(job_id=row['job_id'], job_key=row['job_key'], lane=row['lane'],
                    action_result=dict(kind='action_result/v0', invocation_address=row['invocation_address'],
                                       runner={'node': 'node-a'}, metrics={'exit': 0}))

    def test_lost_core_enqueue_reply_uses_exact_run_across_pages(self):
        other, own = self.row('twin', run='102'), self.row()
        self.queue.enqueue.side_effect = ConnectionError('accepted reply lost')
        self.queue.jobs.side_effect = [dict(jobs=[other], next_page=100), dict(jobs=[own])]
        self.assertEqual(control.enqueue(self.root, 'example', '101', source='sha256:source'), ('core', '101'))
        self.assertEqual(self.queue.enqueue.call_count, 1)
        self.assertEqual([call.kwargs['page'] for call in self.queue.jobs.call_args_list], [None, 100])
        self.queue.jobs.side_effect = None
        self.queue.jobs.return_value = dict(jobs=[own, dict(own, job_id='duplicate')])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            control.find_task(self.root, self.queue, 'example', '101', self.job)

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

    def test_running_status_uses_native_progress(self):
        row = self.row()
        row.update(state='RUNNING', progress=dict(phase='profiling', correctness_exit=0))
        with patch.dict(control.os.environ), patch('sys.argv', ['control.py', str(self.root), 'example', 'status']), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(control.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['jobs'][0]['progress'], row['progress'])
        self.queue.logs.assert_not_called()

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


class Pipeline(Fixture):
    def gate(self, core_exit, bench_exit):
        work = self.root / 'containers'
        phases, output = [], io.StringIO()
        self.job['bench_required'] = True
        def phase(root, project, run, out, prefix=''):
            phases.append(json.loads((work / 'invoke-progress.json').read_text()))
            job = json.loads((out / 'job.json').read_text())
            if prefix:
                self.assertIn('correctness_exit=0', output.getvalue())
                job.update(bench_exit=bench_exit)
            else:
                job.update(ended_at=4, exit=core_exit)
            (out / 'job.json').write_text(json.dumps(job))
            return bench_exit if prefix else core_exit
        with patch('sys.stdout', output), patch.object(execute, 'CANCELLED', False), \
                patch.object(execute, 'run_phase', side_effect=phase):
            status = execute.pipeline(self.root, 'example', '101', self.out, work, self.job)
        return phases, json.loads((work / 'invoke-progress.json').read_text()), status, json.loads((self.out / 'job.json').read_text())

    def test_correctness_precedes_failed_profiling(self):
        phases, progress, status, job = self.gate(0, 1)
        self.assertEqual(phases, [dict(phase='correctness'), dict(phase='profiling', correctness_exit=0)])
        self.assertEqual(progress, dict(phase='complete', correctness_exit=0, exit=1))
        self.assertEqual(status, 1)
        self.assertEqual((job['exit'], job['ended_at'], job['bench_exit'], job['overall_exit']), (0, 4, 1, 1))

    def test_red_correctness_skips_profiling(self):
        phases, progress, status, job = self.gate(2, 0)
        self.assertEqual(phases, [dict(phase='correctness')])
        self.assertEqual(progress, dict(phase='complete', correctness_exit=2, exit=2))
        self.assertEqual(status, 2)
        self.assertNotIn('bench_exit', job)


if __name__ == '__main__':
    unittest.main()
