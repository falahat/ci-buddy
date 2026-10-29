"""One native runner phase; submitted programs only execute inside job.sh."""
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


def execute(root, request):
    global CHILD, CURRENT
    invocation = request['invocation']
    if invocation['kind'] != 'invocation/v0' or invocation['op'] != 'command' or invocation['op_version'] != 1:
        raise ValueError('unsupported invocation')
    params = invocation['params']
    project, run, phase = params['project'], params['run'], params['phase']
    identity(project, run)
    if phase not in ('core', 'bench') or params['revision'] != data.read_file(root / 'bin/revision').strip():
        raise ValueError('execution phase or reviewed revision differs')
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
        prefix = 'bench-' if phase == 'bench' else ''
        if phase == 'core':
            out.mkdir(mode=0o700)
            data.output(out, job)
        else:
            previous = json.loads(data.read_file(out / 'job.json'))
            if previous['commit'] != job['commit'] or previous.get('exit') != 0:
                raise ValueError('benchmark has no matching local correctness artifacts')
            data.write_file(out / 'job.json', json.dumps(job))
        data.write_file(out / (prefix + 'task'), request['job_id'])
        if not CANCELLED:
            CHILD = subprocess.Popen(['bash', str(root / 'bin/job.sh'), str(root), project, run,
                                      'bench' if phase == 'bench' else '', str(out)], start_new_session=True)
            if CANCELLED:
                interrupt(signal.SIGTERM, None)
            CHILD.wait()
        if CANCELLED:
            interrupt(signal.SIGTERM, None)
        marker = data.read_file(out / (prefix + 'exit')).strip()
        status = 137 if CANCELLED else int(marker) if marker else (CHILD.returncode if 0 < CHILD.returncode <= 255 else 1)
        if not marker or CANCELLED:
            job = json.loads(data.read_file(out / 'job.json'))
            job.update({prefix.replace('-', '_') + 'exit': status, prefix.replace('-', '_') + 'ended_at': time.time()})
            data.write_file(out / 'job.json', json.dumps(job))
            data.report(out, prefix)
            data.write_file(out / (prefix + 'exit'), str(status))
        destination = work / 'outputs/run.tar.zst'
        destination.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable, str(root / 'bin/archive.py'), str(out), str(destination)], check=True)
        data.write_file(work / 'invoke-result.json', json.dumps(dict(outputs={'run': 'outputs/run.tar.zst'}, metrics={'exit': status})))
    finally:
        data.discard(repository, run)



if __name__ == '__main__':
    execute(Path(sys.argv[1]).resolve(), json.load(sys.stdin))
