"""Bounded native CAS artifacts with coordinator-owned identity and history."""
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import time

import data
import history
import safe_output

OUTPUT_LIMIT = 128 * 1024 * 1024
ARCHIVE_LIMIT = 512 * 1024 * 1024
HISTORY_LIMIT = history.HISTORY_LIMIT


def cas(root, operation, *arguments, limit=None):
    command = [str(root / 'bin/runner'), 'cas', '--url', data.read_file(root / 'dispatch/cas-url').strip(),
               '--credential', str(root / 'secrets/cas.json')]
    if limit is not None:
        command += ['--max-bytes', str(limit)]
    return subprocess.check_output(command + [operation, *map(str, arguments)], text=True).strip()


def unpack_results(archive_path, destination):
    allowed = {'report.txt', 'phases.tsv', 'metadata.json', 'runner.log',
               'stats', 'fetch-stats', 'prepare-stats', 'job.json', 'slot', 'bench-phases.tsv',
               'bench-stats', 'bench-fetch-stats', 'bench-prepare-stats', 'bench-runner.log'}
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
            if (junit or entry.name in ('report.txt', 'job.json', 'phases.tsv', 'bench-phases.tsv')) and entry.size > HISTORY_LIMIT:
                raise ValueError('worker metadata exceeds size limit')
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as output:
                shutil.copyfileobj(archive.extractfile(entry), output)


def accounting(payload, slot, status, phase='core'):
    prefix = 'bench_' if phase == 'bench' else ''
    if not isinstance(payload, dict) or type(payload.get(prefix + 'exit')) is not int or payload[prefix + 'exit'] != status or not 0 <= status <= 255:
        raise ValueError('worker exit differs from transport')
    values = {key: payload.get(prefix + key) for key in ('queued_at', 'started_at', 'ended_at', 'cpu_s', 'peak_bytes')}
    for value in values.values():
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError('invalid worker accounting')
    if (values['queued_at'] is None or values['ended_at'] is None
            or values['queued_at'] > values['ended_at']
            or values['started_at'] is not None and not values['queued_at'] <= values['started_at'] <= values['ended_at']
            or values['peak_bytes'] is not None and type(values['peak_bytes']) is not int
            or slot is not None and (type(slot) is not int or slot < 1)):
        raise ValueError('invalid worker timestamps or slot')
    del values['queued_at']
    return {prefix + key: value for key, value in dict(values, exit=status).items()}


def collect(root, project, identifier, action, phase, cancelled=False):
    folder = root / 'projects' / project
    out = folder / 'runs' / identifier
    output = action['outputs']['run']
    status = action['metrics']['exit']
    if (type(output['size']) is not int or not 0 <= output['size'] <= OUTPUT_LIMIT
            or type(status) is not int or not 0 <= status <= 255):
        raise ValueError('invalid native archive size or exit metric')
    with tempfile.TemporaryDirectory(dir=root / 'containers') as temporary:
        temporary = Path(temporary)
        compressed, decoded = temporary / 'run.tar.zst', temporary / 'run.tar'
        cas(root, 'get', output['address'], compressed, limit=OUTPUT_LIMIT)
        if compressed.stat().st_size != output['size']:
            raise ValueError('worker archive size differs')
        with decoded.open('xb') as destination, subprocess.Popen(
                ['zstd', '-dq', '--memory=128MB', '-c', str(compressed)], stdout=subprocess.PIPE) as process:
            total = 0
            try:
                while block := process.stdout.read1(min(65536, ARCHIVE_LIMIT - total + 1)):
                    total += len(block)
                    if total > ARCHIVE_LIMIT:
                        raise ValueError('worker archive expands beyond size limit')
                    destination.write(block)
            except BaseException:
                process.terminate()
                raise
            if process.wait():
                raise ValueError('worker archive decoding failed')
        returned = temporary / identifier
        returned.mkdir()
        unpack_results(decoded, returned)
        slot_text = data.read_file(returned / 'slot').strip()
        slot = int(slot_text) if slot_text else None
        values = accounting(json.loads(data.read_file(returned / 'job.json')), slot, status, phase)
        prefix = 'bench_' if phase == 'bench' else ''
        values[prefix + 'exit'] = 137 if cancelled else status
        job = json.loads(data.read_file(out / 'job.json'))
        job.update(values)
        job[prefix + 'node'] = action['runner']['node']
        data.write_file(returned / 'job.json', json.dumps(job))
        data.write_file(returned / 'slot', '' if slot is None else str(slot))
        job['junit_ingested_at'] = history.record(folder, returned, data.read_file)
        for path in returned.iterdir():
            if path.name in ('job.json', 'slot', 'runner.log'):
                continue
            target = out / path.name
            if target.is_dir():
                shutil.rmtree(target)
            path.replace(target)
        data.write_file(out / 'job.json', json.dumps(job))
        data.write_file(out / 'slot', '' if slot is None else str(slot))
        report = out / 'report.txt'
        text = safe_output.clean(data.read_file(report)) if report.exists() else ''
        data.write_file(report, text + ('owner cancellation: exit=137\n' if cancelled else ''))
        saved = folder / 'logs' / time.strftime('%Y-%m') / (identifier + ('-bench' if phase == 'bench' else '') + '.tar.zst')
        saved.parent.mkdir(parents=True, exist_ok=True)
        compressed.replace(saved)
        for path in out.rglob('junit.xml'):
            path.unlink()
    return 137 if cancelled else status
