"""Validate raw artifacts once on the worker; store bounded summaries centrally."""
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import data
import history
import safe_output

OUTPUT_LIMIT = 128 * 1024 * 1024
ARCHIVE_LIMIT = 512 * 1024 * 1024
HISTORY_LIMIT = history.HISTORY_LIMIT
FILE_LIMIT = 4096
TEST_LIMIT = 100000
MAX_NUMBER = 2**63 - 1
PLAN = ('name', 'kind', 'exclusive', 'argv')
TIMING = ('queued_at', 'started_at', 'ended_at', 'cpu_s', 'peak_bytes', 'cap_killed')


def arguments(value):
    if (not isinstance(value, list) or len(value) > 4096
            or any(not isinstance(arg, str) or len(arg) > 65536 or '\0' in arg for arg in value)):
        raise ValueError('arguments require at most 4096 strings of at most 65536 characters without NUL')


def spec(params, revision, project=None):
    if (not isinstance(params, dict) or set(params) != {'project', 'revision', 'job'}
            or not isinstance(params['project'], str) or len(params['project']) > 65536 or not re.fullmatch(r'[A-Za-z0-9_-]+', params['project'])
            or params['revision'] != revision or not re.fullmatch(r'[0-9a-f]{40}', revision)
            or project is not None and params['project'] != project):
        raise ValueError('canonical job project or reviewed revision differs')
    try:
        size = len(json.dumps(params, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise ValueError('canonical job contains unsupported values') from None
    if size > HISTORY_LIMIT:
        raise ValueError('canonical job arguments exceed the 16 MiB metadata limit')
    job = params['job']
    fields = {'kind', 'commit', 'base', 'tree', 'branch', 'submitter', 'key', 'rerun', 'queued_at', 'steps'}
    if (not isinstance(job, dict) or set(job) != fields
            or any(not isinstance(job[key], str) or len(job[key]) > 65536 or '\0' in job[key] for key in fields - {'rerun', 'steps'})
            or job['kind'] not in ('run', 'test', 'gate', 'bench') or type(job['rerun']) is not bool
            or any(not re.fullmatch(r'[0-9a-f]{40}', job[key]) for key in ('commit', 'base', 'tree'))
            or not re.fullmatch(r'[0-9a-f]{64}|', job['key']) or (job['kind'] == 'run') != (job['key'] == '')
            or not re.fullmatch(r'(0|[1-9][0-9]*)(\.[0-9]+)?', job['queued_at'])
            or not isinstance(job['steps'], list) or not 1 <= len(job['steps']) <= 2):
        raise ValueError('invalid canonical job fields')
    queued = float(job['queued_at'])
    if not math.isfinite(queued):
        raise ValueError('invalid canonical queue timestamp')
    for step in job['steps']:
        if not isinstance(step, dict) or set(step) != set(PLAN) or type(step['exclusive']) is not bool:
            raise ValueError('invalid canonical step fields')
        arguments(step['argv'])
    first = job['steps'][0]
    if job['kind'] == 'run' and not first['argv']:
        raise ValueError('run requires a command')
    if (first['kind'] != job['kind']
            or first['name'] != ('profiling' if job['kind'] == 'bench' else 'correctness')
            or job['kind'] == 'bench' and not first['exclusive'] or job['kind'] == 'test' and first['exclusive']
            or job['kind'] == 'run' and job['rerun']
            or job['kind'] in ('gate', 'bench') and first['argv']
            or len(job['steps']) == 2 and (job['kind'] != 'gate' or job['steps'][1] != dict(name='profiling', kind='bench', exclusive=True, argv=[]))):
        raise ValueError('canonical steps differ from the selected alias')
    return params['project'], dict(job, queued_at=queued)


class JUnit(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError('JUnit must not declare a DTD')


class Tests(dict):
    total = 0
    def add(self, name, status, ms, excerpt):
        if (not isinstance(name, str) or not name or len(name.encode()) > 4096 or '\0' in name
                or status not in ('passed', 'failed', 'skipped')
                or type(ms) not in (int, float) or not 0 <= ms <= MAX_NUMBER
                or excerpt is not None and (not isinstance(excerpt, str) or len(excerpt.encode()) > 4096)
                or name.startswith('phase/') and Path(name[6:]).name != name[6:]):
            raise ValueError('invalid test or phase result')
        row = [name, status, ms, excerpt]
        self.total += len(json.dumps(row).encode()) - (len(json.dumps(self[name]).encode()) if name in self else 0)
        self[name] = row
        if len(self) > TEST_LIMIT or self.total > HISTORY_LIMIT:
            raise ValueError('test result count or byte limit exceeded')


def cas(root, operation, *arguments, limit=None):
    command = [str(root / 'bin/runner'), 'cas', '--url', data.read_file(root / 'dispatch/cas-url').strip(),
               '--credential', str(root / 'secrets/cas.json')]
    if limit is not None:
        command += ['--max-bytes', str(limit)]
    return subprocess.check_output(command + [operation, *map(str, arguments)], text=True).strip()


def artifact_path(root, path):
    relative = path.relative_to(root)
    name = relative.as_posix()
    if (not relative.parts or '..' in relative.parts or '\\' in name or not name.isprintable()
            or len(name) > 4096 or ':' in relative.parts[0]
            or any(not stat.S_ISDIR(parent.lstat().st_mode) for parent in path.parents if parent != root and parent.is_relative_to(root))):
        raise ValueError('unsafe raw artifact path')


def raw(root, path, limit=HISTORY_LIMIT, tail=False):
    artifact_path(root, path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o111 or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('unsafe raw artifact type or size')
        if tail:
            stream.seek(max(0, info.st_size - 4096))
        block = stream.read(4096 if tail else limit + 1)
        if len(block) > limit:
            raise ValueError('raw artifact exceeds byte limit')
        return block.decode(errors='ignore' if tail else 'strict')


def validate(value, job, identifier, status):
    if (not isinstance(value, dict) or set(value) != {'job_id', 'commit', 'base', 'tree', 'slot', 'steps', 'exit', 'tests', 'report'}
            or value['job_id'] != identifier or not re.fullmatch(r'j_[0-9A-HJKMNP-TV-Z]{26}', identifier)
            or any(value[key] != job[key] for key in ('commit', 'base', 'tree'))
            or type(status) is not int or not 0 <= status <= 255 or type(value['exit']) is not int or value['exit'] != status
            or value['slot'] is not None and (type(value['slot']) is not int or value['slot'] < 1)
            or not isinstance(value['report'], str) or len(value['report'].encode()) > HISTORY_LIMIT
            or not isinstance(value['steps'], list) or not 0 < len(value['steps']) <= len(job['steps'])
            or not isinstance(value['tests'], list)):
        raise ValueError('normalized result identity, shape or size differs')
    previous = job['queued_at']
    for index, step in enumerate(value['steps']):
        if (not isinstance(step, dict) or set(step) != set(('name',) + TIMING + ('exit', 'phases'))
                or step['name'] != job['steps'][index]['name']
                or not (step['exit'] is None and status == 137 or type(step['exit']) is int and 0 <= step['exit'] <= 255)
                or not isinstance(step['phases'], list) or len(step['phases']) > 4096):
            raise ValueError('normalized step differs from canonical plan')
        for field in TIMING:
            number = step[field]
            if number is not None and (type(number) not in (int, float) or not 0 <= number <= MAX_NUMBER):
                raise ValueError('invalid step accounting')
        if (step['queued_at'] is None or step['ended_at'] is None or not previous <= step['queued_at'] <= step['ended_at']
                or step['started_at'] is not None and not step['queued_at'] <= step['started_at'] <= step['ended_at']
                or any(step[field] is not None and type(step[field]) is not int for field in ('peak_bytes', 'cap_killed'))
                or index and value['steps'][index - 1]['exit'] != 0):
            raise ValueError('invalid step order or timestamps')
        phase_at = step['started_at'] if step['started_at'] is not None else step['queued_at']
        for name, when in step['phases']:
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name)
                    or type(when) not in (int, float) or not phase_at <= when <= step['ended_at']):
                raise ValueError('invalid step phase')
            phase_at = when
        previous = step['ended_at']
    if status != 137 and (status != value['steps'][-1]['exit'] or status == 0 and len(value['steps']) != len(job['steps'])):
        raise ValueError('aggregate exit differs from completed steps')
    tests = Tests()
    for row in value['tests']:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError('invalid normalized test row')
        tests.add(*row)
    if len(tests) != len(value['tests']) or len(json.dumps(value).encode()) > HISTORY_LIMIT:
        raise ValueError('duplicate or oversized normalized results')
    return value


def normalize(artifacts, job, identifier, slot):
    tests, xml_bytes, total, count = Tests(), 0, 0, 0
    steps, report = [], ['host_load=' + str(os.getloadavg()) + '\n']
    for path in artifacts.rglob('*'):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o111:
            artifact_path(artifacts, path)
            total += info.st_size
            count += 1
            if info.st_size > OUTPUT_LIMIT or total > ARCHIVE_LIMIT or count > FILE_LIMIT:
                raise ValueError('raw artifact count or byte limit exceeded')
    for planned in job['steps']:
        if 'exit' not in planned:
            break
        name = planned['name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('invalid step name')
        out = artifacts / 'steps' / name
        step = {key: planned.get(key) for key in ('name',) + TIMING + ('exit',)}
        step['phases'] = planned.get('phases', [])
        totals = []
        for field in ('fetch-stats', 'prepare-stats', 'stats'):
            try:
                numbers = dict(line.replace('=', ' ').split() for line in raw(artifacts, out / field).splitlines())
            except FileNotFoundError:
                continue
            numbers = {key: int(numbers[key]) for key in ('usage_usec', 'memory_peak_bytes', 'oom_kill')}
            if any(not 0 <= number <= MAX_NUMBER for number in numbers.values()):
                raise ValueError('cgroup accounting outside numeric bounds')
            totals.append(numbers)
        complete = len(totals) == (3 if any(phase == 'prepare' for phase, _ in step['phases']) else 2)
        step.update(cpu_s=sum(row['usage_usec'] for row in totals) / 1000000 if complete else None,
                    peak_bytes=max(row['memory_peak_bytes'] for row in totals) if complete else None,
                    cap_killed=sum(row['oom_kill'] for row in totals) if complete else None)
        steps.append(step)
        started, ended = step['started_at'], step['ended_at']
        if started is not None:
            failure = f"{name} exited {step['exit']}" if step['exit'] is not None else f'{name} cancelled before command exit'
            tests.add('phase/' + name, 'passed' if step['exit'] == 0 else 'failed', (ended - started) * 1000,
                      None if step['exit'] == 0 else failure)
            if name == 'profiling':
                tests.add('phase/profiling-wait', 'passed', (started - step['queued_at']) * 1000, None)
            report.append(f"{name} queue_s={started - step['queued_at']:.3f} run_s={ended - started:.3f} exit={step['exit']} cpu_s={step['cpu_s']} peak_bytes={step['peak_bytes']} cap_killed={step['cap_killed']} scope=fetch+prepare+run peak_includes_file_cache=true\n")
            points = step['phases'] + [['end', ended]]
            for (phase, begin), (_, end) in zip(points, points[1:]):
                report.append(f'{name}.{phase}_s={end - begin:.3f}\n')
            residual = ended - started - sum(end[1] - begin[1] for begin, end in zip(points, points[1:]))
            report.append(f'{name} unaccounted_s={residual:.3f}\n')
        else:
            report.append(name + ' ended before start; stats incomplete\n')
        if not complete:
            report.append(name + ' stats incomplete\n')
        for path in out.rglob('junit.xml'):
            text = raw(artifacts, path)
            xml_bytes += len(text.encode())
            if xml_bytes > HISTORY_LIMIT:
                raise ValueError('JUnit XML exceeds byte limit')
            for suite in ET.fromstring(text, parser=ET.XMLParser(target=JUnit())).iter('testsuite'):
                for case in suite.findall('testcase'):
                    failed = [node for node in case if node.tag in ('failure', 'error')]
                    status = 'failed' if failed else 'skipped' if case.find('skipped') is not None else 'passed'
                    seconds = float(case.attrib['time'])
                    if not 0 <= seconds <= MAX_NUMBER / 1000:
                        raise ValueError('invalid JUnit duration')
                    text = '\n'.join(node.get('message', '') + '\n' + ''.join(node.itertext())
                        for node in [*failed, *case.findall('system-err'), *case.findall('system-out')]) if failed else None
                    tests.add(case.attrib['classname'] + '::' + case.attrib['name'], status, round(seconds * 1000),
                              text.encode()[:4096].decode(errors='ignore') if text is not None else None)
    if job['kind'] == 'test':
        counts = []
        for filename in ('all-tests.json', 'selected-tests.json'):
            try:
                listing = json.loads(raw(artifacts, artifacts / 'out' / filename))
            except FileNotFoundError:
                if job['exit'] == 0:
                    raise
                report.append('test selection incomplete\n')
                break
            if not isinstance(listing, dict) or not isinstance(listing['rust-suites'], dict):
                raise ValueError('invalid nextest listing')
            runnable, total = 0, 0
            for suite in listing['rust-suites'].values():
                if not isinstance(suite, dict) or not isinstance(suite['testcases'], dict):
                    raise ValueError('invalid nextest suite')
                for case in suite['testcases'].values():
                    total += 1
                    if (not isinstance(case, dict) or type(case['ignored']) is not bool
                            or not isinstance(case['filter-match'], dict) or case['filter-match']['status'] not in ('matches', 'mismatch')
                            or total > TEST_LIMIT):
                        raise ValueError('invalid nextest case or count')
                    runnable += not case['ignored'] and case['filter-match']['status'] == 'matches'
            counts.append(runnable)
        if len(counts) == 2:
            if counts[1] > counts[0]:
                raise ValueError('selected count exceeds all runnable tests')
            report.append(f"touched_packages={job['touched_packages']} selection={job['selection']} selected={counts[1]} total={counts[0]} scope=runnable\n")
    for filename in ('phases.tsv', 'report.txt'):
        try:
            text = raw(artifacts, artifacts / 'out' / filename)
        except FileNotFoundError:
            continue
        if filename == 'report.txt':
            report.append(safe_output.clean(text))
            continue
        for line in text.splitlines():
            name, seconds, status = line.split('\t')
            if Path(name).name != name or not 0 <= int(status) <= 255:
                raise ValueError('invalid phase name or status')
            try:
                excerpt = raw(artifacts, artifacts / 'out' / (name + '.log'), OUTPUT_LIMIT, tail=True) if int(status) else None
            except FileNotFoundError:
                excerpt = f'{name} exited {status}'
            tests.add('phase/' + name, 'passed' if int(status) == 0 else 'failed', float(seconds) * 1000, excerpt)
    result = dict(job_id=identifier, slot=slot, steps=steps, exit=job['exit'], tests=list(tests.values()), report=''.join(report))
    result.update({key: job[key] for key in ('commit', 'base', 'tree')})
    return validate(result, job, identifier, job['exit'])


def collect(root, project, identifier, action, job):
    outputs, status = action['outputs'], action['metrics']['exit']
    folder = root / 'projects' / project
    for name, limit in (('results', HISTORY_LIMIT), ('logs', OUTPUT_LIMIT)):
        output = outputs[name]
        if (type(output['size']) is not int or not 0 <= output['size'] <= limit
                or not isinstance(output['address'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', output['address'])):
            raise ValueError('invalid native output size or address')
    with tempfile.TemporaryDirectory(dir=root / 'containers') as directory:
        path = Path(directory) / 'result.json'
        cas(root, 'get', outputs['results']['address'], path, limit=HISTORY_LIMIT)
        if path.stat().st_size != outputs['results']['size']:
            raise ValueError('normalized result size differs')
        result = validate(json.loads(data.read_file(path)), job, identifier, status)
        archive = Path(directory) / 'logs.tar.zst'
        cas(root, 'get', outputs['logs']['address'], archive, limit=OUTPUT_LIMIT)
        if archive.stat().st_size != outputs['logs']['size']:
            raise ValueError('opaque log archive size differs')
        (folder / 'logs').mkdir(mode=0o700, parents=True, exist_ok=True)
        archive.replace(folder / 'logs' / (identifier + '.tar.zst'))
    result['report'] = safe_output.clean(result['report'])
    history.record(folder, identifier, job, result, node=action['runner']['node'], logs=outputs['logs'])
    return result['exit']
