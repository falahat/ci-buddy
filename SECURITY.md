# Private build workers

Clients contact their approved coordinator over pinned private OpenSSH. Workers
use dispatch-service's existing PEP, signed per-request proofs and authenticated
CAS over Tailscale. Never use Tailscale SSH for automation. Machine creation, accounts,
tailnet policy, host firewalls, cloud lifetimes and network-denial checks belong
to tailworker; follow its README before enabling an endpoint. Installing
ci-buddy does not establish those controls, and a two-root transport test does
not verify isolation across machines. Jobs have no provisioning authority.

Use the same reviewed revision on every endpoint. The trusted local client
never downloads executable code from a server. SSH destinations must resolve
only to private, loopback or tailnet addresses. Host keys are pinned; proxies,
forwarding, local SSH commands and agent forwarding are disabled. Connection
establishment has one bounded attempt; submitted jobs have no timeout or retry.

An optional `<root>/ssh_config` supplies owned native OpenSSH configuration.
The client reads `~/.local/lib/ci-buddy/ssh_config`, shared by revision bundles.
It must be a regular file owned by the user, without group/other permissions
or a symlink. Native Windows rejects this optional POSIX configuration; use
WSL when it is needed. Without it, ordinary owner SSH configuration applies,
including tailworker's alias and known-host files.

Roots are owner-only and installation is rootless. A host's queues share its
machine lock; installing refuses conflicting existing locks. Scheduling lanes
are not reservations: each execution host enforces its own jobs, slots, disk
floor and container memory cap. Offline endpoints do not block installation.
Exclusive work can use any enrolled machine; a gate's benchmark is pinned to its
correctness worker. The coordinator's app API is an owner-private Unix socket.
Only the authenticated PEP ports are published, on the configured private address;
dispatcher and CAS backend ports remain inside the rootless pod namespace.
Each consumer has its own credential, generated on that worker. The trusted
installed executor launches isolated rootless workload containers; it never gives
them the runner credential, container socket or coordinator issuer key.
Cancellation retains the lease until cleanup and application persistence finish.
A crashed worker fails visibly; it is never automatically resubmitted or declared clean.

Only the trusted installed fetch image receives project secrets. Keep them in
`projects/NAME/secrets`, directories 0700 and files 0600. The fetch recipe and
entrypoint are independent of project images, toolchains and Cargo settings.
Only registry and Git cache directories and their native Cargo locks are shared;
mutable Cargo-home configuration is absent. Deploy keys are read-only. Prepare
uses the project image with networking and no credentials; runs are offline.
Network restrictions for fetch, prepare and image builds are tailworker's
machine boundary, not a property proved by ci-buddy's offline run container.

`auth` enrolls a GitHub token over the same pinned private SSH transport.
It is stored outside projects at the owner-only root's `github-token`, mode 0600,
and mounted read-only only in fetch. The client retains no token.
Configured host pools and host.conf fetch_hosts authorize distribution;
submitted source cannot add destinations. Fetch supplies the token through Git's
credential helper and rewrites GitHub SSH URLs to HTTPS. Create a fine-grained
token with selected repositories and Contents read-only; GitHub enforces these
permissions, which the tool cannot infer from the token string. Expiry/revocation
is managed in GitHub; repeat `auth` for replacement. There is no Secret Manager
or automatic cloud enrollment.

Worker returns are bounded data and are never executed. Result import rejects
links, special files, unsafe names, control characters and invalid history.
Workers cannot overwrite coordinator job identity or control metadata. Jobs
see their control metadata read-only and use separate filenames for artifacts.
Only the selected coordinator owns result reuse and retained history.

Existing owner-administered hosts remain trusted. Approved device code can use
its owner's job credentials. A compromised host, kernel, SSH implementation or
container runtime remains outside these application checks. Keep workers
disabled until the owner has verified tailworker's access and network denials.
