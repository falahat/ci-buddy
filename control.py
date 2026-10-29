"""Application state over the native dispatch queue, CAS and completion stream."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET

import data
import history
import results
from execute import identity


def client(root):
    from dispatch_client import DispatchClient
    return DispatchClient('unix:' + str(root / 'dispatch/app.sock'), retry_requests=False)


def lane(revision, exclusive=False, node=None):
    value = ('exclusive_' if exclusive else 'build_') + revision[:12]
    return value + ('_' + hashlib.sha256(node.encode()).hexdigest()[:12] if node else '')


def invocation(root, address=None, value=None):
    with tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
        path = Path(directory) / 'invocation.json'
        if value is not None:
            path.write_text(json.dumps(value))
            return results.cas(root, 'put', path)
        results.cas(root, 'get', address, path, limit=results.HISTORY_LIMIT)
        return json.loads(data.read_file(path))


def enqueue(root, project, run, phase='core', node=None, source=None):
    folder = root / 'projects' / project
    out = folder / 'runs' / run
    job = json.loads(data.read_file(out / 'job.json'))
    revision = data.read_file(root / 'bin/revision').strip()
    if source is None:
        with tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
            bundle = Path(directory) / 'source.bundle'
            try:
                for name, commit in (('source', job['commit']), ('base', job['base'])):
                    data.git(folder / 'git', 'update-ref', 'refs/ci-buddy/' + name, commit)
                data.git(folder / 'git', 'bundle', 'create', str(bundle), 'refs/ci-buddy/source', 'refs/ci-buddy/base')
                source = results.cas(root, 'put', bundle)
            finally:
                for name in ('source', 'base'):
                    data.git(folder / 'git', 'update-ref', '-d', 'refs/ci-buddy/' + name)
                data.discard(folder / 'git', run)
    inputs = dict(source=dict(address=source, kind='blob'),
                  job=dict(address=results.cas(root, 'put', out / 'job.json'), kind='blob'))
    address = invocation(root, value=dict(kind='invocation/v0', op='command', op_version=1,
                         params=dict(project=project, run=run, phase=phase, revision=revision), inputs=inputs))
    key = project + '/' + (job['key'] if job['key'] and not job['rerun'] else run)
    if phase == 'bench':
        key = project + '/' + run + '/bench'
    with client(root) as queue:
        answer = queue.enqueue([dict(job_key=key,
                    lane=lane(revision, phase == 'bench' or job['exclusive'], node),
                    invocation_address=address, policy=dict(max_attempts=1))])[0]
        if answer['created']:
            return answer['job_id'], run
        prior = invocation(root, address=queue.job(answer['job_id'])['invocation_address'])['params']
        identity(prior['project'], prior['run'])
        if prior['project'] != project or prior['phase'] != phase:
            raise ValueError('native deduplication returned another execution')
        return answer['job_id'], prior['run']


def recover(out, prefix, reason):
    job = json.loads(data.read_file(out / 'job.json'))
    job.update({prefix.replace('-', '_') + 'exit': 137, prefix.replace('-', '_') + 'ended_at': time.time()})
    data.write_file(out / 'job.json', json.dumps(job))
    data.report(out, prefix)
    with data.open_file(out / 'report.txt', 'a') as report:
        report.write('native execution incomplete: ' + reason + '\n')
    data.record(out)
    data.write_file(out / (prefix + 'exit'), '137')


def wait_run(root, project, run, prefix=''):
    out = root / 'projects' / project / 'runs' / run
    task = data.read_file(out / (prefix + 'task')).strip()
    if task:
        with client(root) as queue:
            view = queue.wait(task)
        marker = data.read_file(out / (prefix + 'exit')).strip()
        if not marker or view['state'] != 'SUCCEEDED' and marker == '0':
            with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                recover(out, prefix, view['state'])
    return int(data.read_file(out / (prefix + 'exit')))


def logs(root, task, wait=True):
    offset = 0
    with client(root) as queue, tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
        path = Path(directory) / 'chunk'
        while True:
            response = queue.logs(task, after=offset, wait=wait)
            for entry in response['entries']:
                if (type(entry['offset']) is not int or entry['offset'] != offset
                        or type(entry['size']) is not int or not 0 <= entry['size'] <= 1024 * 1024
                        or offset + entry['size'] > results.OUTPUT_LIMIT):
                    raise ValueError('invalid native log chunk')
                results.cas(root, 'get', entry['address'], path, limit=1024 * 1024)
                block = path.read_bytes()
                if len(block) != entry['size']:
                    raise ValueError('native log chunk size differs')
                sys.stdout.buffer.write(block)
                sys.stdout.buffer.flush()
                offset += len(block)
                path.unlink()
            if response['next_offset'] != offset:
                raise ValueError('native log cursor differs')
            if response['done'] or not wait and not response['entries']:
                return


def collect(root):
    with client(root) as queue:
        for completion in queue.completions():
            task = completion['job_id']
            view = queue.job(task)
            action = completion['action_result']
            if action['kind'] != 'action_result/v0' or action['invocation_address'] != view['invocation_address']:
                raise ValueError('native completion identity differs')
            spec = invocation(root, address=view['invocation_address'])
            params = spec['params']
            project, run, phase = params['project'], params['run'], params['phase']
            identity(project, run)
            if phase not in ('core', 'bench') or params['revision'] != data.read_file(root / 'bin/revision').strip():
                raise ValueError('completion phase or reviewed revision differs')
            out = root / 'projects' / project / 'runs' / run
            prefix = 'bench-' if phase == 'bench' else ''
            with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if data.read_file(out / (prefix + 'task')).strip() != task:
                    raise ValueError('completion task differs from canonical run')
                if not data.read_file(out / (prefix + 'exit')).strip():
                    try:
                        status = results.collect(root, project, run, action, phase, queue.job(task)['cancel_requested'])
                        job = json.loads(data.read_file(out / 'job.json'))
                        if phase == 'core' and status == 0 and job['bench_required'] and not data.read_file(out / 'bench-task').strip():
                            if job.get('bench_submission_started'):
                                raise ValueError('benchmark submission is ambiguous; inspect its native job key')
                            job['bench_queued_at'] = time.time()
                            job['bench_submission_started'] = True
                            data.write_file(out / 'job.json', json.dumps(job))
                            bench, original = enqueue(root, project, run, 'bench', action['runner']['node'], spec['inputs']['source']['address'])
                            if original != run:
                                raise ValueError('benchmark deduplication returned another run')
                            data.write_file(out / 'bench-task', bench)
                        data.write_file(out / (prefix + 'exit'), str(status))
                    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.CalledProcessError) as error:
                        recover(out, prefix, type(error).__name__)
                        queue.ack(task, 'rejected', reason='invalid or incomplete CI result')
                        continue
                queue.ack(task, 'persisted')


def main():
    root, project, command, *args = sys.argv[1:]
    root = Path(root).resolve()
    identity(project, args[0] if command in ('submit', 'has-run', 'log', 'cancel') and args else '0')
    os.environ['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
    folder = root / 'projects' / project
    if command == 'collect':
        collect(root)
    elif command == 'version':
        if args not in ([], ['--read-only'], ['--worker']):
            raise ValueError('invalid version probe')
        if args != ['--read-only']:
            limits = tomllib.loads((root / 'host.conf').read_text())
            if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
                raise ValueError('free disk below configured floor')
            if args != ['--worker']:
                with client(root) as queue:
                    queue.fleet()
        print(data.read_file(root / 'bin/revision').strip())
    elif command == 'init':
        for name in ('slots', 'data', 'secrets', 'runs', 'logs'):
            (folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (folder / 'git').exists():
            subprocess.run(['git', 'init', '--quiet', '--bare', str(folder / 'git')], check=True)
    elif command == 'submit':
        run, *request = args
        out = folder / 'runs' / run
        out.mkdir(mode=0o700)
        attempted = False
        try:
            with data.open_file(folder / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                data.make_job(folder, run, *request[:5], request[5:])
                original = data.reuse_finished(folder, run)
                if original and data.read_file(out / 'exit').strip():
                    print('reused_from=' + original, flush=True)
                    print(data.read_file(out / 'report.txt'), end='')
                    return int(data.read_file(out / 'exit'))
                attempted = True
                task, original = (data.read_file(folder / 'runs' / original / 'task').strip(), original) if original else enqueue(root, project, run)
                if original != run:
                    data.discard(folder / 'git', run)
                    shutil.rmtree(out)
                    run = original
                    out = folder / 'runs' / run
                    print('reused_from=' + run, flush=True)
                else:
                    data.write_file(out / 'task', task)
            print('run=' + run + ' task=' + task, flush=True)
            logs(root, task)
            status = wait_run(root, project, run)
            bench = data.read_file(out / 'bench-task').strip()
            if bench:
                print('bench_task=' + bench, flush=True)
            return status
        finally:
            if not attempted:
                data.discard(folder / 'git', run)
    elif command == 'has-run':
        return 0 if (folder / 'runs' / args[0]).is_dir() else 1
    elif command in ('has-gate', 'verify'):
        if not args or not re.fullmatch(r'[0-9a-f]{40}', args[0]):
            raise ValueError('invalid commit')
        run = data.gate_run(folder, args[0])
        if command == 'has-gate':
            print(run)
            return 0
        out = folder / 'runs' / run
        status = wait_run(root, project, run)
        if data.read_file(out / 'bench-task').strip():
            print('waiting bench_task=' + data.read_file(out / 'bench-task').strip(), flush=True)
            status = wait_run(root, project, run, 'bench-') or status
        print(data.read_file(out / 'report.txt'), end='')
        job = json.loads(data.read_file(out / 'job.json'))
        if status == 0 and (not job['bench_required'] or data.read_file(out / 'bench-exit').strip() == '0'):
            print('verified=' + args[0] + ' base=' + job['base'])
            return 0
        return 1
    elif command == 'status':
        with client(root) as queue:
            view = queue.jobs(filter={'job_key_prefix': project + '/'})
            for row in view['jobs']:
                row['ci_exit'] = 137 if row['state'] == 'CANCELLED' else (row.get('action_result') or {}).get('metrics', {}).get('exit')
            print(json.dumps(view))
    elif command == 'history':
        history.query(folder, args)
    elif command in ('log', 'cancel'):
        out = folder / 'runs' / args[0]
        for prefix in ('', 'bench-'):
            with data.open_file(folder / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                task = data.read_file(out / (prefix + 'task')).strip()
                if command == 'cancel' and task and not data.read_file(out / (prefix + 'exit')).strip():
                    with client(root) as queue:
                        queue.cancel(task, reason='owner cancellation')
            if task:
                if command == 'log':
                    logs(root, task, wait=False)
                else:
                    wait_run(root, project, args[0], prefix)
    else:
        raise ValueError('unknown server command')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('ci-buddy: ' + str(error)) from None
