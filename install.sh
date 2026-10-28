set -euo pipefail
umask 077
[[ $# == 2 && $1 == --root ]] || { echo 'usage: bash install.sh --root <dir>' >&2; exit 2; }
[[ $(id -u) != 0 ]] || { echo 'ci-buddy requires a rootless user' >&2; exit 2; }
for tool in podman git python3 zstd systemctl systemd-escape; do command -v "$tool"; done
source_dir=$(cd "$(dirname "$0")" && pwd -P)
[[ -f $2/host.conf ]] || { echo 'ci-buddy: write <root>/host.conf with jobs, slots, memory, cpus, min_free_gb, slot_max_gb and workers before installing' >&2; exit 2; }
root=$(cd "$2" && pwd -P)
# Reuse keys need the installed revision even when staging omits Git metadata.
if [[ -e $source_dir/.git ]]; then
    revision=$(git -C "$source_dir" rev-parse --verify HEAD)
elif [[ -f $source_dir/revision ]]; then
    revision=$(<"$source_dir/revision")
else
    echo 'ci-buddy: install from its Git checkout or provide a staging revision file' >&2; exit 2
fi
[[ $revision =~ ^[0-9a-f]{40}$ ]] || { echo 'ci-buddy: invalid installed revision' >&2; exit 2; }
python3 - "$root" "$source_dir" <<'PY'
import hashlib, json, os, pathlib, platform, shutil, stat, subprocess, sys, tomllib, urllib.request
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
for directory in ('bin', 'containers', 'cargo', 'sccache', 'projects', 'pueue'):
    path = root / directory
    path.mkdir(exist_ok=True)
    path.chmod(0o700)
# Only the trusted fetch image writes these shared Cargo-native locks.
for name in ('.package-cache', '.package-cache-mutate'):
    with os.fdopen(os.open(root / 'cargo' / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), 'a') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise SystemExit('ci-buddy: Cargo lock must be a regular file')
checksums = {
    'x86_64': ('c1b10d7e4e62211075ddd0e1dc3e8cbfc5a43d662cb3be7402a28504e23fcb51', '5afeff6adbafb909e8d54e2caff158e6966c2adffa2c09e60fd631cc51b60390'),
    'aarch64': ('759bf5100a51024997111c6913aaf3330a0cdfd893ff552dcf429ae9b5e01e09', '332c5ef74270b64aeaf04894c8c04826f3422eb7d50dbd1a8e0706d74a42f653'),
}
machine = platform.machine()
if machine not in checksums:
    raise SystemExit('ci-buddy: no pinned Pueue binary for this architecture')
for name, expected in zip(('pueue', 'pueued'), checksums[machine]):
    path = root / 'bin' / name
    data = path.read_bytes() if path.exists() else urllib.request.urlopen(f'https://github.com/Nukesor/pueue/releases/download/v4.0.4/{name}-{machine}-unknown-linux-musl').read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f'ci-buddy: {name} checksum differs from pinned release')
    if not path.exists():
        path.write_bytes(data)
    path.chmod(0o755)
config_home = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', pathlib.Path.home() / '.config'))
storage = config_home / 'containers' / 'storage.conf'
storage.parent.mkdir(parents=True, exist_ok=True)
if not storage.exists():
    storage.write_text(f'[storage]\ndriver = "overlay"\ngraphroot = {json.dumps(str(root / "containers"))}\n')
(root / 'pueue.yml').write_text(
    'shared:\n'
    f'  pueue_directory: {json.dumps(str(root / "pueue"))}\n'
    f'  runtime_directory: {json.dumps(str(root / "pueue"))}\n'
    'client:\n  read_local_logs: false\n')
units = config_home / 'systemd' / 'user'
units.mkdir(parents=True, exist_ok=True)
instance = subprocess.check_output(['systemd-escape', '--path', str(root)], text=True).strip()
for template in pathlib.Path(sys.argv[2], 'units').iterdir():
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
for file in ci-buddy data.py history.py archive.py workers.py safe_output.py ssh.py auth.py fetch.Containerfile fetch.sh job.sh upkeep.sh; do
    cp "$source_dir/$file" "$root/bin/$file.new"
    chmod +x "$root/bin/$file.new"
    mv "$root/bin/$file.new" "$root/bin/$file"
done
printf '%s\n' "$revision" >"$root/bin/revision.new"
mv "$root/bin/revision.new" "$root/bin/revision"
touch "$root/machine.lock" "$root/turnstile"
systemctl --user daemon-reload
# A path instance gives every root its own queue and upkeep without changing peers.
instance=$(systemd-escape --path "$root")
queue_unit=ci-buddy-pueued@$instance.service
systemctl --user enable --now "$queue_unit" "ci-buddy-upkeep@$instance.timer"
export PUEUE_CONFIG_PATH="$root/pueue.yml"
python3 - "$root" "$queue_unit" <<'PY'
import getpass, json, pathlib, subprocess, sys, time, tomllib
root = pathlib.Path(sys.argv[1])
pueue = str(root / 'bin' / 'pueue')
socket = root / 'pueue' / f'pueue_{getpass.getuser()}.socket'
while not socket.is_socket():
    subprocess.run(['systemctl', '--user', 'is-active', '--quiet', sys.argv[2]], check=True)
    time.sleep(0.05)
groups = json.loads(subprocess.check_output([pueue, 'group', '--json']))
limits = tomllib.loads((root / 'host.conf').read_text())
# Scheduling lanes do not reserve machines. Each chosen host separately admits
# no more than its jobs limit, even while an endpoint is unavailable at install.
jobs = limits['jobs'] * (1 + len(limits['workers']))
for group, parallel in (('jobs', jobs), ('exclusive', 1)):
    if group not in groups:
        subprocess.run([pueue, 'group', 'add', '--parallel', str(parallel), group], check=True)
    subprocess.run([pueue, 'parallel', '--group', group, str(parallel)], check=True)
PY
