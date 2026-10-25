# ci-buddy

A small build-and-test queue for Rust projects, using rootless Podman and Pueue.
Invoke its shell scripts with Bash.

Install with `bash install.sh --root <directory>` on Linux with rootless
Podman, delegated cgroup v2, Git, Python 3.11+, zstd and systemd user services.
It installs pinned Pueue binaries, a user daemon and daily upkeep, and selects
the container store only when no storage configuration exists. Enable user
lingering so the queue survives logout. The root is linked at `~/.ci-buddy`.

Set the limits in `host.conf`: jobs, slots, memory, optional cpus, min_free_gb,
slot_max_gb, keep_days and workers. Workers are not enabled yet. The queue has
jobs and exclusive groups. Projects have independent repositories, slots,
data, secrets, runs and logs below the root's projects directory.

Copy `example/ci-buddy.toml` into a Rust workspace and set its name, host, main,
image files and test command. Optional prepare and wrapper commands belong to
the project. The image must supply sh, Rust, sccache, cargo-nextest and
cargo-sweep. Only listed image files enter its build context.

From the project, run `bash <client> run [--exclusive] -- <command>`,
`test [arguments]`, `gate`, `verify <commit>`, `merge`, `status`, `log <run>`
or `cancel <run>`. Fetch the client over SSH from
`~/.ci-buddy/bin/ci-buddy` each time. Candidates include tracked edits through
Git stash objects. Output streams through Pueue; the client returns the job's
exit status. There is no client polling or automatic retry.

The project's gate command reports correctness first. On success, its optional
bench command queues in the exclusive group with the same output folder.
`verify` waits for both and prints their report; a missing or red result fails.
`merge` verifies tracked-clean HEAD, refuses if main has moved since its gate,
and pushes that exact commit to origin's main branch without forcing.
Exclusive jobs drain the machine through the turnstile, make project data
writable and disable seccomp for CPU/NUMA pinning and profiling.

Jobs choose the unlocked slot with the nearest commit; unknown slots rank
last and ties use the lowest number. The checkout and target stay warm.
Fetch alone mounts secrets and has network access. Put its gitconfig and
optional ssh configuration, pinned host keys and key under project secrets,
with directory mode 0700 and files 0600. Run has no network or secrets, and
sees dependency caches and project data read-only. Every job checks free disk,
has a memory cap, and inherits slot/machine locks into the container.
Reports include queue/run seconds, fetch/run CPU seconds, peak memory and OOM
counts; missing statistics are marked incomplete. Image-build CPU is excluded.

Run `python3 check.py` and `python3 e2e.py <host>` to check the source boundary
and drive the example through output, exit status, tests, isolation and cancel.

