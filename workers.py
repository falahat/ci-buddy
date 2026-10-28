"""Choose a warm execution host; remote hosts execute directly, without a queue."""
import fcntl
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib

import auth
import data
import history
import safe_output
import ssh
from ssh import capture, receive

SSH = [sys.executable, str(Path(__file__).with_name('ssh.py'))]
OUTPUT_LIMIT = 128 * 1024 * 1024
ARCHIVE_LIMIT = 512 * 1024 * 1024
HISTORY_LIMIT = history.HISTORY_LIMIT
CANCELLED = False


def transport(host, command):
    return command if host == 'local' else SSH + [host, shlex.join(command)]


def distance(root, project, commit):
    folder = root / 'projects' / project
    choices = []
    limits = tomllib.loads((root / 'host.conf').read_text())
    if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
        return (10**13, 0)
    if capture([sys.executable, str(root / 'bin/data.py'), 'admit', str(root), str(limits['jobs'])]).strip():
        return (10**13, 0)
    for slot in range(1, limits['slots'] + 1):
        path = folder / 'slots' / str(slot)
        path.mkdir(parents=True, exist_ok=True)
        with data.open_file(path / 'lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            ref = 'refs/slots/' + str(slot)
            git = ['git', '-C', str(folder / 'git')]
            known = subprocess.run(git + ['show-ref', '--quiet', '--verify', ref]).returncode == 0
            steps = int(capture(git + ['rev-list', '--count', ref + '...' + commit])) if known else 10**12
            choices.append((steps, slot))
    return min(choices, default=(10**13, 0))


def unpack_results(archive_path, destination):
    allowed = {'report.txt', 'phases.tsv', 'metadata.json', 'runner.log',
               'stats', 'fetch-stats', 'prepare-stats', 'job.json', 'slot'}
    seen, total = set(), 0
    with tarfile.open(archive_path, 'r:') as archive:
        for entry in archive:
            name = PurePosixPath(entry.name)
            total += entry.size
            if (not entry.isfile() or entry.issparse() or entry.mode & 0o111
                    or not name.parts or name.is_absolute() or '..' in name.parts
                    or name.as_posix() != entry.name or '\\' in entry.name
                    or not entry.name.isprintable() or ':' in name.parts[0]
                    or entry.name in seen or len(entry.name) > 4096
                    or entry.size < 0 or entry.size > OUTPUT_LIMIT
                    or total > ARCHIVE_LIMIT or len(seen) >= 4096):
                raise ValueError('unsafe worker archive entry')
            seen.add(entry.name)
            junit = len(name.parts) == 3 and name.parts[0] == 'junit' and name.parts[-1] == 'junit.xml'
            log = len(name.parts) == 1 and name.suffix == '.log'
            if entry.name not in allowed and not junit and not log:
                continue
            if (junit or entry.name in ('report.txt', 'job.json', 'phases.tsv')) and entry.size > HISTORY_LIMIT:
                raise ValueError('worker metadata exceeds size limit')
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as output:
                shutil.copyfileobj(archive.extractfile(entry), output)


def accounting(payload, slot, status):
    if not isinstance(payload, dict) or type(payload.get('exit')) is not int or payload['exit'] != status or not 0 <= status <= 255:
        raise ValueError('worker exit differs from transport')
    values = {key: payload.get(key) for key in ('queued_at', 'started_at', 'ended_at', 'cpu_s', 'peak_bytes')}
    for value in values.values():
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError('invalid worker accounting')
    if (values['queued_at'] is None or values['ended_at'] is None
            or values['started_at'] is not None and not values['queued_at'] <= values['started_at'] <= values['ended_at']
            or values['peak_bytes'] is not None and type(values['peak_bytes']) is not int
            or slot is not None and (type(slot) is not int or slot < 1)):
        raise ValueError('invalid worker timestamps or slot')
    del values['queued_at']
    return dict(values, exit=status)


def collect(root, project, identifier, endpoint, status):
    folder = root / 'projects' / project
    out = folder / 'runs' / identifier
    host, worker_root = ssh.endpoint(endpoint)
    logs = str(Path(worker_root, 'projects', project, 'logs'))
    saved = folder / 'logs' / time.strftime('%Y-%m') / (identifier + '.tar.zst')
    saved.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=identifier + '-worker-', dir=out.parent) as temporary:
        temporary = Path(temporary)
        compressed, decoded = temporary / 'return.tar.zst', temporary / 'return.tar'
        with compressed.open('xb') as output:
            code = receive(transport(host, ['bash', '-c', 'cat ' + shlex.quote(logs) + '/*/' + shlex.quote(identifier + '.tar.zst')]), output, OUTPUT_LIMIT)
        if code:
            raise subprocess.CalledProcessError(code, 'worker archive transfer')
        with decoded.open('xb') as output:
            code = receive(['zstd', '-dq', '--memory=128MB', '-c', str(compressed)], output, ARCHIVE_LIMIT)
        if code:
            raise subprocess.CalledProcessError(code, 'worker archive decoding')
        returned = temporary / 'results'
        returned.mkdir()
        unpack_results(decoded, returned)
        slot_text = data.read_file(returned / 'slot').strip()
        slot = int(slot_text) if slot_text else None
        values = accounting(json.loads(data.read_file(returned / 'job.json')), slot, status)
        if saved.exists():
            raise ValueError('worker return already exists')
        for path in returned.iterdir():
            if path.name in ('job.json', 'slot', 'runner.log'):
                continue
            if (out / path.name).exists():
                raise ValueError('worker return collides with coordinator data')
            path.rename(out / path.name)
        with data.open_file(folder / 'submit.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            job = json.loads(data.read_file(out / 'job.json'))
            job.update(values)
            data.write_file(out / 'job.json', json.dumps(job))
            data.write_file(out / 'slot', '' if slot is None else str(slot))
            data.record(out)
        report = out / 'report.txt'
        data.write_file(report, safe_output.clean(report.read_text(errors='replace')) if report.exists() else f'worker={endpoint} exit={status}\n')
        compressed.rename(saved)
        for path in out.rglob('junit.xml'):
            path.unlink()
        data.write_file(out / 'exit', str(status) + '\n')


def dispatch(root, project, identifier):
    folder = root / 'projects' / project
    out = folder / 'runs' / identifier
    job = json.loads(data.read_file(out / 'job.json'))
    peers = tomllib.loads((root / 'host.conf').read_text())['workers']
    if not peers or job['exclusive'] or job['bench_required']:
        return None
    choices = [(distance(root, project, job['commit']), '', '')]
    def discard(host, target):
        try:
            capture(transport(host, ['git', '-C', str(Path(target, 'projects', project, 'git')), 'update-ref', '-d', 'refs/ci-buddy/' + identifier]))
        except (OSError, subprocess.CalledProcessError, ValueError):
            print('worker_ref_cleanup_unavailable=' + host, flush=True)
    def probe(endpoint):
        staged = False
        try:
            if CANCELLED:
                return None
            host, target = ssh.endpoint(endpoint)
            server = ['python3', str(Path(target, 'bin/control.py')), target, project]
            if capture(transport(host, server + ['version', '--worker'])).decode().strip() != (root / 'bin/revision').read_text().strip():
                raise ValueError('version differs')
            capture(transport(host, server + ['init']))
            token = auth.read(root)
            if token:
                auth.rpc(endpoint, 'store', token)
            staged = True
            git_target = str(Path(target, 'projects', project, 'git'))
            capture(['git', '-C', str(folder / 'git'), '-c', 'core.sshCommand=' + shlex.join(SSH), '-c', 'ssh.variant=simple',
                     'push', '--quiet', git_target if host == 'local' else host + ':' + git_target,
                     job['commit'] + ':refs/ci-buddy/' + identifier])
            value = json.loads(capture(transport(host, server + ['distance', job['commit']])))
            if not isinstance(value, list) or len(value) != 2 or any(type(number) is not int or number < 0 for number in value):
                raise ValueError('invalid distance')
            return tuple(value), host, target
        except (OSError, subprocess.CalledProcessError, ValueError, TypeError) as error:
            if staged:
                discard(host, target)
            print(f'worker_unavailable={endpoint!r} reason={type(error).__name__}', flush=True)
    with ThreadPoolExecutor() as pool:
        choices.extend(value for value in pool.map(probe, peers) if value is not None)
    _, host, target = min(choices)
    for _, other_host, other_root in choices[1:]:
        if other_host != host or other_root != target:
            discard(other_host, other_root)
    if CANCELLED:
        raise InterruptedError('owner cancellation before execution')
    if not host:
        return None
    endpoint = host + ':' + target
    job['worker'] = endpoint
    data.write_file(out / 'job.json', json.dumps(job))
    with data.open_file(out / 'runner.log', 'a') as log:
        log.write('worker=' + endpoint + '\n')
        log.flush()
        command = [sys.executable if host == 'local' else 'python3', str(Path(target, 'bin/execute.py')), '--direct', target, project, identifier]
        status = receive(transport(host, command), log.buffer, OUTPUT_LIMIT, display=True,
                         payload=json.dumps(job).encode() + b'\n', keep_input=True)
    status = 137 if CANCELLED else status
    collect(root, project, identifier, endpoint, status)
    with data.open_file(out / 'runner.log', 'a') as log:
        log.write('worker_returned=' + identifier + '\n')
    return status
