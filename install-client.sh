set -euo pipefail
umask 077
source_dir=$(cd "$(dirname "$0")" && pwd -P)
endpoint=
while (($#)); do
    [[ $1 == --host && $# -ge 2 ]] || { echo 'usage: bash install-client.sh [--host <destination>[:<root>]]' >&2; exit 2; }
    endpoint=$2; destination=${endpoint%%:*}; root=.ci-buddy
    [[ $endpoint != *:* ]] || root=${endpoint#*:}
    [[ $destination =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || exit 2
    [[ $root == .ci-buddy || $root == /* ]] || exit 2
    [[ $endpoint != *$'\n'* && $endpoint != *$'\r'* ]] || exit 2
    shift 2
    (($# == 0)) || { echo 'one coordinator serves this fleet' >&2; exit 2; }
done
if [[ -e $source_dir/.git ]]; then
    git -C "$source_dir" diff --quiet HEAD -- || {
        echo 'install the client from a committed, reviewed checkout' >&2; exit 2;
    }
    revision=$(git -C "$source_dir" rev-parse --verify HEAD)
else revision=$(<"$source_dir/revision"); fi
[[ $revision =~ ^[0-9a-f]{40}$ ]] || exit 2
bundle=$HOME/.local/lib/ci-buddy/$revision
mkdir -p "$bundle" "$HOME/.local/bin"
for name in ci-buddy safe_output.py ssh.py auth.py; do cp "$source_dir/$name" "$bundle/$name"; done
printf '%s\n' "$revision" >"$bundle/revision"
printf 'exec bash %q "$@"\n' "$bundle/ci-buddy" >"$HOME/.local/bin/ci-buddy"
chmod 700 "$HOME/.local/bin/ci-buddy"
if [[ -n $endpoint ]]; then
    git config --global --replace-all ci-buddy.host "$endpoint"
fi
printf 'installed trusted client %s; invoke bash "$HOME/.local/bin/ci-buddy"\n' "$revision"
