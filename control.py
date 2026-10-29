"""Application state over the native dispatch queue, CAS and completion stream."""
import fcntl
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


def lane(revision, exclusive=False):
    return ('x-' if exclusive else 'b-') + revision[:12]


def invocation(root, address=None, value=None):
    with tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
        path = Path(directory) / 'invocation.json'
        if value is not None:
            path.write_text(json.dumps(value))
            return results.cas(root, 'put', path)
        results.cas(root, 'get', address, path, limit=results.HISTORY_LIMIT)
        return json.loads(data.read_file(path))


def job_key(project, run, job):
    return project + '/' + (job['key'] if job['key'] and not job['rerun'] else run)


def native_job(root, view, project, run, job, spec=None):
    spec = invocation(root, address=view['invocation_address']) if spec is None else spec
    revision = data.read_file(root / 'bin/revision').strip()
    expected = dict(project=project, run=run, revision=revision)
    if (spec.get('kind') != 'invocation/v0' or spec.get('op') != 'command'
            or spec.get('op_version') != 1 or spec.get('params') != expected
            or view['job_key'] != job_key(project, run, job)
            or 'lane' in view and view['lane'] != lane(revision, job['exclusive'])):
        raise ValueError('native invocation differs from canonical run')
    return spec


def find_task(root, queue, project, run, job):
    key = job_key(project, run, job)
    matches, page = {}, None
    while True:
        response = queue.jobs(filter={'job_key_prefix': key}, page=page)
        for view in response['jobs']:
            if view['job_key'] != key:
                continue
            spec = invocation(root, address=view['invocation_address'])
            if spec['params']['run'] != run:
                continue
            native_job(root, dict(view, lane=view['lane']), project, run, job, spec)
            matches[view['job_id']] = view
        following = response.get('next_page')
        if following is None:
            break
        if type(following) is not int or following <= (page or 0):
            raise ValueError('invalid native job page')
        page = following
    if len(matches) != 1:
        raise ValueError('native submission missing or ambiguous; execution unconfirmed')
    return next(iter(matches))


def publish_task(out, task):
    previous = data.read_file(out / 'task').strip()
    if not isinstance(task, str) or not task or previous and previous != task:
        raise ValueError('completion task differs from canonical run')
    if not previous:
        data.write_file(out / 'task', task)


def task_for(root, queue, out):
    project, run = out.parents[1].name, out.name
    job = json.loads(data.read_file(out / 'job.json'))
    task = data.read_file(out / 'task').strip()
    if task:
        native_job(root, queue.job(task), project, run, job)
    else:
        task = find_task(root, queue, project, run, job)
        publish_task(out, task)
    return task


def enqueue(root, project, run, source=None):
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
                         params=dict(project=project, run=run, revision=revision), inputs=inputs))
    with client(root) as queue:
        try:
            answer = queue.enqueue([dict(job_key=job_key(project, run, job),
                        lane=lane(revision, job['exclusive']),
                        invocation_address=address, policy=dict(max_attempts=1))])[0]
        except Exception:
            return find_task(root, queue, project, run, job), run
        if answer['created']:
            return answer['job_id'], run
        view = queue.job(answer['job_id'])
        spec = invocation(root, address=view['invocation_address'])
        prior = spec['params']
        identity(prior['project'], prior['run'])
        if prior['project'] != project or view['job_key'] != job_key(project, run, job):
            raise ValueError('native deduplication returned another execution')
        previous = json.loads(data.read_file(folder / 'runs' / prior['run'] / 'job.json'))
        native_job(root, view, project, prior['run'], previous, spec)
        return answer['job_id'], prior['run']


def recover(out, reason):
    job = json.loads(data.read_file(out / 'job.json'))
    job['overall_exit'] = 137
    if 'exit' not in job:
        job.update(exit=137, ended_at=time.time())
    data.write_file(out / 'job.json', json.dumps(job))
    with data.open_file(out / 'report.txt', 'a') as report:
        report.write('native execution incomplete: ' + reason + '\n')
    data.record(out)
    data.write_file(out / 'exit', '137')


def wait_run(root, project, run):
    out = root / 'projects' / project / 'runs' / run
    with client(root) as queue:
        with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not data.read_file(out / 'task').strip() and data.read_file(out / 'exit').strip():
                return int(data.read_file(out / 'exit'))
            task = task_for(root, queue, out)
        settled(out, queue.wait(task))
    return int(data.read_file(out / 'exit'))


def settled(out, view):
    with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = data.read_file(out / 'exit').strip()
        if not marker or view['state'] != 'SUCCEEDED' and marker == '0':
            recover(out, view['state'])


def cancel(root, project, run):
    out = root / 'projects' / project / 'runs' / run
    try:
        with client(root) as queue:
            with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    task = task_for(root, queue, out)
                except ValueError:
                    if not data.read_file(out / 'task').strip() and not data.read_file(out / 'exit').strip():
                        recover(out, 'native identity unresolved; cancellation unconfirmed')
                    raise
                if queue.job(task)['state'] not in ('CANCELLED', 'SUCCEEDED', 'FAILED'):
                    queue.cancel(task, reason='owner cancellation')
            view = queue.wait(task)
            settled(out, view)
            if view['state'] not in ('CANCELLED', 'SUCCEEDED'):
                raise ValueError('native state ' + view['state'])
        return 0
    except Exception as error:
        print('cancellation unconfirmed: ' + str(error), file=sys.stderr)
        return 1


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
            bound = False
            disposition = 'persisted'
            try:
                view = queue.job(task)
                action = completion['action_result']
                if (action['kind'] != 'action_result/v0' or action['invocation_address'] != view['invocation_address']
                        or completion['job_key'] != view['job_key']):
                    raise ValueError('native completion identity differs')
                spec = invocation(root, address=view['invocation_address'])
                params = spec['params']
                project, run = params['project'], params['run']
                identity(project, run)
                out = root / 'projects' / project / 'runs' / run
                with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    job = json.loads(data.read_file(out / 'job.json'))
                    native_job(root, dict(view, lane=completion['lane']), project, run, job, spec)
                    publish_task(out, task)
                    bound = True
                    if not data.read_file(out / 'exit').strip():
                        status = results.collect(root, project, run, action, queue.job(task)['cancel_requested'])
                        data.write_file(out / 'exit', str(status))
            except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.CalledProcessError) as error:
                print('native completion rejected: ' + task + ': ' + str(error), file=sys.stderr)
                # Never mutate another run on identity failure, or poison the inbox.
                if bound:
                    with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        if data.read_file(out / 'task').strip() == task and not data.read_file(out / 'exit').strip():
                            recover(out, str(error))
                disposition = 'rejected'
            queue.ack(task, disposition, reason='invalid or incomplete CI result' if disposition == 'rejected' else None)


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
                if not task:
                    with client(root) as queue:
                        task = task_for(root, queue, out)
                publish_task(out, task)
            print('run=' + run + ' task=' + task, flush=True)
            logs(root, task)
            return wait_run(root, project, run)
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
        print(data.read_file(out / 'report.txt'), end='')
        job = json.loads(data.read_file(out / 'job.json'))
        if status == 0:
            print('verified=' + args[0] + ' base=' + job['base'])
            return 0
        return 1
    elif command == 'status':
        with client(root) as queue:
            view = queue.jobs(filter={'job_key_prefix': project + '/'})
            for row in view['jobs']:
                current = queue.job(row['job_id'])
                row['ci_exit'] = 137 if row['state'] == 'CANCELLED' else (current.get('action_result') or {}).get('metrics', {}).get('exit')
                if row['state'] == 'RUNNING':
                    row['progress'] = current.get('progress')
            print(json.dumps(view))
    elif command == 'history':
        history.query(folder, args)
    elif command == 'cancel':
        return cancel(root, project, args[0])
    elif command == 'log':
        out = folder / 'runs' / args[0]
        task = data.read_file(out / 'task').strip()
        if task:
            logs(root, task, wait=False)
    else:
        raise ValueError('unknown server command')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('ci-buddy: ' + str(error)) from None
