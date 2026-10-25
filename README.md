# ci-buddy

A rootless Podman/Pueue build-and-test queue for Rust projects.

Install with `bash install.sh --root <directory>` on Linux with rootless Podman,
cgroup v2, Git, Python 3.11+, zstd and systemd user services. Enable user lingering.
First write `<root>/host.conf` with jobs, slots, memory, cpus (empty for uncapped),
min_free_gb, slot_max_gb and workers (an empty array until workers are wanted).
keep_days defaults to 90. The installer refuses missing configuration, pins Pueue,
and selects Podman storage only if none is configured. Each root has its own
systemd queue and upkeep instances. The first links `~/.ci-buddy`; later roots
leave that link alone. The queue service restarts on failure without retrying jobs.

Copy `example/ci-buddy.toml` to a workspace and set name, host, main, image and test.
Optional prepare, wrapper, gate and bench commands belong to the project. Images
supply sh, Rust, sccache, cargo-nextest and cargo-sweep, plus ssh for private fetches.
Only listed image files enter the build context.

Set host to an SSH destination, optionally followed by `:<root>` for another root.
Fetch that root's `bin/ci-buddy` over SSH each time and invoke it with Bash:
`run [--exclusive] -- <command>`, `test [arguments]`, `gate`, `verify <commit>`,
`merge`, `status`, `log <run>` or `cancel <run>`. Tracked edits use Git stash objects;
output streams and the exit status returns without polling or automatic retries.
A detached HEAD records an empty branch. Candidate refs disappear after checkout
or when a job ends before checkout, including failed submissions and cancellation.

A green gate queues its optional exclusive bench in the same output folder.
Correctness returns first. `verify` waits for both and prints their report.
`merge` verifies clean HEAD, rejects a moved main and pushes without forcing.
Exclusive jobs drain the machine, make data writable and allow CPU/NUMA profiling.

Unlocked slots rank by commit distance, unknown last, then lowest number. Targets
stay warm. Host Git metadata stays outside the writable checkout; jobs get a copy.
Put gitconfig and optional SSH configuration, pinned host keys and key in
`projects/NAME/secrets` (directories 0700, files 0600). Only fetch has network and
secrets, and executes only Cargo's fetch and metadata commands. Optional prepare
runs next with network, writable data and no secrets. Run is offline with
dependency caches/data read-only except exclusive data.
Containers inherit slot/machine locks, enforce a memory cap and check the disk floor.
Output reads reject links and nonregular files. Queue time includes machine admission.
Reports separate queue, checkout, image, fetch, prepare and command time and label
unaccounted time. CPU covers fetch/prepare/run; peak memory includes file cache.
Missing statistics are marked incomplete; image-build CPU is excluded.

Test candidates in an independently installed root while the shared root runs main.
Check with `python3 check.py` and `python3 e2e.py <destination>:<private-root>`.
