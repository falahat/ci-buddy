# Only ci-buddy's trusted fetch image runs this code with secrets mounted.
set -euo pipefail
# Neither Python imports nor Cargo config discovery may start in project code.
cd /tmp
exec python3 -I - "${1:-}" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

prefix = sys.argv[1]
if prefix not in ('', 'bench-'):
    raise SystemExit('invalid fetch prefix')
env = {key: os.environ[key] for key in ('PATH', 'RUSTUP_HOME')}
env.update(HOME='/tmp', CARGO_HOME='/work/cargo', RUSTUP_TOOLCHAIN='1.99.0',
    CARGO_NET_GIT_FETCH_WITH_CLI='true', CARGO_NET_RETRY='0', GIT_TERMINAL_PROMPT='0',
    GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/run/secrets/gitconfig',
    GIT_ALLOW_PROTOCOL='https:ssh', GIT_CONFIG_COUNT='3',
    GIT_CONFIG_KEY_0='core.hooksPath', GIT_CONFIG_VALUE_0='/tmp/no-hooks',
    GIT_CONFIG_KEY_1='core.fsmonitor', GIT_CONFIG_VALUE_1='false',
    GIT_CONFIG_KEY_2='credential.helper', GIT_CONFIG_VALUE_2='',
    GIT_SSH_COMMAND='ssh -F /run/secrets/ssh/config -T -x -o BatchMode=yes '
        '-o StrictHostKeyChecking=yes -o IdentitiesOnly=yes '
        '-o ForwardAgent=no -o ClearAllForwardings=yes')


def checked(*arguments, **options):
    return subprocess.run(arguments, env=env, check=True, **options)


def source_file(member, destination):
    if not (member.isfile() or member.isdir() or member.issym()):
        raise ValueError('special file in source archive')
    # The standard filter rejects absolute paths, traversal and escaping links.
    return tarfile.data_filter(member, destination)


try:
    snapshot = Path('/work/src')
    snapshot.mkdir(exist_ok=True)
    if any(snapshot.iterdir()):
        raise ValueError('trusted fetch source directory must start empty')
    with tempfile.TemporaryFile(dir='/tmp') as archive:
        checked('git', '--git-dir=/work/repository', 'archive', '--format=tar', 'HEAD', stdout=archive)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as source:
            source.extractall(snapshot, filter=source_file)
    config = Path('/run/secrets/cargo-config.toml')
    if config.is_symlink():
        raise ValueError('owner Cargo config must be a regular file')
    cargo = ['cargo', '--config', str(config)] if config.is_file() else ['cargo']
    manifest = str(snapshot / 'Cargo.toml')
    checked(*cargo, 'fetch', '--locked', '--manifest-path', manifest)
    metadata = checked(*cargo, 'metadata', '--locked', '--no-deps', '--format-version', '1',
                       '--manifest-path', manifest, stdout=subprocess.PIPE).stdout
    Path('/work/out/metadata.json').write_bytes(metadata)
except subprocess.CalledProcessError as error:
    raise SystemExit(error.returncode)
finally:
    stats = Path('/sys/fs/cgroup/cpu.stat').read_text()
    stats += 'memory_peak_bytes=' + Path('/sys/fs/cgroup/memory.peak').read_text()
    stats += Path('/sys/fs/cgroup/memory.events').read_text()
    Path('/work/out', prefix + 'fetch-stats').write_text(stats)
PY
