set -euo pipefail
[[ $(id -u) != 0 ]] || { echo 'ci-buddy requires a rootless user' >&2; exit 2; }
python3 -I - "$(cd "$(dirname "$0")" && pwd -P)" "$@" 3<&0 <<'PY'
import argparse, fcntl, json, os, pathlib, re, shutil, stat, subprocess, sys, tarfile, tempfile, tomllib

source = pathlib.Path(sys.argv.pop(1))
parser = argparse.ArgumentParser()
parser.add_argument('--root', required=True, type=pathlib.Path)
parser.add_argument('--dispatch-dir', type=pathlib.Path)
parser.add_argument('--worker', action='store_true')
parser.add_argument('--archive', action='store_true')
args = parser.parse_args()
os.umask(0o077)
root = args.root.resolve()
root.mkdir(mode=0o700, parents=True, exist_ok=True)
assert root.stat().st_uid == os.getuid(), 'runner root must belong to the installing user'
if subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
    raise SystemExit('ci-buddy: keep runner state and credentials outside repositories')
if args.archive:
    assert root.stat().st_mode & 0o077 == 0, 'runner root must be private'
    with (root / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            source = pathlib.Path(directory)
            with tarfile.open(fileobj=os.fdopen(3, 'rb'), mode='r|') as payload:
                payload.extractall(source, filter='data')
            revision = (source / 'revision').read_text().strip()
            conf = source / 'host.conf'
            if (root / 'host.conf').exists():
                assert (root / 'bin/revision').read_text().strip() == revision, 'update the existing runtime explicitly'
                assert tomllib.loads((root / 'host.conf').read_text()) == tomllib.loads(conf.read_text()), 'existing host.conf differs'
            else:
                assert {path.name for path in root.iterdir()} == {'install.lock', source.name}, 'runner root is not empty'
                shutil.copyfile(conf, root / 'host.conf')
            subprocess.run(['bash', str(source / 'install.sh'), '--root', str(root), '--worker'], check=True)
    raise SystemExit
for tool in ('podman', 'git', 'python3', 'zstd', 'systemctl', 'systemd-escape'):
    assert shutil.which(tool), 'missing required tool: ' + tool
if (source / '.git').exists():
    assert not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain']), 'install committed reviewed source'
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD'], text=True).strip()
else:
    revision = (source / 'revision').read_text().strip()
assert re.fullmatch('[0-9a-f]{40}', revision), 'invalid installed revision'
root.chmod(0o700)
sys.path.insert(0, str(source))
import data
limits = data.host_config(root)
if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
    raise SystemExit('ci-buddy: free disk is below min_free_gb')
for directory in ('bin', 'containers', 'cargo', 'sccache', 'projects', 'dispatch'):
    path = root / directory
    path.mkdir(exist_ok=True)
    path.chmod(0o700)
# Only the trusted fetch image writes these shared Cargo-native locks.
for name in ('.package-cache', '.package-cache-mutate'):
    with os.fdopen(os.open(root / 'cargo' / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), 'a') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise SystemExit('ci-buddy: Cargo lock must be a regular file')
config_home = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', pathlib.Path.home() / '.config'))
storage = config_home / 'containers' / 'storage.conf'
storage.parent.mkdir(parents=True, exist_ok=True)
if not storage.exists():
    storage.write_text(f'[storage]\ndriver = "overlay"\ngraphroot = {json.dumps(str(root / "containers"))}\n')
units = config_home / 'systemd' / 'user'
units.mkdir(parents=True, exist_ok=True)
instance = subprocess.check_output(['systemd-escape', '--path', str(root)], text=True).strip()
for template in (source / 'units').iterdir():
    if args.worker and template.name.startswith('ci-buddy-collect'):
        continue
    # Exact instance files leave every other root's template and units alone.
    shutil.copyfile(template, units / template.name.replace('@.', '@' + instance + '.'))
link = pathlib.Path.home() / '.ci-buddy'
if not link.exists() and not link.is_symlink():
    link.symlink_to(root, target_is_directory=True)
primary = link.resolve(strict=True)
if primary != root:
    # A second queue on this machine must join the first queue's exclusion.
    for name in ('machine.lock', 'turnstile'):
        target, path = primary / name, root / name
        if not target.is_file():
            raise SystemExit('ci-buddy: primary root has no machine locks')
        if path.exists() or path.is_symlink():
            if not path.exists() or not path.samefile(target):
                raise SystemExit('ci-buddy: existing machine lock differs; drain this root before replacing it')
        else:
            path.symlink_to(target)
assert subprocess.check_output(['podman', 'info', '--format', '{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}'], text=True).strip() == 'true v2', 'rootless Podman with cgroup v2 required'
for name in 'ci-buddy control.py execute.py data.py history.py archive.sh results.py safe_output.py ssh.py auth.py enroll.py install.sh fetch.Containerfile fetch.sh upkeep.sh'.split():
    temporary = root / 'bin' / (name + '.new')
    shutil.copyfile(source / name, temporary)
    temporary.chmod(0o700)
    temporary.replace(root / 'bin' / name)
(root / 'bin/revision.new').write_text(revision + '\n')
(root / 'bin/revision.new').replace(root / 'bin/revision')
for name in ('machine.lock', 'turnstile'):
    (root / name).touch()
if not args.worker:
    bundle = args.dispatch_dir
    assert bundle and os.access(bundle / 'runner', os.X_OK) and (bundle / 'dispatch_client/__init__.py').is_file() and (bundle / 'kit/install.sh').is_file() and (bundle / 'kit/plane.sh').is_file(), 'provide the reviewed dispatcher bundle with --dispatch-dir'
    shutil.copyfile(bundle / 'runner', root / 'bin/runner')
    (root / 'bin/runner').chmod(0o700)
    shutil.copytree(bundle / 'dispatch_client', root / 'bin/dispatch_client', dirs_exist_ok=True)
    shutil.copytree(bundle / 'kit', root / 'dispatch/kit', dirs_exist_ok=True)
    shutil.copyfile(bundle / 'kit/plane.sh', root / 'bin/plane.sh')
    shutil.copyfile(source / 'queue.toml', root / 'dispatch/plane.toml')
if (source / '.git').exists():
    with (root / 'bin/runtime.tar').open('wb') as runtime:
        subprocess.run(['git', '-C', str(source), 'archive', '--format=tar', 'HEAD'], stdout=runtime, check=True)
elif not args.worker:
    shutil.copyfile(source / 'runtime.tar', root / 'bin/runtime.tar')
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now', 'ci-buddy-upkeep@' + instance + '.timer'], check=True)
PY
