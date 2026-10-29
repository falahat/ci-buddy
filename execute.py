"""One ordered native job, with private context and rootless project steps."""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import data
import results

CHILD = None
CANCELLED = False
CURRENT = None


def identity(project, identifier=None):
    if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', project)
            or identifier is not None and (not isinstance(identifier, str)
            or not re.fullmatch(r'j_[0-9A-HJKMNP-TV-Z]{26}', identifier))):
        raise ValueError('invalid native CI identity')


def interrupt(signum, frame):
    global CANCELLED
    CANCELLED = True
    for name in ('ci-buddy-', 'ci-buddy-prepare-', 'ci-buddy-fetch-'):
        name += CURRENT
        if subprocess.run(['podman', 'container', 'exists', name]).returncode == 0:
            subprocess.run(['podman', 'kill', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['podman', 'wait', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if CHILD is not None and CHILD.poll() is None:
        os.killpg(CHILD.pid, signal.SIGTERM)


def pipeline(root, project, identifier, work, job):
    global CHILD
    path = work / 'job.json'
    progress = work / 'invoke-progress.json'
    correctness, announced, status = None, False, 137
    for index, plan in enumerate(job['steps']):
        job = json.loads(data.read_file(path))
        job['steps'][index]['queued_at'] = job['queued_at'] if index == 0 else time.time()
        data.write_file(path, json.dumps(job))
        value = dict(phase=plan['name'])
        if correctness is not None:
            value['correctness_exit'] = correctness
        data.write_file(progress, json.dumps(value))
        if correctness is not None:
            print('correctness_exit=' + str(correctness), flush=True)
            announced = True
        CHILD = None
        if not CANCELLED:
            CHILD = subprocess.Popen(['bash', str(root / 'bin/job.sh'), str(root), project, identifier,
                                      str(path), str(index), str(work / 'artifacts')], start_new_session=True)
            if CANCELLED:
                interrupt(signal.SIGTERM, None)
            CHILD.wait()
        job = json.loads(data.read_file(path))
        step = job['steps'][index]
        if 'exit' not in step:
            step.update(exit=137 if CANCELLED else CHILD.returncode if 0 < CHILD.returncode <= 255 else 1,
                        ended_at=time.time())
        if CANCELLED and step['exit'] != 0:
            step['exit'] = 137
        status = step['exit']
        data.write_file(path, json.dumps(job))
        if step['name'] == 'correctness':
            correctness = status
        if status or CANCELLED:
            break
    if correctness is not None and not announced:
        print('correctness_exit=' + str(correctness), flush=True)
    job['exit'] = 137 if CANCELLED else status
    data.write_file(path, json.dumps(job))
    data.write_file(progress, json.dumps(dict(phase='complete', correctness_exit=correctness, exit=job['exit'])))
    return job


def execute(root, request):
    global CURRENT
    invocation = request['invocation']
    params = invocation['params']
    project, identifier = params['project'], request['job_id']
    identity(project, identifier)
    if (invocation['kind'] != 'invocation/v0' or invocation['op'] != 'command' or invocation['op_version'] != 1
            or params != dict(project=project, revision=data.read_file(root / 'bin/revision').strip())):
        raise ValueError('execution differs from reviewed source')
    work = Path(request['workdir']).resolve()
    if work != Path.cwd().resolve() or request['outputs_dir'] != 'outputs':
        raise ValueError('native work directory differs')
    inputs = {name: (work / request['inputs'][name]).resolve() for name in ('source', 'job')}
    if any(not path.is_relative_to(work) for path in inputs.values()):
        raise ValueError('staged input escapes work directory')
    job = json.loads(data.read_file(inputs['job'], results.HISTORY_LIMIT + 1))
    if (any(not re.fullmatch(r'[0-9a-f]{40}', job[key]) for key in ('commit', 'base', 'tree'))
            or not 1 <= len(job['steps']) <= 2
            or any(step['name'] not in ('correctness', 'profiling') for step in job['steps'])
            or len({step['name'] for step in job['steps']}) != len(job['steps'])):
        raise ValueError('invalid immutable source context')
    CURRENT = identifier
    signal.signal(signal.SIGTERM, interrupt)
    folder = root / 'projects' / project
    for name in ('slots', 'data', 'secrets'):
        (folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    repository = folder / 'git'
    with data.open_file(folder / 'submit.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not repository.exists():
            subprocess.run(['git', 'init', '--quiet', '--bare', str(repository)], check=True)
        data.git(repository, 'fetch', '--atomic', '--quiet', str(inputs['source']),
                 'refs/ci-buddy/source:refs/ci-buddy/' + identifier, 'refs/ci-buddy/base:refs/ci-buddy/base-' + identifier)
    try:
        if (data.git(repository, 'rev-parse', 'refs/ci-buddy/' + identifier) != job['commit']
                or data.git(repository, 'rev-parse', 'refs/ci-buddy/base-' + identifier) != job['base']
                or data.git(repository, 'rev-parse', job['commit'] + '^{tree}') != job['tree']):
            raise ValueError('source bundle differs from canonical job')
        data.write_file(work / 'job.json', json.dumps(job))
        artifacts = work / 'artifacts'
        (artifacts / 'out').mkdir(mode=0o700, parents=True)
        job = pipeline(root, project, identifier, work, job)
        summary = results.normalize(artifacts, job, identifier, job.get('slot'))
        print(summary['report'], end='', flush=True)
        (work / 'outputs').mkdir(exist_ok=True)
        data.write_file(work / 'outputs/results.json', json.dumps(summary))
        subprocess.run(['bash', str(root / 'bin/archive.sh'), str(artifacts), str(work / 'outputs/logs.tar.zst')], check=True)
        data.write_file(work / 'invoke-result.json', json.dumps(dict(outputs=dict(results='outputs/results.json',
                        logs='outputs/logs.tar.zst'), metrics=dict(exit=summary['exit']))))
    finally:
        data.discard(repository, identifier)


if __name__ == '__main__':
    execute(Path(sys.argv[1]).resolve(), json.load(sys.stdin))
