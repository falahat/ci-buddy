set -euo pipefail
root=$1 project=$2 run=$3
export PATH="$root/bin:$PATH"
export PUEUE_CONFIG_PATH="$root/pueue.yml"
export TMPDIR="$root/containers"
folder=$root/projects/$project
out=$folder/runs/$run
prefix=""; [[ ${4:-} != bench ]] || prefix=bench-
values=$(python3 "$root/bin/data.py" host "$root"); eval "$values"
values=$(python3 "$root/bin/data.py" load "$out" "${4:-}"); eval "$values"
started=$EPOCHREALTIME
started_field=started_at; [[ $kind != bench ]] || started_field=bench_started_at
python3 "$root/bin/data.py" stamp "$out" "$started_field" "$started"
finish() {
    status=$?
    python3 "$root/bin/data.py" report "$out" "$prefix" "$started" "$EPOCHREALTIME" "$status"
    if [[ $status == 0 && $kind == gate && -n ${bench:-} ]]; then
        python3 "$root/bin/data.py" stamp "$out" bench_queued_at "$EPOCHREALTIME"
        id=$(pueue add --print-task-id --group exclusive --after "$(<"$out/task")" --label "$project/$run/bench" --escape -- bash "$root/bin/job.sh" "$root" "$project" "$run" bench)
        echo "$id" >"$out/bench-task"
        echo "bench_task=$id"
    fi
    cat "$out/report.txt"
    echo "$status" >"$out/${prefix}exit"
}
trap finish EXIT
free=$(df --output=avail -B1 "$root" | tail -1)
((free >= min_free_gb * 1000000000)) || { echo "disk below ${min_free_gb} GB floor"; exit 2; }
exec 5>"$folder/picker.lock"
flock 5
order=$(python3 "$root/bin/data.py" slots "$folder/git" "$commit" "$slots")
chosen=""
for slot in $order; do
    mkdir -p "$folder/slots/$slot"
    exec 3>"$folder/slots/$slot/lock"
    if flock -n 3; then chosen=$slot; break; fi
done
if [[ -z $chosen ]]; then read -r chosen _ <<<"$order"; exec 3>"$folder/slots/$chosen/lock"; flock 3; fi
slot=$folder/slots/$chosen
flock -u 5
echo "$chosen" >"$out/slot"
echo "slot=$chosen commit=$commit"
[[ -d $slot/src/.git ]] || git clone --quiet --no-checkout "$folder/git" "$slot/src"
git -C "$slot/src" fetch --quiet "$folder/git" "$commit"
git -C "$slot/src" checkout --quiet --force --detach "$commit"
git -C "$slot/src" clean -fdq
git -C "$folder/git" update-ref "refs/slots/$chosen" "$commit"
git -C "$slot/src" update-ref refs/remotes/origin/main "$base"
git -C "$folder/git" update-ref -d "refs/ci-buddy/$run"
mkdir -p "$root/cargo"/{registry,git} "$root/sccache" "$slot/context"
values=$(python3 "$root/bin/data.py" image "$slot/src" "$slot/context" "$project"); eval "$values"
exec 6>"$root/turnstile" 4>"$root/machine.lock"
lock_mode=-s; [[ $exclusive != True ]] || lock_mode=-x
flock -x 6; flock "$lock_mode" 4; flock -u 6
exec 7>"$root/containers/$image.lock"
flock 7
if ! podman image exists "$image"; then
    podman build --memory="$memory" --memory-swap="$memory" -t "$image" -f "$slot/context/$containerfile" "$slot/context"
fi
flock -u 7
common=(--rm --init --hostname "$(hostname)" --memory="$memory" --memory-swap="$memory" --pids-limit=8192
    -v "$slot/src:/work/src" -v "$out:/work/out" -v "$root/sccache:/work/sccache" -w /work/src
    -e CARGO_HOME=/work/cargo -e RUSTC_WRAPPER=sccache -e SCCACHE_DIR=/work/sccache
    -e "CI_BUDDY_PROJECT=$project" -e "CI_BUDDY_RUN=$run" -e "CI_BUDDY_KIND=$kind"
    -e "CI_BUDDY_COMMIT=$commit" -e "CI_BUDDY_BASE=$base" -e CI_BUDDY_OUT=/work/out -e CI_BUDDY_DATA=/work/data)
podman run "${common[@]}" --name "ci-buddy-fetch-$run" --preserve-fds=2 \
    -v "$root/cargo:/work/cargo" -v "$folder/data:/work/data" -v "$folder/secrets:/run/secrets:ro" \
    -e GIT_CONFIG_GLOBAL=/run/secrets/gitconfig -e 'GIT_SSH_COMMAND=ssh -F /run/secrets/ssh/config' \
    -e CARGO_NET_GIT_FETCH_WITH_CLI=true "$image" sh -c '
    cargo fetch --locked && cargo metadata --locked --no-deps --format-version 1 > /work/out/metadata.json && eval "$1"
    status=$?
    { cat /sys/fs/cgroup/cpu.stat; echo memory_peak_bytes=$(cat /sys/fs/cgroup/memory.peak); cat /sys/fs/cgroup/memory.events; } > "/work/out/${2}fetch-stats"
    exit "$status"' fetch "$prepare" "$prefix"
case $kind in
    test) argv=(bash -c "$test \"\$@\"" test "${argv[@]}") ;;
    gate|bench) argv=(bash -c "${!kind}") ;;
esac
limits=(); [[ -z $cpus ]] || limits=(--cpus "$cpus")
data_mode=ro
if [[ $exclusive == True ]]; then limits+=(--security-opt seccomp=unconfined); data_mode=rw; fi
podman run "${common[@]}" "${limits[@]}" --name "ci-buddy-$run" --network=none --preserve-fds=2 \
    -v "$root/cargo/registry:/work/cargo/registry:ro" -v "$root/cargo/git:/work/cargo/git:ro" \
    -v "$folder/data:/work/data:$data_mode" -e CARGO_NET_OFFLINE=true "$image" sh -c '
    wrapper=$1; stats=$2; shift 2
    if [ -n "$wrapper" ]; then eval "set -- $wrapper \"\$@\""; fi
    "$@"; status=$?
    { cat /sys/fs/cgroup/cpu.stat; echo memory_peak_bytes=$(cat /sys/fs/cgroup/memory.peak); cat /sys/fs/cgroup/memory.events; } > "/work/out/${stats}stats"
    exit "$status"' run "$wrapper" "$prefix" "${argv[@]}"
