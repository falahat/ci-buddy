# ci-buddy

A rootless Podman build-and-test tool for Rust projects, using dispatch-service's queue.

Install with `bash install.sh --root <directory> --dispatch-dir <reviewed-bundle>` on Linux with rootless Podman,
cgroup v2, Git, Python 3.11+, zstd and systemd user services. Enable user lingering.
First write `<root>/host.conf` with jobs, slots, memory, cpus (empty for uncapped),
min_free_gb, slot_max_gb and workers (an empty array until workers are wanted).
keep_days defaults to 90. The reviewed bundle contains dispatch-local and its
dispatch_client Python package; installation never downloads them. The installer
refuses missing configuration and selects Podman storage only if none is configured. Each root has its own
private Unix-socket dispatch queue and systemd upkeep instances. The first links `~/.ci-buddy`; later roots
leave that link alone. The queue service restarts on failure without retrying jobs.

Copy `example/ci-buddy.toml` to a workspace and set name, host, main, image and test.
Optional prepare, wrapper, gate and bench commands belong to the project. Images
supply sh, Rust, sccache, cargo-nextest and cargo-sweep, plus ssh for private fetches.
Only listed image files enter the build context.

Set host to an SSH destination, optionally followed by `:<root>` for another root.
Install the reviewed client with `bash install-client.sh --host <destination>:<root>`.
Repeat `--host` for each approved coordinator; `local:<root>`
uses a local Linux queue without SSH. Endpoints live in the owner's global
Git configuration (`ci-buddy.host`), outside submitted source. The project's
`host` is a preference within that allowlist. The client skips unavailable or
incompatible hosts before submission, then its coordinator picks an available
warm worker. Use the same reviewed revision everywhere and update explicitly.
A submission never downloads executable client code from a server.

Invoke `bash "$HOME/.local/bin/ci-buddy"` (or add that directory to PATH):
`run [--exclusive] -- <command>`, `test [arguments]`, `gate`, `verify <commit>`,
`merge`, `status`, `log <run>` or `cancel <run>`. Tracked edits use Git stash objects;
output streams and the exit status returns without polling or automatic retries.
`status` and `history` show each available endpoint with provenance. `log`,
`cancel`, `verify` and `merge` locate runs or gates across available endpoints.
Unavailable history is identified, not silently treated as empty. After submit
starts, a disconnect never causes automatic resubmission.
A detached HEAD records an empty branch. Candidate refs disappear after checkout
or when a job ends before checkout, including failed submissions and cancellation.

A green gate runs its optional exclusive bench in the same dispatch job and output folder.
Correctness returns first. `verify` waits for both and prints their report.
`merge` verifies clean HEAD, rejects a moved main and pushes without forcing.
Exclusive jobs drain the machine, make data writable and allow CPU/NUMA profiling.
`gate --exclusive` also serializes correctness for an isolated build measurement.

Unlocked slots rank by commit distance, unknown last, then lowest number. Targets
stay warm. Host Git metadata stays outside the writable checkout; jobs get a copy.
For private GitHub sources, create a fine-grained token for selected repositories
with Contents read-only and an expiry, then run `ci-buddy auth`. Its hidden prompt
accepts the token once and distributes it over pinned private SSH to approved
hosts and their configured workers. No GitHub CLI or Secret Manager is needed.
The token stays in each host's owner-only root as `github-token` (0600);
the client keeps only its SHA-256 fingerprint in owner Git configuration.
New or previously offline hosts obtain that token from an available approved
host before submission; workers receive it before dispatch. A lost connection
is reported without retry. Repeat `auth` to replace an expired or revoked token.
GitHub enforces repository permissions; enrollment cannot prove a token is
read-only, so select that permission when creating it.

Only the trusted fetch container mounts the token read-only at `/work/token`.
Git supplies it through a credential helper, never a source URL or argument.
Token mode fetches GitHub SSH URLs over HTTPS, preserving other providers' settings.
Prepare and run receive no token. A compromised host or its owning OS account
can read the stored token; approved hosts therefore remain trusted.

For other Git hosts, put gitconfig and optional SSH configuration, pinned host keys and key in
`projects/NAME/secrets` (directories 0700, files 0600). Only fetch has network and
secrets, and executes only Cargo's fetch and metadata commands. Optional prepare
runs next with network, writable data and no secrets. Run is offline with
dependency caches/data read-only except exclusive data.
Containers inherit slot/machine locks, enforce a memory cap and check the disk floor.
Output reads reject links and nonregular files. Queue time includes machine admission.
Reports separate queue, checkout, image, fetch, prepare and command time and label
unaccounted time. CPU covers fetch/prepare/run; peak memory includes file cache.
Missing statistics are marked incomplete; image-build CPU is excluded.

Each project keeps SQLite history, including nextest JUnit results and failed
phase excerpts. Ask `history test <name>`, `history failures --branch <branch>`
(or `--range <base>..<tip>`), `history slowest [--since 7d]`,
`history change <ref>`, `history flaky` or `history gates [--days 7]`.
Logs use zstd archives; upkeep expires artifacts after keep_days and retains rows.

Identical tests and gates reuse finished results or follow an existing task,
naming its source run. The key covers kind, tree, arguments, the base of an
unfiltered test, and the installed tool revision. `test --rerun` and `gate --rerun`
force execution. Arbitrary `run --` commands always execute.

Test candidates in an independently installed root while the shared root runs main.
Check with `python3 check.py` and `python3 e2e.py <destination>:<private-root>`.

An unfiltered `test` compares the candidate with its main merge base (or
`--since <ref>`), then uses nextest rdeps for changed packages. Shared paths
select all tests; explicit nextest arguments keep the caller's selection.
Reports give touched packages and runnable selected/total counts.
Hourly upkeep locks each idle slot and runs cargo-sweep with seven-day age and
slot_max_gb limits. Busy slots are skipped; maintenance respects exclusive work.

Workers are SSH destinations with roots in host.conf's workers array. Install
ci-buddy there first, satisfy [the private access requirements](SECURITY.md),
enroll the token with `auth` (or provision project fetch secrets). Install leaf
runners with `--worker`; only coordinators run dispatch-service. Free worker
slots join the commit-distance choice; the worker locks and executes directly.
Output streams back and its bounded archived run returns to the
coordinator. Cancellation follows the worker. Exclusive work and gates with a
bench remain on their coordinator so executables and comparison data share a
host. There is no automatic retry or cloud provisioning. Optional sccache.env
is passed to prepare/run containers; never store project fetch credentials there.
To exercise workers in the end-to-end test, give e2e.py a second coordinator
endpoint configured to use the first endpoint as its worker.

Install the same commit on coordinator and workers. The coordinator owns reuse;
a newly dispatched request executes once on the chosen worker under the original queue lease.

Job transport SSH permits only private, loopback or tailnet addresses, with independently pinned
host keys and separate machine keys. Proxies, forwarding and local SSH commands
are disabled. Only connection establishment has a five-second bound; running
jobs have no timeout and are never retried. Worker returns are bounded data,
cannot overwrite local job identity, and are never executed. These controls do
not configure host firewalls, cloud ingress or tailnet policy. Keep remote
workers disabled until the owner verifies the denial checks in SECURITY.md.

Fetch runs in a separately built trusted image with a fixed Cargo toolchain,
using a safe snapshot of tracked files and ignoring project Cargo/rustup config.
Project images never receive credentials. Reviewed registry settings may live
in the owner's `secrets/cargo-config.toml`; ordinary source configuration is
restored implicitly by running builds in the normal checkout. Control metadata
inside the output directory is read-only; use other filenames for artifacts.
Reuse belongs to the selected coordinator; independent submissions to different
coordinators during a partition can create independent runs.

## Adding a tailworker machine

Machine creation, accounts and private networking belong to
tailworker; follow its README and complete the owner's access checks first.
ci-buddy runs the same installer and container runner on every machine.

On the control host, the owner first reviews a dry run, then creates the
machine explicitly:

```sh
tailworker up gcp --name "$name" --hours 1 --dry-run
```

After creation, install the reviewed ci-buddy client used by the coordinator.
Keep the reviewed `tailworker` command on the control host's owner PATH. Prepare
a leaf `worker.conf` with explicit resource/disk limits and `workers = []`, then:

```sh
ci-buddy enroll "$name" --root "$worker_root" --host-conf worker.conf
```

The command resolves the existing logical machine, sends only the local
reviewed runner archive, verifies readiness, synchronizes the enrolled fetch
token and adds the leaf to an approved coordinator's workers. The coordinator
defaults to the first owner `ci-buddy.host`; `--coordinator destination:root`
selects another approved endpoint. Its own SSH configuration must already have
the worker alias, approved authentication and host pin; enrollment copies no
keys. A local leaf must share the coordinator's machine.

New roots must be empty; existing roots must have the same reviewed revision
and host settings. Coordinator updates change host.conf and native dispatch capacity
without reinstalling the runtime or restarting work. Failed registration restores changed
configuration and removes its temporary authentication route. Existing routes and
credentials remain. Normal jobs can use the free leaf through the existing
commit-distance picker; exclusive work and gates with a bench retain their
coordinator locality. Offline workers fall back before acceptance. Ordinary
submissions never create or start machines.

The dispatch daemon has no TCP listener. Native job keys, claims, waiting and
cancellation own queue lifecycle; ci-buddy owns source, containers and history.
Capacity increases use SIGHUP. A failed registration restores configuration and
the capacity file; any already-added idle consumers remain until an idle restart.
Per-host admission still enforces physical limits. A gate returns correctness
early, while verify waits for the entire native job and its CI exit status.
