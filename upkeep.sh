set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd -P)
export PATH="$root/bin:$PATH"
values=$(python3 "$root/bin/data.py" host "$root"); eval "$values"
for project in "$root/projects/"*; do
    [[ -d $project ]] || continue
    # Retention expires disposable artifacts, never the SQLite result rows.
    find "$project/runs" -mindepth 1 -maxdepth 1 -type d -mmin "+$((keep_days * 1440))" -exec rm -rf -- {} +
    find "$project/logs" -type f -name '*.tar.zst' -mmin "+$((keep_days * 1440))" -delete
    for slot in "$project/slots/"*; do
        [[ -d $slot/src/target ]] || continue
        (
            exec 3>"$slot/lock"
            flock -n 3 || { echo "busy_slot=$slot skipped"; exit 0; }
            exec 6>"$root/turnstile" 4>"$root/machine.lock"
            # Maintenance follows the same machine admission as builds.
            flock -x 6; flock -s 4; flock -u 6
            free=$(df --output=avail -B1 "$root" | tail -1)
            ((free >= min_free_gb * 1000000000)) || exit 2
            image=$(podman images --filter "reference=ci-buddy-${project##*/}:*" --sort created --format '{{.Repository}}:{{.Tag}}' | head -1)
            [[ -n $image ]] || exit 0
            read -r total _ < <(du -sb "$slot")
            read -r target _ < <(du -sb "$slot/src/target")
            budget=$((slot_max_gb * 1000000000 - total + target))
            ((budget > 0)) || { echo "slot non-target data exceeds limit: $slot"; exit 1; }
            limits=(); [[ -z $cpus ]] || limits=(--cpus "$cpus")
            podman run --rm --init \
                --memory="$memory" --memory-swap="$memory" --pids-limit=8192 --network=none --preserve-fds=2 \
                "${limits[@]}" -v "$slot/src:/work/src" -w /work/src \
                -v "$root/cargo/registry:/work/cargo/registry:ro" -v "$root/cargo/git:/work/cargo/git:ro" \
                -e CARGO_HOME=/work/cargo -e CARGO_NET_OFFLINE=true "$image" \
                sh -c 'cargo sweep --time 7 && cargo sweep --maxsize "$1" && du -sb target' upkeep "${budget}B"
            du -sb "$slot"
        )
    done
    while read -r image; do
        podman image rm "$image" || true
    done < <(podman images --filter "reference=ci-buddy-${project##*/}:*" --sort created --format '{{.Repository}}:{{.Tag}}' | tail -n +4)
done
