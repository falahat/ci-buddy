"""Native queue identity, immutable source context and normalized CI history."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import data
import history
import results
from execute import identity


def client(root):
    from dispatch_client import DispatchClient
    return DispatchClient('unix:' + str(root / 'dispatch/app.sock'), retry_requests=False)


def lane(revision, exclusive=False):
    return ('x-' if exclusive else 'b-') + revision[:12]


def blob(root, address=None, value=None):
    with tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
        path = Path(directory) / 'value.json'
        if value is not None:
            data.write_file(path, json.dumps(value))
            return results.cas(root, 'put', path)
        results.cas(root, 'get', address, path, limit=results.HISTORY_LIMIT)
        return json.loads(data.read_file(path, results.HISTORY_LIMIT + 1))


def context(root, view, project=None):
    spec = blob(root, address=view['invocation_address'])
    params = spec['params']
    name = params['project']
    identity(name, view['job_id'])
    revision = data.read_file(root / 'bin/revision').strip()
    if (spec['kind'] != 'invocation/v0' or spec['op'] != 'command' or spec['op_version'] != 1
            or params != dict(project=name, revision=revision) or project is not None and project != name):
        raise ValueError('native invocation differs from reviewed project')
    job = blob(root, address=spec['inputs']['job']['address'])
    key = job['key'] if job['key'] and not job['rerun'] else view['invocation_address']
    if view['job_key'] != name + '/' + key or view['lane'] != lane(revision, job['steps'][0]['exclusive']):
        raise ValueError('native cache identity or lane differs')
    return name, job


def enqueue(root, project, nonce, job):
    folder = root / 'projects' / project
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
    revision = data.read_file(root / 'bin/revision').strip()
    address = blob(root, value=dict(kind='invocation/v0', op='command', op_version=1,
        params=dict(project=project, revision=revision), inputs=dict(source=dict(address=source, kind='blob'),
        job=dict(address=blob(root, value=job), kind='blob'))))
    submission = 'ci/' + project + '/' + nonce
    key = job['key'] if job['key'] and not job['rerun'] else address
    with client(root) as queue:
        try:
            answer = queue.enqueue([dict(job_key=project + '/' + key, submission_key=submission,
                lane=lane(revision, job['steps'][0]['exclusive']), invocation_address=address,
                policy=dict(max_attempts=1))])[0]
        except Exception:
            return queue.lookup(submission)
        return queue.job(answer['job_id'])


def directory(root, project, identifier, job):
    out = root / 'projects' / project / 'runs' / identifier
    out.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not (out / 'job.json').exists():
        data.write_file(out / 'job.json', json.dumps(job))
    return out


def settle(root, view, project=None):
    project, job = context(root, view, project)
    out = directory(root, project, view['job_id'], job)
    row = history.run(out.parents[1], view['job_id'])
    if view['state'] in ('SUCCEEDED', 'CANCELLED') and row and row['exit'] is not None:
        if not (out / 'report.txt').exists():
            data.write_file(out / 'report.txt', json.dumps(row))
        return row['exit']
    reason = 'native execution incomplete: ' + view['state'] + ': ' + str(view.get('reason', '')) + '\n'
    if row is None:
        history.record(out.parents[1], view['job_id'], job, dict(steps=[], tests=[], exit=None, slot=None))
    data.write_file(out / 'report.txt', reason)
    print(reason, end='', file=sys.stderr)
    return 1


def logs(root, identifier, wait=True):
    offset = 0
    with client(root) as queue, tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
        path = Path(directory) / 'chunk'
        while True:
            response = queue.logs(identifier, after=offset, wait=wait)
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
            disposition = 'persisted'
            try:
                view = queue.job(completion['job_id'])
                action = completion['action_result']
                if (action['kind'] != 'action_result/v0' or action['invocation_address'] != view['invocation_address']
                        or completion['job_key'] != view['job_key'] or completion['lane'] != view['lane']):
                    raise ValueError('native completion identity differs')
                project, job = context(root, view)
                folder = root / 'projects' / project
                with data.open_file(folder / 'submit.lock', 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    directory(root, project, view['job_id'], job)
                    results.collect(root, project, view['job_id'], action, job)
            except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
                print('native completion rejected: ' + str(error), file=sys.stderr)
                disposition = 'rejected'
            queue.ack(completion['job_id'], disposition)


def main():
    root, project, command, *args = sys.argv[1:]
    root = Path(root).resolve()
    identity(project)
    os.environ['PATH'] = str(root / 'bin') + os.pathsep + os.environ['PATH']
    folder = root / 'projects' / project
    if command == 'collect':
        collect(root)
    elif command == 'version':
        if args not in ([], ['--read-only'], ['--worker']):
            raise ValueError('invalid version probe')
        if args != ['--read-only']:
            if shutil.disk_usage(root).free < data.host_config(root)['min_free_gb'] * 10**9:
                raise ValueError('free disk below configured floor')
            if args != ['--worker']:
                with client(root) as queue:
                    queue.fleet()
        print(data.read_file(root / 'bin/revision').strip())
    elif command == 'init':
        for name in ('slots', 'data', 'secrets', 'runs'):
            (folder / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (folder / 'git').exists():
            subprocess.run(['git', 'init', '--quiet', '--bare', str(folder / 'git')], check=True)
    elif command == 'submit':
        nonce, *request = args
        if not re.fullmatch('[0-9]+', nonce):
            raise ValueError('invalid source submission nonce')
        try:
            with data.open_file(folder / 'submit.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                job = data.make_job(folder, *request[:5], request[5:])
                previous = history.reuse(folder, job['key']) if job['key'] and not job['rerun'] else None
                if previous:
                    with client(root) as queue:
                        if queue.job(previous['id'])['state'] != 'SUCCEEDED':
                            previous = None
                if previous:
                    print('reused_from=' + previous['id'], flush=True)
                    print('run=' + previous['id'] + ' task=' + previous['id'], flush=True)
                    report = folder / 'runs' / previous['id'] / 'report.txt'
                    print(data.read_file(report) if report.exists() else json.dumps(previous))
                    return previous['exit']
                view = enqueue(root, project, nonce, job)
                project, original = context(root, view, project)
                directory(root, project, view['job_id'], original)
                if original != job:
                    print('reused_from=' + view['job_id'], flush=True)
            print('run=' + view['job_id'] + ' task=' + view['job_id'], flush=True)
            logs(root, view['job_id'])
            with client(root) as queue:
                return settle(root, queue.wait(view['job_id']), project)
        finally:
            data.discard(folder / 'git', nonce)
    elif command == 'verify':
        commit = args[0]
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('invalid commit')
        candidates = [(out, json.loads(data.read_file(out / 'job.json'))) for out in (folder / 'runs').iterdir()]
        out, job = next((out, job) for out, job in sorted(candidates, reverse=True)
                        if job['kind'] == 'gate' and job['commit'] == commit)
        with client(root) as queue:
            status = settle(root, queue.wait(out.name), project)
        print(data.read_file(out / 'report.txt'), end='')
        if status == 0:
            print('verified=' + commit + ' base=' + job['base'])
        return 0 if status == 0 else 1
    elif command == 'status':
        with client(root) as queue:
            print(json.dumps(queue.jobs(filter={'job_key_prefix': project + '/'})))
    elif command == 'history':
        history.query(folder, args)
    elif command in ('log', 'cancel'):
        identity(project, args[0])
        with client(root) as queue:
            view = queue.job(args[0])
            context(root, view, project)
            if command == 'log':
                logs(root, view['job_id'], wait=False)
            else:
                if view['state'] not in ('SUCCEEDED', 'CANCELLED', 'FAILED'):
                    queue.cancel(view['job_id'], reason='owner cancellation')
                view = queue.wait(view['job_id'])
                settle(root, view, project)
                if view['state'] not in ('SUCCEEDED', 'CANCELLED'):
                    raise ValueError('cancellation unconfirmed: ' + view['state'])
    else:
        raise ValueError('unknown server command')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        raise SystemExit('ci-buddy: ' + str(error)) from None
