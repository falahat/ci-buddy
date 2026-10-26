import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import history


def distance(root, project, commit):
    folder = root / 'projects' / project
    choices = []
    limits = tomllib.loads((root / 'host.conf').read_text())
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
if not workers or job['exclusive'] or job['bench_required']:
    sys.exit(3)
choices = [(distance(root, project, job['commit']), '', '')]
for endpoint in workers:
    host, worker_root = endpoint.split(':', 1)
    server = ['bash', str(Path(worker_root, 'bin', 'ci-buddy')), '--server', project]
    subprocess.run(['ssh', host, shlex.join(server + ['init'])], check=True)
    subprocess.run(['git', '-C', str(folder / 'git'), 'push', '--quiet',
        host + ':' + str(Path(worker_root, 'projects', project, 'git')), job['commit'] + ':refs/ci-buddy/' + identifier], check=True)
    value = subprocess.check_output(['ssh', host, shlex.join(server + ['distance', job['commit']])])
    choices.append((tuple(json.loads(value)), host, worker_root))
_, host, worker_root = min(choices)
for _, other_host, other_root in choices[1:]:
    if other_host != host or other_root != worker_root:
        command = ['git', '-C', str(Path(other_root, 'projects', project, 'git')), 'update-ref', '-d', 'refs/ci-buddy/' + identifier]
        subprocess.run(['ssh', other_host, shlex.join(command)], check=True)
if not host:
    sys.exit(3)
job['worker'] = host + ':' + worker_root
(out / 'job.json').write_text(json.dumps(job))
server = ['bash', str(Path(worker_root, 'bin', 'ci-buddy')), '--server', project]
arguments = ['--rerun'] if job['rerun'] else []
if job['kind'] == 'test' and not job['argv']:
    arguments += ['--since', job['base']]
command = server + ['submit', identifier, job['kind'], job['commit'], job['base'], job['branch'], job['submitter']] + arguments + job['argv']
print('worker=' + job['worker'], flush=True)
with (out / 'runner.log').open('x') as log:
    with subprocess.Popen(['ssh', host, shlex.join(command)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
        for line in process.stdout:
            print(line, end='', flush=True)
            log.write(line)
        status = process.wait()
remote_logs = str(Path(worker_root, 'projects', project, 'logs'))
saved = folder / 'logs' / time.strftime('%Y-%m') / (identifier + '.tar.zst')
saved.parent.mkdir(parents=True, exist_ok=True)
with saved.open('wb') as compressed:
    subprocess.run(['ssh', host, 'cat ' + shlex.quote(remote_logs) + '/*/' + shlex.quote(identifier + '.tar.zst')], stdout=compressed, check=True)
with subprocess.Popen(['zstd', '-dq', '-c', str(saved)], stdout=subprocess.PIPE) as process:
    with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
        for entry in archive:
            name = Path(entry.name)
            if not entry.isfile() or name.is_absolute() or '..' in name.parts or entry.mode & 0o111:
                raise ValueError('unsafe worker output')
            if entry.name == 'task':
                continue
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'wb') as target:
                shutil.copyfileobj(archive.extractfile(entry), target)
    if process.wait():
        raise subprocess.CalledProcessError(process.returncode, process.args)
job = json.loads((out / 'job.json').read_text())
job.update(worker=host + ':' + worker_root)
(out / 'job.json').write_text(json.dumps(job))
row, results = json.loads(subprocess.check_output(['ssh', host, shlex.join(server + ['export', identifier])]))
database = history.db(folder)
with database:
    columns = ','.join('"' + key + '"' for key in row)
    database.execute(f'INSERT OR REPLACE INTO runs ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    for name, test_status, ms, excerpt in results:
        database.execute('INSERT OR IGNORE INTO tests(name) VALUES (?)', (name,))
        test = database.execute('SELECT id FROM tests WHERE name=?', (name,)).fetchone()[0]
        database.execute('INSERT OR REPLACE INTO results VALUES (?,?,?,?,?)', (test, int(identifier), test_status, ms, excerpt))
for path in out.rglob('junit.xml'):
    path.unlink()
print('worker_returned=' + identifier, flush=True)
sys.exit(status)
