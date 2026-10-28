"""Trusted dispatch command adapter; project programs only run in job.sh's containers."""
import json
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

import data
import workers
import ssh

CHILD = None
CANCELLED = False
CURRENT = None


def identity(project, identifier):
    if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', project)
            or not isinstance(identifier, str) or not re.fullmatch(r'[0-9]+', identifier)):
        raise ValueError('invalid execution identity')


def interrupt(signum, frame):
    global CANCELLED
    CANCELLED = True
    workers.CANCELLED = True
    ssh.STOP_INPUT = True
    root, project, run = CURRENT
    for process in tuple(ssh.ACTIVE):
        if getattr(process, 'keep_input', False):
            with process.input_lock:
                process.close_input = True
                if process.input_finished and not process.stdin.closed:
                    process.stdin.close()
        elif process.poll() is None:
            process.terminate()
    job = json.loads(data.read_file(root / 'projects' / project / 'runs' / run / 'job.json'))
    if not job.get('worker'):
        for name in ('ci-buddy-' + run, 'ci-buddy-prepare-' + run, 'ci-buddy-fetch-' + run):
            if subprocess.run(['podman', 'container', 'exists', name]).returncode == 0:
                subprocess.run(['podman', 'kill', name], stdout=subprocess.DEVNULL)
                subprocess.run(['podman', 'wait', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if CHILD is not None and CHILD.poll() is None:
        os.killpg(CHILD.pid, signal.SIGTERM)


def run(root, project, identifier, bench=False, worker=False):
    global CHILD, CURRENT
    identity(project, identifier)
    CURRENT = root, project, identifier
    signal.signal(signal.SIGTERM, interrupt)
    out = root / 'projects' / project / 'runs' / identifier
    prefix = 'bench-' if bench else ''
    if worker:
        def disconnected():
            os.read(sys.stdin.fileno(), 1)
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=disconnected, daemon=True).start()
    if not worker and not bench:
        status = workers.dispatch(root, project, identifier)
        if status is not None:
            return status
    if not CANCELLED:
        CHILD = subprocess.Popen(['bash', str(root / 'bin/job.sh'), str(root), project, identifier,
                                  'bench' if bench else '', 'worker' if worker else ''], start_new_session=True)
        if CANCELLED:
            interrupt(signal.SIGTERM, None)
        CHILD.wait()
    if data.read_file(out / (prefix + 'exit')).strip():
        return int(data.read_file(out / (prefix + 'exit')))
    status = 137 if CANCELLED else (CHILD.returncode if 0 < CHILD.returncode <= 255 else 1)
    if not worker:
        import control
        control.recover(root, project, identifier, prefix, status)
        return int(data.read_file(out / (prefix + 'exit')))
    job = json.loads(data.read_file(out / 'job.json'))
    stage = prefix.replace('-', '_')
    job.update({stage + 'exit': status, stage + 'ended_at': time.time()})
    data.write_file(out / 'job.json', json.dumps(job))
    data.report(out, prefix)
    subprocess.run([sys.executable, str(root / 'bin/archive.py'), str(out)], check=True)
    data.write_file(out / (prefix + 'exit'), str(status) + '\n')
    return status


def direct(root, project, identifier, job):
    identity(project, identifier)
    folder = root / 'projects' / project
    (folder / 'runs' / identifier).mkdir(mode=0o700)
    argv = ['--'] if job['kind'] == 'run' else ['--rerun']
    if job['kind'] == 'test':
        argv += ['--since', job['base']]
    if job['exclusive'] or job['bench_required']:
        raise ValueError('exclusive jobs and benchmark gates stay on their coordinator')
    data.make_job(folder, identifier, *[job[key] for key in ('kind', 'commit', 'base', 'branch', 'submitter')], argv + job['argv'])
    return run(root, project, identifier, worker=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--direct']:
        root, project, identifier = sys.argv[2:]
        payload = sys.stdin.buffer.readline(workers.HISTORY_LIMIT + 1)
        if len(payload) > workers.HISTORY_LIMIT or not payload.endswith(b'\n'):
            raise ValueError('invalid direct execution input')
        raise SystemExit(direct(Path(root).resolve(), project, identifier, json.loads(payload)))
    request = json.load(sys.stdin)
    invocation = request['invocation']
    if invocation['kind'] != 'invocation/v0' or invocation['op'] != 'command' or invocation['op_version'] != 1:
        raise ValueError('unsupported invocation')
    params = invocation['params']
    identity(params['project'], params['run'])
    root = Path(__file__).resolve().parent.parent
    out = root / 'projects' / params['project'] / 'runs' / params['run']
    with data.open_file(out.parents[1] / 'submit.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data.write_file(out / 'task', request['job_id'])
    status = run(root, params['project'], params['run'])
    job = json.loads(data.read_file(out / 'job.json'))
    if status == 0 and job['bench_required']:
        status = run(root, params['project'], params['run'], bench=True)
    result = Path(request['workdir']) / 'invoke-result.json'
    data.write_file(result, json.dumps(dict(outputs={}, metrics=dict(exit=status))))
