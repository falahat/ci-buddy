# ci-buddy

A rootless Podman build-and-test tool for Rust projects, using dispatch-service's queue.

Install with `bash install.sh --root <directory> --dispatch-dir <reviewed-bundle>`
on Linux with rootless Podman, cgroup v2, Git, Python 3.11+, zstd, tailworker and
systemd user services. Enable user lingering. The reviewed bundle contains the
native runner, dispatch_client Python package and remote-runner kit; installation
never downloads them. Write `<root>/host.conf` with jobs, slots, memory, cpus
(empty for uncapped), min_free_gb, slot_max_gb and fetch_hosts (initially empty).
keep_days defaults to 90. Installation preserves warm caches and existing machine
locks, refuses missing configuration, and selects Podman storage only if absent.
The first root links `~/.ci-buddy`; later roots leave that link alone.

Write owner-controlled `dispatch/plane.json` with address, runner_port, cas_port,
dispatcher_image, auth_image and cas_image, using reviewed local image identities.
`bash <root>/bin/plane.sh <root> init` starts the standard dispatcher, authenticated
PEP and CAS in a rootless Podman pod. Only PEP ports are published on the private
address; backend ports stay inside its namespace. Each root has its own private
Unix app socket and systemd upkeep instances.

Install the reviewed client with `bash install-client.sh --host <destination>:<root>`.
Exactly one coordinator lives in the owner's global Git setting `ci-buddy.host`;
`local:<root>` uses a local Linux coordinator without SSH. Submitted source cannot
choose a destination. Install tailworker on the client for pinned private SSH.
Use the same reviewed revision everywhere and update explicitly; the client never
downloads executable code from a server or resubmits after an ambiguous disconnect.

Copy `example/ci-buddy.toml` to a workspace and set name, main, image and test.
Optional prepare, wrapper, gate and bench commands belong to the project. Images
supply sh, Rust, sccache, cargo-nextest and cargo-sweep. Only listed image files
enter the build context. Host Git metadata stays outside the writable checkout;
jobs get a copy. Unlocked slots rank by commit distance, unknown last, then number.

Invoke `bash "$HOME/.local/bin/ci-buddy"` or add its directory to PATH:
`run [--exclusive] -- <command>`, `test [arguments]`, `gate`, `bench`,
`verify <commit>`, `merge`, `status`, `log <job>` or `cancel <job>`.
Public job IDs are native `j_` identifiers. Output streams with the command's exit
status and no job timeout or retry. Tracked edits use Git stash objects; detached
HEAD records an empty branch. Candidate refs disappear after source publication
or failed pre-submission validation. Cancellation waits for owned-container cleanup;
a failed worker remains visibly unconfirmed rather than being declared clean.
Without a validated result, the CLI fails and history keeps the workload exit unknown.

A gate runs correctness and optional exclusive profiling in one native job on the
same worker and cached slot. Correctness prints its result immediately; `status`
shows trusted native progress while profiling continues. `gate` and `verify` wait
for the complete result and fail if either step fails. Reports retain each step's
timings and CPU accounting. `merge` verifies clean HEAD, rejects a moved main and
pushes without forcing. Exclusive work drains the machine, makes data writable
and permits CPU/NUMA profiling. `gate --exclusive` also isolates correctness.

An unfiltered `test` compares the candidate with its main merge base, or
`--since <ref>`, then uses nextest rdeps for changed packages. Shared paths select
all tests; explicit nextest arguments keep the caller's selection. Reports give
touched packages and runnable selected/total counts. Identical tests and gates
follow a live task or return the existing finished job ID and report. The reuse
key covers kind, tree, arguments, the base of an unfiltered test and tool revision.
`--rerun` forces configured tests, gates or benchmarks to execute; arbitrary
`run --` commands always execute.

For private GitHub sources, create a fine-grained token for selected repositories
with Contents read-only and an expiry, then run `ci-buddy auth`. Its hidden prompt
sends the token to the coordinator and its approved fetch_hosts over pinned SSH.
Each stores `github-token` outside projects with mode 0600; the client retains none.
Only trusted fetch mounts it read-only at `/work/token`, using Git's credential
helper and HTTPS for GitHub while preserving other providers' settings. Prepare
and run receive no token. Repeat `auth` to rotate it; unavailable fetch hosts make
distribution visibly incomplete and must be updated before enabling them. GitHub
enforces permissions; the tool cannot infer read-only access from a token string.
Approved hosts and their owning OS accounts remain trusted with stored secrets.

For other Git hosts, put gitconfig and optional SSH configuration, pinned host keys
and deploy key in `projects/NAME/secrets` (directories 0700, files 0600). Fetch uses
a separate trusted image, fixed Cargo toolchain and safe tracked snapshot, ignoring
project Cargo/rustup configuration. Reviewed registry settings can live in
`secrets/cargo-config.toml`. Only fetch receives secrets. Prepare uses the project
image with network and writable data; run is offline with dependencies and data
read-only except exclusive data. Optional sccache.env reaches prepare/run only;
never put fetch credentials there. Output control metadata is read-only.

Containers inherit slot/machine locks and enforce memory and free-disk limits.
Reports separate queue, checkout, image, fetch, prepare and command time, labelling
unaccounted time. CPU covers fetch/prepare/run; peak memory includes file cache.
Missing statistics are incomplete; image-build CPU is excluded. Native CAS carries
source, opaque logs and validated normalized step results. The coordinator retains
SQLite history, nextest JUnit results and failed phase excerpts before acknowledging
completion. Ask `history test <name>`, `history failures --branch <branch>` or
`--range <base>..<tip>`, `history slowest [--since 7d]`, `history change <ref>`,
`history flaky` or `history gates [--days 7]`. Hourly upkeep skips busy slots,
respects exclusive work, and uses cargo-sweep with seven-day age and slot_max_gb.
The coordinator stores each validated result document once in SQLite and retains
opaque compressed log archives for keep_days. It never extracts those archives;
history remains after log expiry and worker cleanup.

Enroll an existing approved tailworker with
`ci-buddy enroll NAME --root DIRECTORY --host-conf FILE`. The coordinator installs
its reviewed executor and uses dispatch-service's shared kit to create unique native
worker credentials. Normal consumers and one exclusive consumer use revision-scoped
lanes. The dispatcher chooses an available consumer; its executor chooses a warm
slot. There is no SSH job transport, automatic job retry or cloud provisioning.
Inspect partial setup before repeating enrollment; fetch_hosts governs token rotation.
Workers stay disabled until the owner verifies tailworker's [network denials](SECURITY.md).
Test candidates in a separate installed root while the shared root runs main:
`python3 check.py` and `python3 e2e.py <destination>:<private-root>`.
