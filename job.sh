set -euo pipefail
root=$1 project=$2 run=$3 out=$5
export PATH="$root/bin:$PATH"
export TMPDIR="$root/containers"
folder=$root/projects/$project
trap 'git -C "$folder/git" update-ref -d "refs/ci-buddy/$run"' EXIT
write_field() { python3 "$root/bin/data.py" write "$out/$1" "$2"; }
phase() { python3 "$root/bin/data.py" phase "$out" "${prefix//-/_}" "$1"; }
prefix=""; [[ ${4:-} != bench ]] || prefix=bench-
values=$(python3 "$root/bin/data.py" host "$root"); eval "$values"
values=$(python3 "$root/bin/data.py" load "$out" "${4:-}"); eval "$values"
free=$(df --output=avail -B1 "$root" | tail -1)
((free >= min_free_gb * 1000000000)) || { echo "disk below ${min_free_gb} GB floor"; exit 2; }
exec 8>&1
exec > >(tee -a "$out/${prefix}runner.log") 2>&1
logger=$!
started=$EPOCHREALTIME
started_field=started_at; [[ -z $prefix ]] || started_field=bench_started_at
finish() {
    status=$?
    git -C "$folder/git" update-ref -d "refs/ci-buddy/$run"
    python3 "$root/bin/data.py" report "$out" "$prefix" "$started" "$EPOCHREALTIME" "$status" || {
        status=1
        python3 "$root/bin/data.py" stamp "$out" "${prefix//-/_}exit" "$status"
    }
    python3 "$root/bin/data.py" read "$out/report.txt" || true
    exec 1>&8 2>&8
    wait "$logger"
}
trap finish EXIT
exec 5>"$root/containers/picker.lock"
flock 5
# Worker capacity must not let local builds exceed this host's jobs limit.
if [[ $exclusive != True ]]; then
    admission=$(python3 "$root/bin/data.py" admit "$root" "$jobs")
    if [[ -n $admission ]]; then
        exec 7>"$admission"
        flock 7
        flock -u 7
    fi
fi
if [[ -n $prefix ]]; then
    order=$(python3 "$root/bin/data.py" read "$out/slot")
    [[ $order =~ ^[1-9][0-9]*$ ]] && ((order <= slots)) || { echo "invalid correctness slot"; exit 1; }
else
    order=$(python3 "$root/bin/data.py" slots "$folder/git" "$commit" "$slots")
fi
chosen=""
for slot in $order; do
    mkdir -p "$folder/slots/$slot"
    exec 3>"$folder/slots/$slot/lock"
    if flock -n 3; then chosen=$slot; break; fi
done
if [[ -z $chosen ]]; then read -r chosen _ <<<"$order"; exec 3>"$folder/slots/$chosen/lock"; flock 3; fi
slot=$folder/slots/$chosen
flock -u 5
write_field slot "$chosen"
echo "slot=$chosen commit=$commit"
exec 6>"$root/turnstile" 4>"$root/machine.lock"
lock_mode=-s; [[ $exclusive != True ]] || lock_mode=-x
# An exclusive waiter holds the turnstile while shared holders drain; newcomers wait.
flock -x 6; flock "$lock_mode" 4; flock -u 6
started=$EPOCHREALTIME
python3 "$root/bin/data.py" stamp "$out" "$started_field" "$started"
phase checkout
mkdir -p "$slot/src"
[[ -d $slot/git ]] || git clone --quiet --bare "$folder/git" "$slot/git"
repository=(git -c "core.hooksPath=$root/bin" -c core.fsmonitor=false -c gc.auto=0 -c maintenance.auto=false
    --git-dir="$slot/git" --work-tree="$slot/src")
"${repository[@]}" fetch --quiet "$folder/git" "$commit" "$base"
"${repository[@]}" checkout --quiet --force --detach "$commit"
"${repository[@]}" clean -fdq -e target -e .git
git -C "$folder/git" update-ref "refs/slots/$chosen" "$commit"
"${repository[@]}" update-ref refs/remotes/origin/main "$base"
python3 "$root/bin/data.py" git-view "$slot/git" "$slot/src/.git"
git -C "$folder/git" update-ref -d "refs/ci-buddy/$run"
mkdir -p "$root/cargo"/{registry,git} "$root/sccache" "$slot/context"
phase image
values=$(python3 "$root/bin/data.py" image "$slot/src" "$slot/context" "$project" "$slot/git"); eval "$values"
exec 7>"$root/containers/$image.lock"
flock 7
if ! podman image exists "$image"; then
    podman build --retry=0 --memory="$memory" --memory-swap="$memory" -t "$image" -f "$slot/context/$containerfile" "$slot/context"
fi
flock -u 7
fetch_hash=$(cat "$root/bin/fetch.Containerfile" "$root/bin/fetch.sh" | sha256sum)
fetch_image="ci-buddy-fetch:${fetch_hash%% *}"
exec 7>"$root/containers/$fetch_image.lock"
flock 7
if ! podman image exists "$fetch_image"; then
    fetch_context=$root/containers/fetch-${fetch_hash%% *}
    mkdir -p "$fetch_context"
    # The credential-bearing image contains installed code, never project files.
    cp "$root/bin/fetch.Containerfile" "$root/bin/fetch.sh" "$fetch_context/"
    podman build --retry=0 --memory="$memory" --memory-swap="$memory" -t "$fetch_image" \
        -f "$fetch_context/fetch.Containerfile" "$fetch_context"
fi
flock -u 7
common=(--rm --init --hostname "$(hostname)" --memory="$memory" --memory-swap="$memory" --pids-limit=8192
    -v "$out:/work/out"
    -e CARGO_HOME=/work/cargo -e RUSTC_WRAPPER=sccache -e SCCACHE_DIR=/work/sccache
    -e "CI_BUDDY_PROJECT=$project" -e "CI_BUDDY_RUN=$run" -e "CI_BUDDY_KIND=$kind"
    -e "CI_BUDDY_COMMIT=$commit" -e "CI_BUDDY_BASE=$base" -e CI_BUDDY_OUT=/work/out -e CI_BUDDY_DATA=/work/data)
# data.py creates and validates these controls before enqueue; every project
# container receives read-only file mounts and cannot replace them.
for control in job.json task slot exit; do
    common+=(-v "$out/$control:/work/out/$control:ro")
done
project_mounts=(-v "$slot/src:/work/src" -v "$root/sccache:/work/sccache" -w /work/src)
# Podman inherits descriptors 3 (slot) and 4 (machine). Its processes keep the
# flock descriptions alive even if the host runner dies; every step inherits both.
phase fetch
token=()
if [[ -e $root/github-token || -L $root/github-token ]]; then
    python3 "$root/bin/auth.py" check
    token=(-v "$root/github-token:/work/token:ro")
fi
podman run "${common[@]}" "${token[@]}" --name "ci-buddy-fetch-$run" --preserve-fds=2 \
    -v "$slot/git:/work/repository:ro" -w /tmp \
    -v "$root/cargo/registry:/work/cargo/registry" -v "$root/cargo/git:/work/cargo/git" \
    -v "$root/cargo/.package-cache:/work/cargo/.package-cache" -v "$root/cargo/.package-cache-mutate:/work/cargo/.package-cache-mutate" \
    -v "$folder/secrets:/run/secrets:ro" \
    -e GIT_CONFIG_GLOBAL=/run/secrets/gitconfig -e 'GIT_SSH_COMMAND=ssh -F /run/secrets/ssh/config' \
    -e CARGO_NET_GIT_FETCH_WITH_CLI=true --entrypoint bash "$fetch_image" /work/fetch.sh "$prefix"
limits=(); [[ -z $cpus ]] || limits=(--cpus "$cpus")
[[ ! -f $root/sccache.env ]] || limits+=(--env-file "$root/sccache.env")
dependencies=(-v "$root/cargo/registry:/work/cargo/registry:ro" -v "$root/cargo/git:/work/cargo/git:ro")
if [[ -n $prepare ]]; then
    phase prepare
    podman run "${common[@]}" "${limits[@]}" --name "ci-buddy-prepare-$run" --preserve-fds=2 \
        "${project_mounts[@]}" "${dependencies[@]}" -v "$folder/data:/work/data" "$image" sh -c '
        eval "$1"; status=$?
        { cat /sys/fs/cgroup/cpu.stat; echo memory_peak_bytes=$(cat /sys/fs/cgroup/memory.peak); cat /sys/fs/cgroup/memory.events; } > "/work/out/${2}prepare-stats"
        exit "$status"' prepare "$prepare" "$prefix"
fi
case $kind in
    test)
        values=$(python3 "$root/bin/data.py" affected "$out" "$slot/git"); eval "$values"
        argv=(bash -c '
            command=$1; affected=$2; shift 2
            listing=${command/nextest run/nextest list}
            # The empty-suite policy applies only to nextest run.
            for policy in fail pass warn; do listing=${listing/ --no-tests $policy/}; done
            bash -c "$listing --message-format json" >"$CI_BUDDY_OUT/all-tests.json" || exit $?
            [[ -z $affected ]] || set -- "$@" -E "$affected"
            bash -c "$listing --message-format json \"\$@\"" list "$@" >"$CI_BUDDY_OUT/selected-tests.json" || exit $?
            bash -c "$command \"\$@\"" test "$@"
        ' test "$test" "$affected" "${argv[@]}") ;;
    gate|bench) argv=(bash -c "${!kind}") ;;
esac
data_mode=ro
if [[ $exclusive == True ]]; then limits+=(--security-opt seccomp=unconfined); data_mode=rw; fi
phase command
podman run "${common[@]}" "${limits[@]}" --name "ci-buddy-$run" --network=none --preserve-fds=2 "${dependencies[@]}" \
    "${project_mounts[@]}" -v "$folder/data:/work/data:$data_mode" -e CARGO_NET_OFFLINE=true "$image" sh -c '
    wrapper=$1; stats=$2; shift 2
    if [ -n "$wrapper" ]; then eval "set -- $wrapper \"\$@\""; fi
    "$@"; status=$?
    # Copy only reports written by this command, while still inside its container.
    for profile in target/nextest/*; do
        junit=$profile/junit.xml
        [ -f "$junit" ] && [ "$junit" -nt /work/out/job.json ] || continue
        profile=${profile##*/}
        mkdir -p "/work/out/junit/$profile"
        cp -p "$junit" "/work/out/junit/$profile/junit.xml" || exit 1
    done
    { cat /sys/fs/cgroup/cpu.stat; echo memory_peak_bytes=$(cat /sys/fs/cgroup/memory.peak); cat /sys/fs/cgroup/memory.events; } > "/work/out/${stats}stats"
    exit "$status"' run "$wrapper" "$prefix" "${argv[@]}"
