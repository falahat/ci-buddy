set -euo pipefail
out=$1 destination=$2
mkdir -p "$(dirname "$destination")"
temporary=$(mktemp "$destination.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
(
    cd "$out"
    find . -type f ! -perm /111 -print0 |
        tar --null --verbatim-files-from --no-recursion -T - -I 'zstd -3 -q' -cf "$temporary"
)
mv -- "$temporary" "$destination"
