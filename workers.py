import fcntl
import math
from concurrent.futures import ThreadPoolExecutor
import json
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
import history
import safe_output
from ssh import receive, capture
import auth

SSH = [sys.executable, str(Path(__file__).with_name('ssh.py'))]


def distance(root, project, commit):
    folder = root / 'projects' / project
    choices = []
    limits = tomllib.loads((root / 'host.conf').read_text())
    if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
        return (10**13, 0)
    if subprocess.check_output([sys.executable, str(root / 'bin/data.py'), 'admit', str(root), str(limits['jobs'])]).strip():
        return (10**13, 0)
    for slot in range(1, limits['slots'] + 1):
        path = folder / 'slots' / str(slot)
        path.mkdir(parents=True, exist_ok=True)
        with (path / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            ref = 'refs/slots/' + str(slot)
            git = ['git', '-C', str(folder / 'git')]
            known = subprocess.run(git + ['show-ref', '--quiet', '--verify', ref]).returncode == 0
            steps = int(subprocess.check_output(git + ['rev-list', '--count', ref + '...' + commit])) if known else 10**12
            choices.append((steps, slot))
    return min(choices, default=(10**13, 0))


OUTPUT_LIMIT = 128 * 1024 * 1024
ARCHIVE_LIMIT = 512 * 1024 * 1024
HISTORY_LIMIT = 16 * 1024 * 1024
RUN_COLUMNS = ('id', 'kind', 'key', 'tree', 'commit', 'branch', 'submitter',
               'slot', 'argv', 'queued_at', 'started_at', 'ended_at', 'exit',
               'cpu_s', 'peak_bytes', 'reused_from')


def unpack_results(archive_path, destination):
    # Only protocol data is materialized, in a new directory with no old links.
    allowed = {'report.txt', 'phases.tsv', 'metadata.json', 'runner.log',
               'stats', 'fetch-stats', 'prepare-stats'}
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
            if entry.name not in allowed:
                continue
            if entry.name == 'report.txt' and entry.size > HISTORY_LIMIT:
                raise ValueError('worker report exceeds size limit')
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as output:
                shutil.copyfileobj(archive.extractfile(entry), output)


def validate_history(payload, job, identifier, status):
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError('invalid worker history')
    row, results = payload
    if not isinstance(row, dict) or set(row) != set(RUN_COLUMNS):
        raise ValueError('invalid worker run columns')
    expected = dict(id=int(identifier), **{key: job[key] for key in
        ('kind', 'key', 'tree', 'commit', 'branch', 'submitter')})
    if (any(row[key] != value for key, value in expected.items())
            or type(row['id']) is not int or json.loads(row['argv']) != job['argv']
            or type(row['exit']) is not int or row['exit'] != status
            or not 0 <= status <= 255 or row['reused_from'] is not None):
        raise ValueError('worker returned a different job')
    for key in ('queued_at', 'started_at', 'ended_at', 'cpu_s', 'peak_bytes', 'slot'):
        value = row[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError('invalid worker accounting')
    if (row['slot'] is not None and (type(row['slot']) is not int or row['slot'] < 1)
            or row['peak_bytes'] is not None and type(row['peak_bytes']) is not int
            or row['queued_at'] is None or row['ended_at'] is None
            or row['started_at'] is not None and not row['queued_at'] <= row['started_at'] <= row['ended_at']):
        raise ValueError('invalid worker timestamps or slot')
    if not isinstance(results, list) or len(results) > 100000:
        raise ValueError('invalid worker results')
    seen = set()
    for result in results:
        if not isinstance(result, list) or len(result) != 4:
            raise ValueError('invalid worker result')
        name, test_status, ms, excerpt = result
        if (not isinstance(name, str) or not name or len(name.encode()) > 4096 or '\0' in name
                or name in seen or test_status not in ('passed', 'failed', 'skipped')
                or type(ms) not in (int, float) or not math.isfinite(ms) or ms < 0
                or excerpt is not None and (not isinstance(excerpt, str) or len(excerpt.encode()) > 4096)
                or name.startswith('phase/') and Path(name[6:]).name != name[6:]):
            raise ValueError('invalid worker result fields')
        seen.add(name)
    # Identity and admission time always come from the coordinator's submission.
    row.update(expected, argv=json.dumps(job['argv']), queued_at=job['queued_at'])
    return row, results


def main():
    mode, root, project, identifier = sys.argv[1:]
    root = Path(root)
    if mode == 'distance':
        print(json.dumps(distance(root, project, identifier)))
        sys.exit()
    folder = root / 'projects' / project
    if mode == 'export':
        database = history.db(folder)
        row = dict(database.execute('SELECT * FROM runs WHERE id=?', (int(identifier),)).fetchone())
        results = database.execute('SELECT name,status,ms,excerpt FROM results JOIN tests ON tests.id=results.test WHERE run=?', (int(identifier),)).fetchall()
        print(json.dumps([row, [list(result) for result in results]]))
        sys.exit()
    out = folder / 'runs' / identifier
    job = json.loads((out / 'job.json').read_text())
    workers = tomllib.loads((root / 'host.conf').read_text())['workers']
    # A gate's copied benchmark executable and comparison data must share a host.
    if not workers or job.get('dispatched') or job['exclusive'] or job['bench_required']:
        sys.exit(3)
    choices = [(distance(root, project, job['commit']), '', '')]


    def discard(host, worker_root):
        command = ['git', '-C', str(Path(worker_root, 'projects', project, 'git')), 'update-ref', '-d', 'refs/ci-buddy/' + identifier]
        try:
            if subprocess.run(SSH + [host, shlex.join(command)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
        except OSError:
            pass
        print(f'worker_ref_cleanup_unavailable={host!r}', flush=True)


    def probe(endpoint):
        staged = False
        try:
            host, worker_root = endpoint.split(':', 1)
            if not host or host.startswith('-') or any(c.isspace() for c in host) or not worker_root:
                raise ValueError('invalid endpoint')
            server = ['bash', str(Path(worker_root, 'bin', 'ci-buddy')), '--server', project]
            revision = capture(SSH + [host, shlex.join(server + ['version'])]).decode().strip()
            if revision != (root / 'bin/revision').read_text().strip():
                raise ValueError('version differs')
            subprocess.run(SSH + [host, shlex.join(server + ['init'])], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            token = auth.read(root)
            if token:
                auth.rpc(endpoint, 'store', token)
            staged = True
            subprocess.run(['git', '-C', str(folder / 'git'), 'push', '--quiet',
                host + ':' + str(Path(worker_root, 'projects', project, 'git')), job['commit'] + ':refs/ci-buddy/' + identifier],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=os.environ | {'GIT_SSH_COMMAND': shlex.join(SSH), 'GIT_SSH_VARIANT': 'simple'})
            value = json.loads(capture(SSH + [host, shlex.join(server + ['distance', job['commit']])]))
            if not isinstance(value, list) or len(value) != 2 or any(type(x) is not int or x < 0 for x in value):
                raise ValueError('invalid distance')
            return tuple(value), host, worker_root
        except (OSError, subprocess.CalledProcessError, ValueError, TypeError) as error:
            if staged:
                discard(host, worker_root)
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            print(f'worker_unavailable={endpoint!r} reason={reason!r}', flush=True)


    # Probe peers together so one disconnected machine does not delay every peer.
    with ThreadPoolExecutor() as pool:
        choices.extend(value for value in pool.map(probe, workers) if value is not None)
    _, host, worker_root = min(choices)
    for _, other_host, other_root in choices[1:]:
        if other_host != host or other_root != worker_root:
            discard(other_host, other_root)
    if not host:
        sys.exit(3)
    job['worker'] = host + ':' + worker_root
    (out / 'job.json').write_text(json.dumps(job))
    server = ['bash', str(Path(worker_root, 'bin', 'ci-buddy')), '--server', project]
    # The coordinator already decided reuse; a dispatched run executes once.
    arguments = ['--worker'] + ([] if job['kind'] == 'run' else ['--rerun'])
    if job['kind'] == 'test' and not job['argv']:
        arguments += ['--since', job['base']]
    command = server + ['submit', identifier, job['kind'], job['commit'], job['base'], job['branch'], job['submitter']] + arguments + job['argv']
    print('worker=' + job['worker'], flush=True)
    with (out / 'runner.log').open('xb') as log:
        status = receive(SSH + [host, shlex.join(command)], log, OUTPUT_LIMIT, display=True)
    remote_logs = str(Path(worker_root, 'projects', project, 'logs'))
    saved = folder / 'logs' / time.strftime('%Y-%m') / (identifier + '.tar.zst')
    saved.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=identifier + '-worker-', dir=out.parent) as temporary:
        temporary = Path(temporary)
        compressed, decoded = temporary / 'return.tar.zst', temporary / 'return.tar'
        with compressed.open('xb') as output:
            code = receive(SSH + [host, 'cat ' + shlex.quote(remote_logs) + '/*/' + shlex.quote(identifier + '.tar.zst')], output, OUTPUT_LIMIT)
        if code:
            raise subprocess.CalledProcessError(code, 'worker archive transfer')
        with decoded.open('xb') as output:
            code = receive(['zstd', '-dq', '--memory=128MB', '-c', str(compressed)], output, ARCHIVE_LIMIT)
        if code:
            raise subprocess.CalledProcessError(code, 'worker archive decoding')
        returned = temporary / 'results'
        returned.mkdir()
        unpack_results(decoded, returned)
        exported = capture(SSH + [host, shlex.join(server + ['export', identifier])], HISTORY_LIMIT)
        row, results = validate_history(json.loads(exported), job, identifier, status)
        if saved.exists() or (out / 'worker-results').exists():
            raise ValueError('worker return already exists')
        returned.rename(out / 'worker-results')
        compressed.rename(saved)

        database = history.db(folder)
        with database:
            columns = ','.join('"' + key + '"' for key in RUN_COLUMNS)
            database.execute(f'INSERT OR REPLACE INTO runs ({columns}) VALUES ({",".join("?" for _ in RUN_COLUMNS)})',
                             tuple(row[key] for key in RUN_COLUMNS))
            for name, test_status, ms, excerpt in results:
                database.execute('INSERT OR IGNORE INTO tests(name) VALUES (?)', (name,))
                test = database.execute('SELECT id FROM tests WHERE name=?', (name,)).fetchone()[0]
                database.execute('INSERT OR REPLACE INTO results VALUES (?,?,?,?,?)', (test, int(identifier), test_status, ms, excerpt))
        database.close()
        job.update({key: row[key] for key in ('started_at', 'ended_at', 'exit', 'cpu_s', 'peak_bytes')})
        (out / 'job.json').write_text(json.dumps(job))
        (out / 'slot').write_text('' if row['slot'] is None else str(row['slot']))
        report = out / 'worker-results' / 'report.txt'
        (out / 'report.txt').write_text(safe_output.clean(report.read_text(errors='replace')) if report.exists()
                                      else f'worker={job["worker"]} exit={status}\n')
        with (out / 'worker-exit').open('w') as marker:
            marker.write(str(status))
    print('worker_returned=' + identifier, flush=True)
    sys.exit(status)


if __name__ == "__main__":
    main()
