set -euo pipefail
[[ $# == 2 && $1 == --root ]] || { echo 'usage: bash install.sh --root <dir>' >&2; exit 2; }
[[ $(id -u) != 0 ]] || { echo 'ci-buddy requires a rootless user' >&2; exit 2; }
for tool in podman git python3 zstd systemctl; do command -v "$tool"; done
source_dir=$(cd "$(dirname "$0")" && pwd -P)
mkdir -p "$2"
root=$(cd "$2" && pwd -P)
python3 - "$root" <<'PY'
import hashlib, json, os, pathlib, platform, shutil, sys, tomllib, urllib.request
root = pathlib.Path(sys.argv[1])
config = root / 'host.conf'
if not config.exists():
    config.write_text('jobs = 3\nslots = 4\nmemory = "28g"\ncpus = ""\nmin_free_gb = 500\nslot_max_gb = 100\nkeep_days = 90\nworkers = []\n')
limits = tomllib.loads(config.read_text())
if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
    raise SystemExit('ci-buddy: free disk is below min_free_gb')
for directory in ('bin', 'containers', 'cargo', 'sccache', 'projects', 'pueue'):
    (root / directory).mkdir(exist_ok=True)
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
if pathlib.Path(tomllib.loads(storage.read_text())['storage']['graphroot']).resolve() != root / 'containers':
    raise SystemExit('ci-buddy: existing Podman storage.conf points elsewhere')
(root / 'pueue.yml').write_text('shared:\n  pueue_directory: ' + json.dumps(str(root / 'pueue')) + '\n  runtime_directory: ' + json.dumps(str(root / 'pueue')) + '\nclient:\n  read_local_logs: false\n')
units = config_home / 'systemd' / 'user'
units.mkdir(parents=True, exist_ok=True)
for name, body in {
    'pueued.service': f'[Unit]\nDescription=ci-buddy queue\n[Service]\nType=exec\nExecStart="{root}/bin/pueued" --config "{root}/pueue.yml"\nRestart=no\n[Install]\nWantedBy=default.target\n',
    'ci-buddy-upkeep.service': f'[Unit]\nDescription=ci-buddy upkeep\n[Service]\nType=oneshot\nExecStart=bash "{root}/bin/upkeep.sh"\n',
    'ci-buddy-upkeep.timer': '[Unit]\nDescription=Daily ci-buddy upkeep\n[Timer]\nOnCalendar=daily\nPersistent=true\n[Install]\nWantedBy=timers.target\n',
}.items():
    (units / name).write_text(body.replace('%', '%%'))
link = pathlib.Path.home() / '.ci-buddy'
if link.is_symlink() and link.resolve() != root:
    raise SystemExit('ci-buddy: existing home link points elsewhere')
if not link.exists():
    link.symlink_to(root, target_is_directory=True)
PY
[[ $(podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}') == 'true v2' ]] || {
    echo 'ci-buddy requires rootless Podman with cgroup v2' >&2; exit 2;
}
for file in ci-buddy data.py job.sh upkeep.sh; do
    cp "$source_dir/$file" "$root/bin/$file.new"
    chmod +x "$root/bin/$file.new"
    mv "$root/bin/$file.new" "$root/bin/$file"
done
touch "$root/machine.lock" "$root/turnstile"
systemctl --user daemon-reload
systemctl --user enable --now pueued.service ci-buddy-upkeep.timer
export PUEUE_CONFIG_PATH="$root/pueue.yml"
python3 - "$root" <<'PY'
import getpass, json, pathlib, subprocess, sys, time, tomllib
root = pathlib.Path(sys.argv[1])
pueue = str(root / 'bin' / 'pueue')
socket = root / 'pueue' / f'pueue_{getpass.getuser()}.socket'
while not socket.is_socket():
    subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'pueued.service'], check=True)
    time.sleep(0.05)
groups = json.loads(subprocess.check_output([pueue, 'group', '--json']))
jobs = tomllib.loads((root / 'host.conf').read_text())['jobs']
for group, parallel in (('jobs', jobs), ('exclusive', 1)):
    if group not in groups:
        subprocess.run([pueue, 'group', 'add', '--parallel', str(parallel), group], check=True)
    subprocess.run([pueue, 'parallel', '--group', group, str(parallel)], check=True)
PY
