set -euo pipefail
umask 077
target= dispatch_dir= worker=false
while (($#)); do
    case $1 in
        --root) target=${2:?root required}; shift 2 ;;
        --dispatch-dir) dispatch_dir=${2:?reviewed dispatch directory required}; shift 2 ;;
        --worker) worker=true; shift ;;
        *) echo 'usage: bash install.sh --root <dir> [--dispatch-dir <reviewed-dir> | --worker]' >&2; exit 2 ;;
    esac
done
[[ -n $target && ( $worker == false || -z $dispatch_dir ) ]] || exit 2
[[ $(id -u) != 0 ]] || { echo 'ci-buddy requires a rootless user' >&2; exit 2; }
for tool in podman git python3 zstd systemctl systemd-escape; do command -v "$tool"; done
source_dir=$(cd "$(dirname "$0")" && pwd -P)
[[ -f $target/host.conf ]] || { echo 'ci-buddy: write <root>/host.conf with jobs, slots, memory, cpus, min_free_gb, slot_max_gb and workers before installing' >&2; exit 2; }
root=$(cd "$target" && pwd -P)
# Reuse keys need the installed revision even when staging omits Git metadata.
if [[ -e $source_dir/.git ]]; then
    revision=$(git -C "$source_dir" rev-parse --verify HEAD)
elif [[ -f $source_dir/revision ]]; then
    revision=$(<"$source_dir/revision")
else
    echo 'ci-buddy: install from its Git checkout or provide a staging revision file' >&2; exit 2
fi
[[ $revision =~ ^[0-9a-f]{40}$ ]] || { echo 'ci-buddy: invalid installed revision' >&2; exit 2; }
python3 - "$root" "$source_dir" "$worker" <<'PY'
import json, os, pathlib, shutil, stat, subprocess, sys, tomllib
root = pathlib.Path(sys.argv[1])
if root.stat().st_uid != os.getuid():
    raise SystemExit('ci-buddy: root must belong to the installing user')
root.chmod(0o700)
limits = tomllib.loads((root / 'host.conf').read_text())
required = {'jobs', 'slots', 'memory', 'cpus', 'min_free_gb', 'slot_max_gb', 'workers'}
missing = required - limits.keys()
if missing:
    raise SystemExit('ci-buddy: host.conf requires ' + ', '.join(sorted(missing)))
if limits.keys() - required - {'keep_days'}:
    raise SystemExit('ci-buddy: unknown host.conf settings')
for name in ('jobs', 'slots', 'min_free_gb', 'slot_max_gb', 'keep_days'):
    value = limits.get(name, 90) if name == 'keep_days' else limits[name]
    if type(value) is not int or value <= 0:
        raise SystemExit(f'ci-buddy: {name} must be a positive integer')
if not isinstance(limits['memory'], str) or not limits['memory'].strip() or not isinstance(limits['cpus'], str):
    raise SystemExit('ci-buddy: memory and cpus must be strings; only cpus may be empty')
if not isinstance(limits['workers'], list) or any(not isinstance(endpoint, str) or ':' not in endpoint for endpoint in limits['workers']):
    raise SystemExit('ci-buddy: workers must be an array of destination:root strings')
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
    if sys.argv[3] == 'true' and template.name.startswith('ci-buddy-dispatch'):
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
for file in ci-buddy control.py execute.py data.py history.py archive.py workers.py safe_output.py ssh.py auth.py fetch.Containerfile fetch.sh job.sh upkeep.sh; do
    cp "$source_dir/$file" "$root/bin/$file.new"
    chmod +x "$root/bin/$file.new"
    mv "$root/bin/$file.new" "$root/bin/$file"
done
printf '%s\n' "$revision" >"$root/bin/revision.new"
mv "$root/bin/revision.new" "$root/bin/revision"
touch "$root/machine.lock" "$root/turnstile"
instance=$(systemd-escape --path "$root")
if [[ $worker == false ]]; then
    if [[ -n $dispatch_dir ]]; then
        [[ -x $dispatch_dir/dispatch-local && -f $dispatch_dir/dispatch_client/__init__.py ]] || exit 2
        cp "$dispatch_dir/dispatch-local" "$root/bin/dispatch-local.new"
        chmod 700 "$root/bin/dispatch-local.new"
        mv "$root/bin/dispatch-local.new" "$root/bin/dispatch-local"
        cp -R "$dispatch_dir/dispatch_client" "$root/bin/"
    fi
    [[ -x $root/bin/dispatch-local && -f $root/bin/dispatch_client/__init__.py ]] || {
        echo 'ci-buddy: provide the reviewed local dispatch artifact with --dispatch-dir' >&2; exit 2;
    }
    cat >"$root/dispatch.toml" <<'TOML'
heartbeat_interval_s = 1
lease_ttl_s = 30
runner_rate_per_min = 0
sweep_interval_s = 1
[default_lane]
single_attempt = true
[lanes.exclusive]
concurrency = 1
single_attempt = true
TOML
    python3 - "$root" <<'PY'
from pathlib import Path
import sys, tomllib
root = Path(sys.argv[1])
limits = tomllib.loads((root / 'host.conf').read_text())
(root / 'capacity').write_text(str(limits['jobs'] * (1 + len(limits['workers']))) + '\n')
PY
fi
systemctl --user daemon-reload
systemctl --user enable --now "ci-buddy-upkeep@$instance.timer"
if [[ $worker == false ]]; then systemctl --user enable --now "ci-buddy-dispatch@$instance.service"; fi
