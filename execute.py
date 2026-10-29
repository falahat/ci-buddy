"""One complete native job; submitted programs only execute inside job.sh."""
import json
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import data

CHILD = None
CANCELLED = False
CURRENT = None


def identity(project, run):
    if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', project)
            or not isinstance(run, str) or not re.fullmatch(r'[0-9]+', run)):
        raise ValueError('invalid execution identity')


def interrupt(signum, frame):
    global CANCELLED
    CANCELLED = True
    root, project, run = CURRENT
    for name in ('ci-buddy-' + run, 'ci-buddy-prepare-' + run, 'ci-buddy-fetch-' + run):
        if subprocess.run(['podman', 'container', 'exists', name]).returncode == 0:
            subprocess.run(['podman', 'kill', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['podman', 'wait', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if CHILD is not None and CHILD.poll() is None:
        os.killpg(CHILD.pid, signal.SIGTERM)


def run_phase(root, project, run, out, prefix=''):
    global CHILD
    CHILD = None
    if not CANCELLED:
        CHILD = subprocess.Popen(['bash', str(root / 'bin/job.sh'), str(root), project, run,
                                  'bench' if prefix else '', str(out)], start_new_session=True)
        if CANCELLED:
            interrupt(signal.SIGTERM, None)
        CHILD.wait()
    job = json.loads(data.read_file(out / 'job.json'))
    stage = prefix.replace('-', '_')
    if stage + 'exit' not in job:
        status = 137 if CANCELLED else CHILD.returncode if 0 < CHILD.returncode <= 255 else 1
        job.update({stage + 'exit': status, stage + 'ended_at': time.time()})
        data.write_file(out / 'job.json', json.dumps(job))
        data.report(out, prefix)
    return json.loads(data.read_file(out / 'job.json'))[stage + 'exit']


def pipeline(root, project, run, out, work, job):
    progress = work / 'invoke-progress.json'
    data.write_file(progress, json.dumps(dict(phase='correctness')))
    correctness = status = run_phase(root, project, run, out)
    profiling = correctness == 0 and job['bench_required'] and not CANCELLED
    if profiling:
        job = json.loads(data.read_file(out / 'job.json'))
        job['bench_queued_at'] = time.time()
        data.write_file(out / 'job.json', json.dumps(job))
        data.write_file(progress, json.dumps(dict(phase='profiling', correctness_exit=correctness)))
    print('correctness_exit=' + str(correctness), flush=True)
    if profiling:
        status = run_phase(root, project, run, out, 'bench-')
    status = 137 if CANCELLED else status
    job = json.loads(data.read_file(out / 'job.json'))
    job['overall_exit'] = status
    data.write_file(out / 'job.json', json.dumps(job))
    data.write_file(progress, json.dumps(dict(phase='complete', correctness_exit=correctness, exit=status)))
    return status


def execute(root, request):
    global CHILD, CURRENT
    invocation = request['invocation']
    if invocation['kind'] != 'invocation/v0' or invocation['op'] != 'command' or invocation['op_version'] != 1:
        raise ValueError('unsupported invocation')
    params = invocation['params']
    project, run = params['project'], params['run']
    identity(project, run)
    if params != dict(project=project, run=run, revision=data.read_file(root / 'bin/revision').strip()):
        raise ValueError('execution parameters or reviewed revision differ')
    work = Path(request['workdir']).resolve()
    if work != Path.cwd().resolve() or request['outputs_dir'] != 'outputs':
        raise ValueError('native work directory differs')
    inputs = {name: (work / request['inputs'][name]).resolve() for name in ('source', 'job')}
    if any(not path.is_relative_to(work) for path in inputs.values()):
        raise ValueError('staged input escapes work directory')
    job = json.loads(data.read_file(inputs['job'], 16 * 1024 * 1024))
    if any(not re.fullmatch(r'[0-9a-f]{40}', job[key]) for key in ('commit', 'base', 'tree')):
        raise ValueError('invalid source identity')
    CURRENT = root, project, run
    signal.signal(signal.SIGTERM, interrupt)
    folder = root / 'projects' / project
    for name in ('slots', 'data', 'secrets', 'executions'):
        (folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    repository = folder / 'git'
    with data.open_file(folder / 'submit.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not repository.exists():
            subprocess.run(['git', 'init', '--quiet', '--bare', str(repository)], check=True)
        data.git(repository, 'fetch', '--atomic', '--quiet', str(inputs['source']),
                 'refs/ci-buddy/source:refs/ci-buddy/' + run, 'refs/ci-buddy/base:refs/ci-buddy/base-' + run)
    try:
        if (data.git(repository, 'rev-parse', 'refs/ci-buddy/' + run) != job['commit']
                or data.git(repository, 'rev-parse', 'refs/ci-buddy/base-' + run) != job['base']
                or data.git(repository, 'rev-parse', job['commit'] + '^{tree}') != job['tree']):
            raise ValueError('source bundle differs from canonical job')
        out = folder / 'executions' / run
        out.mkdir(mode=0o700)
        data.output(out, job)
        data.write_file(out / 'task', request['job_id'])
        status = pipeline(root, project, run, out, work, job)
        destination = work / 'outputs/run.tar.zst'
        destination.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable, str(root / 'bin/archive.py'), str(out), str(destination)], check=True)
        data.write_file(work / 'invoke-result.json', json.dumps(dict(outputs={'run': 'outputs/run.tar.zst'}, metrics={'exit': status})))
    finally:
        data.discard(repository, run)



if __name__ == '__main__':
    execute(Path(sys.argv[1]).resolve(), json.load(sys.stdin))
