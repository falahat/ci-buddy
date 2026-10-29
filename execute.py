"""Native job execution with direct rootless Podman steps and shared locks."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import auth
import data
import results

CHILD = None
CANCELLED = KILLED = False
CONTAINER = None
STATS = '{ cat /sys/fs/cgroup/cpu.stat; echo memory_peak_bytes=$(cat /sys/fs/cgroup/memory.peak); cat /sys/fs/cgroup/memory.events; } > /work/step/'
COMMAND = '''wrapper=$1; shift
if [ -n "$wrapper" ]; then eval "set -- $wrapper \\"\\$@\\""; fi
"$@"; status=$?
for profile in target/nextest/*; do
    junit=$profile/junit.xml
    [ -f "$junit" ] && [ "$junit" -nt /work/step/start ] || continue
    profile=${profile##*/}
    mkdir -p "/work/step/junit/$profile"
    cp -p "$junit" "/work/step/junit/$profile/junit.xml" || exit 1
done
''' + STATS + 'stats\nexit "$status"'
TEST = '''command=$1; affected=$2; shift 2
listing=${command/nextest run/nextest list}
for policy in fail pass warn; do listing=${listing/ --no-tests $policy/}; done
bash -c "$listing --message-format json" >"$CI_BUDDY_OUT/all-tests.json" || exit $?
[ -z "$affected" ] || set -- "$@" -E "$affected"
bash -c "$listing --message-format json \\"\\$@\\"" list "$@" >"$CI_BUDDY_OUT/selected-tests.json" || exit $?
bash -c "$command \\"\\$@\\"" test "$@"
'''


def identity(project, identifier=None):
    if (not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', project)
            or identifier is not None and (not isinstance(identifier, str)
            or not re.fullmatch(r'j_[0-9A-HJKMNP-TV-Z]{26}', identifier))):
        raise ValueError('invalid native CI identity')


def interrupt(signum, frame):
    global CANCELLED, KILLED
    CANCELLED = True
    unconfirmed = False
    if CONTAINER:
        exists = subprocess.run(['podman', 'container', 'exists', CONTAINER]).returncode
        if exists == 0:
            killed = subprocess.run(['podman', 'kill', CONTAINER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            stopped = subprocess.run(['podman', 'wait', '--ignore', CONTAINER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            KILLED |= killed and stopped
            unconfirmed |= not stopped
        elif exists != 1:
            unconfirmed = True
    if CHILD is not None and CHILD.poll() is None:
        try:
            os.killpg(CHILD.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            KILLED = True
    if unconfirmed:
        raise RuntimeError('owned container cleanup is unconfirmed')


def cancelled(signum, frame):
    interrupt(signum, frame)
    if not KILLED:
        raise InterruptedError('cancelled before starting a command')


def run(argv, fds=()):
    global CHILD, CONTAINER
    if CANCELLED:
        raise InterruptedError('cancelled before command started')
    # Register the child before handling cancellation; unblock its inherited mask.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        CONTAINER = next((arg[7:] for arg in argv if isinstance(arg, str) and arg.startswith('--name=')), None)
        CHILD = None
        CHILD = subprocess.Popen(list(map(str, argv)), pass_fds=fds, start_new_session=True,
                                 preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    try:
        status = CHILD.wait()
    except BaseException:
        if not CANCELLED:
            interrupt(signal.SIGTERM, None)
        CHILD.wait()
        raise
    if status:
        if status < 0 and not KILLED:
            interrupt(signal.SIGTERM, None)
            raise RuntimeError('runner command terminated by signal')
        raise subprocess.CalledProcessError(137 if KILLED else status, argv)


@contextmanager
def choose_slot(root, folder, job, limits):
    with data.open_file(root / 'containers/picker.lock', 'a') as picker:
        fcntl.flock(picker, fcntl.LOCK_EX)
        order = [job['slot']] if 'slot' in job else data.slots(folder / 'git', job['commit'], limits['slots'])
        selected = None
        for number in order:
            slot = folder / 'slots' / str(number)
            slot.mkdir(parents=True, exist_ok=True)
            lock = data.open_file(slot / 'lock', 'a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
            else:
                selected = lock
                break
        if selected is None:
            number = order[0]
            slot = folder / 'slots' / str(number)
            selected = data.open_file(slot / 'lock', 'a')
            fcntl.flock(selected, fcntl.LOCK_EX)
        job['slot'] = number
    try:
        yield slot, selected.fileno()
    finally:
        selected.close()


def run_step(root, project, identifier, work, job, index, slot, slot_fd):
    step = job['steps'][index]
    folder, out = root / 'projects' / project, work / 'artifacts/out'
    raw = work / 'artifacts/steps' / data.step_name(step['kind'])
    raw.mkdir(parents=True)
    limits = data.host_config(root)
    with data.open_file((root / 'turnstile').resolve(), 'a') as turnstile, \
            data.open_file((root / 'machine.lock').resolve(), 'a') as machine:
        fcntl.flock(turnstile, fcntl.LOCK_EX)
        fcntl.flock(machine, fcntl.LOCK_EX if step['exclusive'] else fcntl.LOCK_SH)
        fcntl.flock(turnstile, fcntl.LOCK_UN)
        if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
            raise OSError('free disk below configured floor')
        fds = slot_fd, machine.fileno()
        def phase(name):
            step.setdefault('phases', []).append([name, time.time()])
        def checked(*argv):
            run(argv, fds)
        def build(image, context, recipe):
            with data.open_file(root / 'containers' / (image + '.lock'), 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if subprocess.run(['podman', 'image', 'exists', image]).returncode:
                    checked('podman', 'build', '--retry=0', '--memory=' + limits['memory'],
                            '--memory-swap=' + limits['memory'], '-t', image, '-f', context / recipe, context)
        step['started_at'] = time.time()
        phase('checkout')
        (slot / 'src').mkdir(exist_ok=True)
        if not (slot / 'git').exists():
            checked('git', 'clone', '--quiet', '--bare', folder / 'git', slot / 'git')
        repository = ['git', '-c', 'core.hooksPath=' + str(root / 'bin'), '-c', 'core.fsmonitor=false',
                      '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', '--git-dir=' + str(slot / 'git'), '--work-tree=' + str(slot / 'src')]
        for args in (['fetch', '--quiet', str(folder / 'git'), job['commit'], job['base']],
                     ['checkout', '--quiet', '--force', '--detach', job['commit']], ['clean', '-fdq', '-e', 'target', '-e', '.git'],
                     ['update-ref', 'refs/remotes/origin/main', job['base']]):
            checked(*repository, *args)
        data.git(folder / 'git', 'update-ref', 'refs/slots/' + str(job['slot']), job['commit'])
        data.git_view(slot / 'git', slot / 'src/.git')
        phase('image')
        config = data.image(project, slot / 'git', slot / 'context')
        build(config['image_name'], slot / 'context', config['containerfile'])
        digest = hashlib.sha256(b''.join((root / 'bin' / name).read_bytes() for name in ('fetch.Containerfile', 'fetch.sh'))).hexdigest()
        context = root / 'containers' / ('fetch-' + digest)
        context.mkdir(exist_ok=True)
        for name in ('fetch.Containerfile', 'fetch.sh'):
            shutil.copyfile(root / 'bin' / name, context / name)
        fetch_image = 'ci-buddy-fetch:' + digest
        build(fetch_image, context, 'fetch.Containerfile')
        for path in ('cargo/registry', 'cargo/git', 'sccache'):
            (root / path).mkdir(parents=True, exist_ok=True)
        common = ['podman', 'run', '--rm', '--init', '--pull=never', '--hostname=' + os.uname().nodename,
                  '--memory=' + limits['memory'], '--memory-swap=' + limits['memory'], '--pids-limit=8192']
        common += ['--preserve-fd=' + str(fd) for fd in fds]
        for source, target in ((out, '/work/out'), (raw, '/work/step'), (work / 'job.json', '/work/out/job.json:ro')):
            common += ['-v', str(source) + ':' + target]
        environment = dict(CARGO_HOME='/work/cargo', RUSTC_WRAPPER='sccache', SCCACHE_DIR='/work/sccache',
                           CI_BUDDY_PROJECT=project, CI_BUDDY_RUN=identifier, CI_BUDDY_KIND=step['kind'],
                           CI_BUDDY_COMMIT=job['commit'], CI_BUDDY_BASE=job['base'], CI_BUDDY_OUT='/work/out', CI_BUDDY_DATA='/work/data')
        common += [part for key, value in environment.items() for part in ('-e', key + '=' + value)]
        dependencies = [part for name in ('registry', 'git') for part in ('-v', str(root / 'cargo' / name) + ':/work/cargo/' + name + ':ro')]
        phase('fetch')
        fetch = common + ['--name=ci-buddy-fetch-' + identifier, '-v', str(slot / 'git') + ':/work/repository:ro', '-w', '/tmp']
        for name in ('registry', 'git', '.package-cache', '.package-cache-mutate'):
            fetch += ['-v', str(root / 'cargo' / name) + ':/work/cargo/' + name]
        fetch += ['-v', str(folder / 'secrets') + ':/run/secrets:ro', '-e', 'GIT_CONFIG_GLOBAL=/run/secrets/gitconfig',
                  '-e', 'GIT_SSH_COMMAND=ssh -F /run/secrets/ssh/config', '-e', 'CARGO_NET_GIT_FETCH_WITH_CLI=true']
        if (root / 'github-token').exists() or (root / 'github-token').is_symlink():
            auth.read(root)
            fetch += ['-v', str(root / 'github-token') + ':/work/token:ro']
        checked(*fetch, '--entrypoint', 'bash', fetch_image, '/work/fetch.sh')
        project_args = dependencies + ['-v', str(slot / 'src') + ':/work/src', '-v', str(root / 'sccache') + ':/work/sccache', '-w', '/work/src']
        if limits['cpus']:
            project_args += ['--cpus=' + limits['cpus']]
        if (root / 'sccache.env').is_file():
            project_args += ['--env-file', str(root / 'sccache.env')]
        if config.get('prepare'):
            phase('prepare')
            checked(*common, *project_args, '--name=ci-buddy-prepare-' + identifier, '-v', str(folder / 'data') + ':/work/data',
                    config['image_name'], 'sh', '-c', 'eval "$1"; status=$?; ' + STATS + 'prepare-stats; exit "$status"', 'prepare', config['prepare'])
        argv = step['argv']
        if step['kind'] == 'test':
            affected = data.affected(job, slot / 'git', out / 'metadata.json')
            argv = ['bash', '-c', TEST, 'test', config['test'], affected, *argv]
        elif step['kind'] in ('gate', 'bench'):
            argv = ['bash', '-c', config[step['kind']]]
        phase('command')
        (raw / 'start').touch()
        if step['exclusive']:
            project_args += ['--security-opt', 'seccomp=unconfined']
        checked(*common, *project_args, '--name=ci-buddy-' + identifier, '--network=none',
                '-v', str(folder / 'data') + ':/work/data:' + ('rw' if step['exclusive'] else 'ro'), '-e', 'CARGO_NET_OFFLINE=true',
                config['image_name'], 'sh', '-c', COMMAND, 'run', config.get('wrapper', ''), *argv)


def pipeline(root, project, identifier, work, job):
    progress = work / 'invoke-progress.json'
    correctness = None
    with choose_slot(root, root / 'projects' / project, job, data.host_config(root)) as (slot, slot_fd):
        for index, step in enumerate(job['steps']):
            step['queued_at'] = job['queued_at'] if index == 0 else time.time()
            value = dict(phase=data.step_name(step['kind']))
            if correctness is not None:
                value['correctness_exit'] = correctness
            data.write_file(progress, json.dumps(value))
            if correctness is not None:
                print('correctness_exit=' + str(correctness), flush=True)
            try:
                run_step(root, project, identifier, work, job, index, slot, slot_fd)
                step['exit'] = 0
            except subprocess.CalledProcessError as error:
                step['exit'] = error.returncode
            step['ended_at'] = time.time()
            if step['kind'] != 'bench':
                correctness = step['exit']
            if step['exit'] or CANCELLED:
                break
    if correctness is not None and index == 0:
        print('correctness_exit=' + str(correctness), flush=True)
    job['exit'] = 137 if KILLED else step['exit']
    data.write_file(progress, json.dumps(dict(phase='complete', correctness_exit=correctness, exit=job['exit'])))
    return job


def execute(root, request):
    invocation = request['invocation']
    project, job = results.spec(invocation['params'], data.read_file(root / 'bin/revision').strip())
    identifier = request['job_id']
    identity(project, identifier)
    if invocation['kind'] != 'invocation/v0' or invocation['op'] != 'command' or invocation['op_version'] != 1:
        raise ValueError('execution differs from reviewed source')
    work = Path(request['workdir']).resolve()
    source = (work / request['inputs']['source']).resolve()
    if work != Path.cwd().resolve() or request['outputs_dir'] != 'outputs' or not source.is_relative_to(work):
        raise ValueError('native work directory differs')
    folder = root / 'projects' / project
    repository = folder / 'git'
    artifacts = work / 'artifacts'
    (artifacts / 'out').mkdir(mode=0o700, parents=True)
    signal.signal(signal.SIGTERM, cancelled)
    try:
        try:
            for name in ('slots', 'data', 'secrets'):
                (folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
            with data.open_file(folder / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not repository.exists():
                    subprocess.run(['git', 'init', '--quiet', '--bare', str(repository)], check=True)
                data.git(repository, 'fetch', '--atomic', '--quiet', str(source),
                         'refs/ci-buddy/source:refs/ci-buddy/' + identifier, 'refs/ci-buddy/base:refs/ci-buddy/base-' + identifier)
            if (data.git(repository, 'rev-parse', 'refs/ci-buddy/' + identifier) != job['commit']
                    or data.git(repository, 'rev-parse', 'refs/ci-buddy/base-' + identifier) != job['base']
                    or data.git(repository, 'rev-parse', job['commit'] + '^{tree}') != job['tree']):
                raise ValueError('source bundle differs from canonical job')
            data.write_file(work / 'job.json', json.dumps(job))
            job = pipeline(root, project, identifier, work, job)
        except InterruptedError:
            if not CANCELLED:
                raise
            pending = next((step for step in job['steps'] if 'exit' not in step), None)
            if pending is not None:
                pending.setdefault('queued_at', job['queued_at'] if pending is job['steps'][0] else time.time())
                pending.update(exit=None, ended_at=time.time())
            job['exit'] = 137
        # No workload remains; finish bounded persistence even after cancellation.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with data.open_file(work / 'invoke.log') as log:
            captured = log.buffer.read(results.OUTPUT_LIMIT + 1)
        if len(captured) > results.OUTPUT_LIMIT:
            raise ValueError('native log exceeds byte limit')
        (artifacts / 'invoke.log').write_bytes(captured)
        summary = results.normalize(artifacts, job, identifier, job.get('slot'))
        print(summary['report'], end='', flush=True)
        (work / 'outputs').mkdir(exist_ok=True)
        data.write_file(work / 'outputs/results.json', json.dumps(summary))
        subprocess.run(['bash', str(root / 'bin/archive.sh'), str(artifacts), str(work / 'outputs/logs.tar.zst')], check=True)
        data.write_file(work / 'invoke-result.json', json.dumps(dict(outputs=dict(results='outputs/results.json',
                        logs='outputs/logs.tar.zst'), metrics=dict(exit=summary['exit']))))
    finally:
        if repository.exists():
            data.discard(repository, identifier)


if __name__ == '__main__':
    execute(Path(sys.argv[1]).resolve(), json.load(sys.stdin))
