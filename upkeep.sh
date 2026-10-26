set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd -P)
export PATH="$root/bin:$PATH"
export PUEUE_CONFIG_PATH="$root/pueue.yml"
values=$(python3 "$root/bin/data.py" host "$root"); eval "$values"
for project in "$root/projects/"*; do
    [[ -d $project ]] || continue
    # Retention expires disposable artifacts, never the SQLite result rows.
    find "$project/runs" -mindepth 1 -maxdepth 1 -type d -mmin "+$((keep_days * 1440))" -exec rm -rf -- {} +
    find "$project/logs" -type f -name '*.tar.zst' -mmin "+$((keep_days * 1440))" -delete
    while read -r image; do
        podman image rm "$image" || true
    done < <(podman images --filter "reference=ci-buddy-${project##*/}:*" --sort created --format '{{.Repository}}:{{.Tag}}' | tail -n +4)
done
pueue clean
