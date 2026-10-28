"""Apply one owner-requested install or worker registration on a private host."""
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib


def identity(root):
    boot = Path(os.sep) / 'proc' / 'sys' / 'kernel' / 'random' / 'boot_id'
    info = root.stat()
    return [boot.read_text().strip(), info.st_dev, info.st_ino]


def write_conf(root, value):
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(value)
            output.flush()
            temporary.replace(root / 'host.conf')
        finally:
            temporary.unlink(missing_ok=True)


def register(root, request, revision, old):
    assert identity(root) != request['identity'], 'a runner cannot dispatch to itself'
    host, peer = request['endpoint'].split(':', 1)
    probe = ['bash', str(Path(peer) / 'bin/ci-buddy'), '--server', 'enrollment', 'version']
    if host == 'local':
        assert identity(root)[0] == request['identity'][0], 'local worker belongs to another control host'
        target = Path(peer).stat()
        assert [target.st_dev, target.st_ino] == request['identity'][1:], 'local worker root differs'
    else:
        probe = ['python3', str(root / 'bin/ssh.py'), host, shlex.join(probe)]
    observed = subprocess.check_output(probe, text=True).strip()
    assert observed == revision, 'coordinator cannot reach the reviewed worker'
    limits = tomllib.loads(old.decode())
    if request['endpoint'] in limits['workers']:
        return False
    limits['workers'].append(request['endpoint'])
    conf = '\n'.join(key + ' = ' + json.dumps(value) for key, value in limits.items()) + '\n'
    write_conf(root, conf.encode())
    return limits


def install(root, revision, archive_data):
    with tempfile.TemporaryDirectory(prefix='enroll-', dir=root) as directory:
        stage = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive_data)) as archive:
            regular = all(member.isfile() or member.isdir() for member in archive)
            assert regular, 'runner archive contains links or special files'
            archive.extractall(stage, filter='data')
        (stage / 'revision').write_text(revision + '\n')
        with (root / 'enrollment.log').open('wb') as log:
            subprocess.run(['bash', str(stage / 'install.sh'), '--root', str(root)],
                           check=True, stdout=log, stderr=subprocess.STDOUT)


def apply(root, request, archive_data):
    revision = request['revision']
    conf = root / 'host.conf'
    old = conf.read_bytes() if conf.exists() else None
    if old is not None:
        installed = (root / 'bin/revision').read_text().strip()
        assert installed == revision, 'update the existing runner explicitly'
        if request['mode'] == 'install':
            settings = tomllib.loads(request['conf'])
            assert tomllib.loads(old.decode()) == settings, 'existing host.conf differs'
    else:
        assert request['mode'] == 'install', 'runner is not installed'
        assert not any(item.name != 'enrollment.lock' for item in root.iterdir()), 'runner root is not empty'
        write_conf(root, request['conf'].encode())
    capacity = [str(root / 'bin/pueue'), '--config', str(root / 'pueue.yml'), 'parallel', '--group', 'jobs']
    def parallel(limits):
        subprocess.run(capacity + [str(limits['jobs'] * (1 + len(limits['workers'])))],
                       check=True, stdout=subprocess.DEVNULL)
    try:
        if request['mode'] == 'register':
            limits = register(root, request, revision, old)
            if not limits:
                return
            parallel(limits)
        elif old is None:
            install(root, revision, archive_data)
        result = subprocess.check_output(['bash', str(root / 'bin/ci-buddy'),
                                          '--server', 'enrollment', 'version'], text=True)
        assert result.strip() == revision, 'installed runner revision or readiness differs'
    except BaseException:
        if request['mode'] == 'register':
            write_conf(root, old)
            parallel(tomllib.loads(old.decode()))
        raise


def main():
    os.umask(0o077)
    data = sys.stdin.buffer.read()
    size = int.from_bytes(data[:8], 'big')
    assert 0 < size <= 65536
    request = json.loads(data[8:8 + size])
    root = Path(sys.argv[1]).expanduser().absolute()
    if root.exists():
        info = root.stat()
        assert root.is_dir() and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
    else:
        assert request['mode'] == 'install'
        root.mkdir(mode=0o700, parents=True)
    root = root.resolve()
    with (root / 'enrollment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        apply(root, request, data[8 + size:])
    print(json.dumps(identity(root)))


if __name__ == '__main__':
    main()
