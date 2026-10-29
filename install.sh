set -euo pipefail
umask 077
target= dispatch_dir= worker=false archive=false
while (($#)); do
    case $1 in
        --root) target=${2:?root required}; shift 2 ;;
        --dispatch-dir) dispatch_dir=${2:?reviewed dispatch directory required}; shift 2 ;;
        --worker) worker=true; shift ;;
        --archive) archive=true; shift ;;
        *) echo 'usage: bash install.sh --root <dir> [--dispatch-dir <reviewed-dir> | --worker]' >&2; exit 2 ;;
    esac
done
[[ -n $target ]] || exit 2
[[ $(id -u) != 0 ]] || { echo 'ci-buddy requires a rootless user' >&2; exit 2; }
if [[ $archive == true ]]; then
    python3 -I - "$target" 3<&0 <<'PY'
import fcntl, os, pathlib, shutil, subprocess, sys, tarfile, tempfile, tomllib
root = pathlib.Path(sys.argv[1]).resolve()
root.mkdir(mode=0o700, parents=True, exist_ok=True)
assert root.stat().st_uid == os.getuid() and root.stat().st_mode & 0o077 == 0, 'runner root must be owned and private'
with (root / 'install.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        stage = pathlib.Path(directory)
        with tarfile.open(fileobj=os.fdopen(3, 'rb'), mode='r|') as payload:
            seen = set()
            for entry in payload:
                assert entry.isfile() and not entry.issparse() and entry.size <= 16 * 1024 * 1024
                assert entry.name in {'runtime.tar', 'host.conf', 'revision'} - seen, 'invalid installer payload'
                seen.add(entry.name)
                payload.extract(entry, stage, filter='data')
        assert seen == {'runtime.tar', 'host.conf', 'revision'}, 'incomplete installer payload'
        source = stage / 'source'
        source.mkdir()
        with tarfile.open(stage / 'runtime.tar') as runtime:
            assert all(entry.isfile() or entry.isdir() for entry in runtime), 'runtime contains links or special files'
            runtime.extractall(source, filter='data')
        revision = (stage / 'revision').read_text().strip()
        conf = stage / 'host.conf'
        if (root / 'host.conf').exists():
            assert (root / 'bin/revision').read_text().strip() == revision, 'update the existing runtime explicitly'
            assert tomllib.loads((root / 'host.conf').read_text()) == tomllib.loads(conf.read_text()), 'existing host.conf differs'
        else:
            assert {path.name for path in root.iterdir()} == {'install.lock', stage.name}, 'runner root is not empty'
            shutil.copyfile(conf, root / 'host.conf')
        for name in ('runtime.tar', 'revision'):
            shutil.copyfile(stage / name, source / name)
        subprocess.run(['bash', str(source / 'install.sh'), '--root', str(root), '--worker'], check=True)
PY
    exit
fi
for tool in podman git python3 zstd systemctl systemd-escape; do command -v "$tool"; done
source_dir=$(cd "$(dirname "$0")" && pwd -P)
[[ -f $target/host.conf ]] || { echo 'ci-buddy: write <root>/host.conf with jobs, slots, memory, cpus, min_free_gb, slot_max_gb and fetch_hosts before installing' >&2; exit 2; }
root=$(cd "$target" && pwd -P)
# Reuse keys need the installed revision even when staging omits Git metadata.
if [[ -e $source_dir/.git ]]; then
    [[ -z $(git -C "$source_dir" status --porcelain) ]] || { echo "install committed reviewed source" >&2; exit 2; }
    revision=$(git -C "$source_dir" rev-parse --verify HEAD)
elif [[ -f $source_dir/revision ]]; then
    revision=$(<"$source_dir/revision")
else
    echo 'ci-buddy: install from its Git checkout or provide a staging revision file' >&2; exit 2
fi
[[ $revision =~ ^[0-9a-f]{40}$ ]] || { echo 'ci-buddy: invalid installed revision' >&2; exit 2; }
python3 -I - "$root" "$source_dir" "$worker" <<'PY'
import json, os, pathlib, shutil, stat, subprocess, sys, tomllib
root = pathlib.Path(sys.argv[1])
if subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
    raise SystemExit('ci-buddy: keep runner state and credentials outside repositories')
if root.stat().st_uid != os.getuid():
    raise SystemExit('ci-buddy: root must belong to the installing user')
root.chmod(0o700)
sys.path.insert(0, sys.argv[2])
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
for template in pathlib.Path(sys.argv[2], 'units').iterdir():
    if sys.argv[3] == 'true' and template.name.startswith('ci-buddy-collect'):
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
PY
[[ $(podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}') == 'true v2' ]] || {
    echo 'ci-buddy requires rootless Podman with cgroup v2' >&2; exit 2;
}
for file in ci-buddy control.py execute.py data.py history.py archive.py results.py safe_output.py ssh.py auth.py enroll.py install.sh plane.sh fetch.Containerfile fetch.sh job.sh upkeep.sh; do
    cp "$source_dir/$file" "$root/bin/$file.new"
    chmod +x "$root/bin/$file.new"
    mv "$root/bin/$file.new" "$root/bin/$file"
done
printf '%s\n' "$revision" >"$root/bin/revision.new"
mv "$root/bin/revision.new" "$root/bin/revision"
touch "$root/machine.lock" "$root/turnstile"
instance=$(systemd-escape --path "$root")
if [[ $worker == false ]]; then
    [[ -n $dispatch_dir && -x $dispatch_dir/runner && -f $dispatch_dir/dispatch_client/__init__.py && -f $dispatch_dir/kit/install-command.sh ]] || {
        echo 'ci-buddy: provide the reviewed runner, Python client and shared kit with --dispatch-dir' >&2; exit 2;
    }
    install -m 700 "$dispatch_dir/runner" "$root/bin/runner"
    cp -R "$dispatch_dir/dispatch_client" "$root/bin/"
    mkdir -p "$root/dispatch/kit"
    cp -R "$dispatch_dir/kit/." "$root/dispatch/kit/"
fi
if [[ -e $source_dir/.git ]]; then git -C "$source_dir" archive --format=tar HEAD >"$root/bin/runtime.tar"
else cp "$source_dir/runtime.tar" "$root/bin/runtime.tar"; fi
systemctl --user daemon-reload
systemctl --user enable --now "ci-buddy-upkeep@$instance.timer"
