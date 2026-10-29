# Private build workers

Clients use one owner-configured coordinator through tailworker's pinned private
OpenSSH transport. Runtime workers use dispatch-service's authenticated PEP and
CAS over Tailscale. Never use Tailscale SSH for automation. Machine creation,
accounts, tailnet policy, host firewalls, cloud lifetimes and network-denial checks
belong to tailworker. Installing ci-buddy does not establish those controls; a
two-root transport fixture does not prove isolation across machines. Jobs have
no provisioning authority. Keep workers disabled until those controls are verified.

The client never downloads executable code. Use the same reviewed revision on the
coordinator and workers. The installed tailworker transport validates private DNS,
pinned host keys, forwarding restrictions and one bounded connection attempt.
Submitted jobs have no timeout or retry. Optional `<root>/ssh_config` is passed to
tailworker for owned-private-file validation. Client bundles share
`~/.local/lib/ci-buddy/ssh_config`; native Windows rejects that optional POSIX file,
so use WSL when needed. Otherwise ordinary owner SSH configuration applies.

Roots are owner-only and installation is rootless. A host's queues share its
machine lock; installation refuses conflicting locks. Lanes are not reservations:
each execution host enforces jobs, slots, disk floor and container memory caps.
A gate retains one native lease through correctness and profiling on the same
worker and slot. The coordinator's app API uses an owner-private Unix socket.
Only authenticated PEP ports are published on the configured private address;
dispatcher and CAS backend ports stay inside the rootless pod namespace.
Each consumer generates its own credential. Workload containers receive neither
that credential, a container socket nor the coordinator's issuer key.
Cancellation waits for owned-container cleanup and application persistence;
failed or disconnected workers are never automatically replayed or declared clean.

Native progress comes from the installed executor's private file, outside workload
mounts. Logs are opaque and cannot establish a successful step. Native job IDs
identify execution; internal source-publication nonces are not execution IDs.
Returned normalized step results are bounded data, never programs. Import rejects
unsafe paths, links, special files, invalid numbers, JUnit DTDs and malformed
history. Workloads cannot overwrite coordinator identity or private job metadata.
Their metadata mount is read-only; artifacts use other files. Raw step output
lives only in the runner-owned work directory and is removed after execution.

Only the installed trusted fetch image receives project secrets. Keep
`projects/NAME/secrets` directories 0700 and files 0600. Its recipe and entrypoint
are independent of project images, toolchains and Cargo settings. Only registry
and Git cache directories and native Cargo locks are shared; mutable Cargo-home
configuration is absent. Deploy keys are read-only. Prepare has network and no
secrets; run is offline. Fetch, prepare and image-build network denials belong
to tailworker's machine boundary, not the offline workload container.

`auth` sends a GitHub token to the coordinator and owner-approved fetch_hosts over
the same pinned SSH transport. Submitted source cannot add recipients. The token
is stored outside projects in `github-token`, mode 0600, and mounted read-only
only in fetch. The client retains none. Git's helper supplies it without argv or
source URLs and uses HTTPS for GitHub while preserving other providers. Select
fine-grained repository access, Contents read-only and an expiry in GitHub; the
tool cannot infer permissions from the string. Rotate with `auth`, updating
unavailable hosts before enabling them. No cloud secret service is involved.

Approved machines remain trusted. Their owning accounts can use stored credentials;
a compromised host, kernel, SSH implementation or container runtime remains
outside these application checks. Workloads receive no authority to enroll workers.
