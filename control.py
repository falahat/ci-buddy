"""ci-buddy's application commands over the reviewed dispatch-service client."""
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
import threading
import time
import tomllib

import data
import history


def client(root):
    from dispatch_client import DispatchClient
    return DispatchClient('unix:' + str(root / 'dispatch/app.sock'), retry_requests=False)


def reader(command, **options):
    process = subprocess.Popen(command, **options)
    threading.Thread(target=process.wait, daemon=True).start()
    return process


def invocation(root, address=None, params=None):
    store = root / 'dispatch/cas/blobs/sha256'
    if params is not None:
        body = json.dumps(dict(kind='invocation/v0', op='command', op_version=1,
                               params=params, inputs={}), sort_keys=True, separators=(',', ':')).encode()
        address = 'sha256:' + hashlib.sha256(body).hexdigest()
        store.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=store) as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
            try:
                os.link(output.name, store / address[7:])
            except FileExistsError:
                pass
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', address):
        raise ValueError('invalid invocation address')
    with data.open_file(store / address[7:]) as source:
        body = source.read(16 * 1024 * 1024 + 1).encode()
    if len(body) > 16 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != address[7:]:
        raise ValueError('invocation integrity or size differs')
    return address if params is not None else json.loads(body)


def enqueue(root, project, run):
    out = root / 'projects' / project / 'runs' / run
    job = json.loads(data.read_file(out / 'job.json'))
    key = project + '/' + (job['key'] if job['key'] and not job['rerun'] else run)
    address = invocation(root, params=dict(project=project, run=run))
    with client(root) as queue:
        answer = queue.enqueue([dict(job_key=key, lane='exclusive' if job['exclusive'] else 'jobs',
                                     invocation_address=address, policy=dict(max_attempts=1))])[0]
        if answer['created']:
            return answer['job_id'], run
        prior = invocation(root, address=queue.job(answer['job_id'])['invocation_address'])['params']
        if prior['project'] != project or not re.fullmatch(r'[0-9]+', prior['run']):
            raise ValueError('queue returned another project or invalid run')
        return answer['job_id'], prior['run']


def recover(root, project, run, prefix, status=137):
    out = root / 'projects' / project / 'runs' / run
    with data.open_file(root / 'containers' / ('recover-' + project + '-' + run + '.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if data.read_file(out / (prefix + 'exit')).strip():
            return
        job = json.loads(data.read_file(out / 'job.json'))
        if job.get('worker'):
            raise ValueError('worker outcome is incomplete; inspect the worker before another submission')
        for name in ('ci-buddy-' + run, 'ci-buddy-prepare-' + run, 'ci-buddy-fetch-' + run):
            subprocess.run(['podman', 'wait', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stage = prefix.replace('-', '_')
        job.update({stage + 'exit': status, stage + 'ended_at': time.time()})
        data.write_file(out / 'job.json', json.dumps(job))
        data.report(out, prefix)
        data.record(out)
        subprocess.run([sys.executable, str(root / 'bin/archive.py'), str(out)], check=True)
        data.write_file(out / (prefix + 'exit'), str(status) + '\n')
        data.git(out.parents[1] / 'git', 'update-ref', '-d', 'refs/ci-buddy/' + run)


def wait_run(root, project, run, prefix=''):
    out = root / 'projects' / project / 'runs' / run
    task = data.read_file(out / (prefix + 'task')).strip()
    if not task and data.read_file(out / (prefix + 'exit')).strip():
        return int(data.read_file(out / (prefix + 'exit')))
    with client(root) as queue:
        view = queue.wait(task)
    recover(root, project, run, prefix)
    status = int(data.read_file(out / (prefix + 'exit')))
    if view['state'] != 'SUCCEEDED' and status == 0:
        raise ValueError('queue execution did not finish successfully: ' + view['state'])
    return status


def phase(root, project, run):
    out = root / 'projects' / project / 'runs' / run
    job = json.loads(data.read_file(out / 'job.json'))
    if data.read_file(out / 'exit').strip():
        return int(data.read_file(out / 'exit'))
    if not job['bench_required']:
        return wait_run(root, project, run)
    with tempfile.TemporaryFile() as reply:
        waiter = reader([sys.executable, __file__, str(root), project, 'wait-job',
                         data.read_file(out / 'task').strip()], stdout=reply)
        tail = subprocess.Popen(['tail', '-n', '+1', '-F', '--pid=' + str(waiter.pid), str(out / 'exit')],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            marker = tail.stdout.readline()
            if marker:
                if waiter.poll() is None:
                    waiter.terminate()
                waiter.wait()
                return int(marker)
            if waiter.wait():
                raise ValueError('queue wait failed; execution is not resubmitted')
            reply.seek(0)
            recover(root, project, run, '')
            return int(data.read_file(out / 'exit'))
        finally:
            if waiter.poll() is None:
                waiter.terminate()
            waiter.wait()
            tail.terminate()
            tail.wait()
            tail.stdout.close()


def watch(root, project, run):
    out = root / 'projects' / project / 'runs' / run
    try:
        log = data.open_file(out / 'runner.log')
    except FileNotFoundError:
        status = phase(root, project, run)
        subprocess.run([sys.executable, str(root / 'bin/archive.py'), str(out), 'log'], check=True)
        return status
    with log:
        process = reader([sys.executable, __file__, str(root), project, 'phase', run])
        subprocess.run(['tail', '-n', '+1', '-f', '--pid=' + str(process.pid)], stdin=log, check=True)
        subprocess.run(['cat'], stdin=log, check=True)
        return process.wait()


def main():
    root, project, command, *args = sys.argv[1:]
    root = Path(root).resolve()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', project):
        raise ValueError('invalid project')
    if command in ('submit', 'phase', 'has-run', 'log', 'cancel'):
        if not args or not re.fullmatch(r'[0-9]+', args[0]):
            raise ValueError('invalid run')
    if command in ('has-gate', 'verify', 'distance') and (not args or not re.fullmatch(r'[0-9a-f]{40}', args[0])):
        raise ValueError('invalid commit')
    os.environ['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
    folder = root / 'projects' / project
    if command == 'version':
        if args not in ([], ['--read-only'], ['--worker']):
            raise ValueError('invalid version probe')
        if args != ['--read-only']:
            limits = tomllib.loads((root / 'host.conf').read_text())
            if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
                raise ValueError('free disk below configured floor')
            if args != ['--worker']:
                with client(root) as queue:
                    queue.jobs()
        print((root / 'bin/revision').read_text().strip())
    elif command == 'init':
        for name in ('slots', 'data', 'secrets', 'runs', 'logs'):
            (folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (folder / 'git').exists():
            subprocess.run(['git', 'init', '--quiet', '--bare', str(folder / 'git')], check=True)
    elif command == 'submit':
        run, *request = args
        if not run.isdecimal():
            raise ValueError('invalid run')
        out = folder / 'runs' / run
        out.mkdir(mode=0o700)
        attempted = False
        try:
            with data.open_file(folder / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                data.make_job(folder, run, *request[:5], request[5:])
                original = data.reuse_finished(folder, run)
                if original:
                    print('reused_from=' + original, flush=True)
                    print(data.read_file(out / 'report.txt'), end='')
                    return int(data.read_file(out / 'exit'))
                attempted = True
                task, original = enqueue(root, project, run)
                if original != run:
                    data.git(folder / 'git', 'update-ref', '-d', 'refs/ci-buddy/' + run)
                    shutil.rmtree(out)
                    run = original
                    out = folder / 'runs' / run
                    if not data.read_file(out / 'task').strip():
                        data.write_file(out / 'task', task + '\n')
                    task = data.read_file(out / 'task').strip()
                    print('reused_from=' + run, flush=True)
                else:
                    data.write_file(out / 'task', task + '\n')
            print('run=' + run + ' task=' + task, flush=True)
            return watch(root, project, run)
        finally:
            if not attempted:
                data.git(folder / 'git', 'update-ref', '-d', 'refs/ci-buddy/' + run)
    elif command == 'phase':
        return phase(root, project, args[0])
    elif command == 'wait-job':
        with client(root) as queue:
            print(json.dumps(queue.wait(args[0])))
    elif command == 'has-run':
        return 0 if args[0].isdecimal() and (folder / 'runs' / args[0]).is_dir() else 1
    elif command in ('has-gate', 'verify'):
        run = data.gate_run(folder, args[0])
        if command == 'has-gate':
            print(run)
            return 0
        out = folder / 'runs' / run
        status = phase(root, project, run)
        if data.read_file(out / 'bench-task').strip():
            print('waiting bench_task=' + data.read_file(out / 'bench-task').strip(), flush=True)
            status = wait_run(root, project, run, 'bench-') or status
        else:
            status = wait_run(root, project, run) or status
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
                params = invocation(root, address=row['invocation_address'])['params']
                out = folder / 'runs' / params['run']
                job = json.loads(data.read_file(out / 'job.json'))
                marker = out / ('bench-exit' if job['bench_required'] else 'exit')
                try:
                    value = data.read_file(marker).strip()
                except FileNotFoundError:
                    value = ''
                row['ci_exit'] = int(value) if value else None
            print(json.dumps(view))
    elif command == 'history':
        history.query(folder, args)
    elif command == 'log':
        run = args[0]
        out = folder / 'runs' / run
        job = json.loads(data.read_file(out / 'job.json'))
        out = folder / 'runs' / str(job.get('reused_from', run))
        if any((folder / 'logs').glob('*/' + out.name + '.tar.zst')):
            subprocess.run([sys.executable, str(root / 'bin/archive.py'), str(out), 'log'], check=True)
        else:
            print(data.read_file(out / 'runner.log'), end='')
    elif command == 'cancel':
        run = args[0]
        out = folder / 'runs' / run
        with client(root) as queue:
            for prefix in ('', 'bench-'):
                task = data.read_file(out / (prefix + 'task')).strip()
                if task and not data.read_file(out / (prefix + 'exit')).strip():
                    queue.cancel(task, reason='owner cancellation')
                    queue.wait(task)
                    recover(root, project, run, prefix)
    elif command == 'distance':
        import workers
        print(json.dumps(workers.distance(root, project, args[0])))
    else:
        raise ValueError('unknown server command')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit('ci-buddy: ' + str(error)) from None
