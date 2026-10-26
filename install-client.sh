set -euo pipefail
umask 077
source_dir=$(cd "$(dirname "$0")" && pwd -P)
hosts=()
while (($#)); do
    [[ $1 == --host && $# -ge 2 ]] || { echo 'usage: bash install-client.sh [--host <destination>[:<root>]]...' >&2; exit 2; }
    endpoint=$2; destination=${endpoint%%:*}; root=.ci-buddy
    [[ $endpoint != *:* ]] || root=${endpoint#*:}
    [[ $destination =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || exit 2
    [[ $root == .ci-buddy || $root == /* ]] || exit 2
    [[ $endpoint != *$'\n'* && $endpoint != *$'\r'* ]] || exit 2
    hosts+=("$endpoint"); shift 2
done
if [[ -e $source_dir/.git ]]; then
    git -C "$source_dir" diff --quiet HEAD -- || {
        echo 'install the client from a committed, reviewed checkout' >&2; exit 2;
    }
    git -C "$source_dir" ls-files --error-unmatch worker/enroll.py worker/create.py worker/profile.py worker/policy.py worker/bootstrap.sh worker/firewall.nft.in >/dev/null
    revision=$(git -C "$source_dir" rev-parse --verify HEAD)
else revision=$(<"$source_dir/revision"); fi
[[ $revision =~ ^[0-9a-f]{40}$ ]] || exit 2
bundle=$HOME/.local/lib/ci-buddy/$revision
mkdir -p "$bundle" "$HOME/.local/bin"
for name in ci-buddy safe_output.py ssh.py; do cp "$source_dir/$name" "$bundle/$name"; done
mkdir -p "$bundle/worker"
for name in enroll.py create.py profile.py policy.py bootstrap.sh firewall.nft.in; do
    cp "$source_dir/worker/$name" "$bundle/worker/$name"
done
python3 - "$source_dir" "$bundle/runtime.tar" "$revision" <<'PY'
import io, pathlib, sys, tarfile
source, destination = map(pathlib.Path, sys.argv[1:3])
names = ('ci-buddy data.py history.py archive.py workers.py safe_output.py ssh.py '
         'fetch.Containerfile fetch.sh job.sh upkeep.sh install.sh install-client.sh').split()
names += ['units/' + name for name in ('ci-buddy-pueued@.service', 'ci-buddy-upkeep@.service', 'ci-buddy-upkeep@.timer')]
names += ['worker/' + name for name in ('enroll.py', 'create.py', 'profile.py', 'policy.py', 'bootstrap.sh', 'firewall.nft.in')]
with tarfile.open(destination, 'w') as archive:
    for name in names:
        path = source / name
        if path.is_symlink():
            raise ValueError('reviewed client bundle contains a link')
        archive.add(path, arcname=name, recursive=False)
    value = (sys.argv[3] + '\n').encode()
    entry = tarfile.TarInfo('revision'); entry.size = len(value); entry.mode = 0o600
    archive.addfile(entry, io.BytesIO(value))
PY
printf '%s\n' "$revision" >"$bundle/revision"
printf 'exec bash %q "$@"\n' "$bundle/ci-buddy" >"$HOME/.local/bin/ci-buddy"
chmod 700 "$HOME/.local/bin/ci-buddy"
for endpoint in "${hosts[@]}"; do
    if ! git config --global --fixed-value --get-all ci-buddy.host "$endpoint"; then
        git config --global --add ci-buddy.host "$endpoint"
    fi
done
printf 'installed trusted client %s; invoke bash "$HOME/.local/bin/ci-buddy"\n' "$revision"
