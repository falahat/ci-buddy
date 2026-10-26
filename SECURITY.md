# Private worker setup

ci-buddy uses SSH, not a public job service. The queue installer does not configure
your network, SSH server, cloud account or firewall. The separate owner-side
[worker setup](WORKERS.md) prepares only approved dedicated worker images and
checks the existing tailnet policy; it never replaces unrelated policy. Keep workers disabled until
the access-denial checks below pass. A same-machine transport test cannot prove
cross-machine isolation. Only approved devices and their private SSH keys may
submit jobs or inspect logs; code you authorize on those devices has that power.

On each Linux host, use a dedicated unprivileged account, rootless Podman and
cgroup v2. Enable its systemd user services and write `<root>/host.conf` with
explicit jobs, slots, memory, cpus, min_free_gb, slot_max_gb and workers. Start
with `workers = []`, then run `bash install.sh --root <root>`. The installer
requires an owned root, makes it private, and joins other roots on the same
machine to the first root's machine locks. It refuses to replace differing
existing locks: drain that root before an owner repairs them.

Pueue has `jobs * (1 + configured worker count)` scheduling lanes. Those are
not running-container reservations: each host independently enforces its own
jobs and slots. Installation does not contact workers, so an offline endpoint
does not block setup and does not need reinstalling when it returns.

Use ordinary OpenSSH over a private network such as Tailscale. Do not use
Tailscale SSH for job automation: that substitutes device identity for the
separate local SSH key. Keep your identity-provider account protected by MFA,
enable device approval, and let only your owner identity assign CI tags. No
CI node is shared; no CI node advertises subnet or exit routes or uses Funnel.
Keep existing laptop/coordinator identities. Merge only the narrow CI worker
fragment into the owner-approved existing policy, preserving unrelated apps:

```json
{
  "tagOwners": {"tag:ci-worker": ["<owner-login>"]},
  "grants": [{
    "src": ["<approved-laptop-tailnet-ip>", "<approved-coordinator-tailnet-ip>"],
    "dst": ["tag:ci-worker"],
    "ip": ["tcp:22"]
  }]
}
```

This is a fragment, not a replacement for the whole tailnet policy. The owner
approves the complete existing policy export. Enrollment compares it with the
owner API and runs native access tests using the approved peer IP identities.

Tag only explicitly approved machines. In an existing network, audit the whole
policy before merging these rules: grants are additive, and a broad existing
allow rule defeats these restrictions. Preserve unrelated services through
explicit rules that do not also authorize CI endpoints. The worker must not
initiate connections to submitters, the coordinator, other workers or the LAN.
Replies on an established SSH connection still work. A submitter needs no
listener or callback; enable its shields-up setting when it is not a worker.

The owner configures the worker's SSH server with private listen addresses and
only the CI account. The following is a dedicated-server template, not a file
to overwrite another application's SSH configuration with:

```text
ListenAddress <private-IPv4>
ListenAddress <private-IPv6>
AllowUsers <ci-account>
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
DisableForwarding yes
PermitTTY no
PermitUserRC no
```

Give each submitting machine its own revocable key. In the CI account's
`~/.ssh/authorized_keys`, authorize its public key with the native restrictions
`restrict,from="<approved-submitter-private-address>"`; use a separate entry for
each submitter. Keep private keys in host secrets outside every job mount.
Pin host keys independently during setup. Never disable host-key checking or
accept a replacement key supplied by the worker being checked. The client must
disable agent, TCP and X11 forwarding and local SSH commands.
The shared transport enforces pinned host keys, private destination addresses
and disabled SSH proxies; it refuses public endpoints. Those checks do not
establish the tailnet and host-firewall denials below.

An optional `<root>/ssh_config` supplies native OpenSSH configuration through
`-F` without changing global SSH files. The installed local client reads
`~/.local/lib/ci-buddy/ssh_config`, shared by its revision bundles.
It must be an owned regular file with no group/other permissions and no symlink
(normally mode 0600). When absent,
OpenSSH uses its usual configuration. Native Windows cannot verify that POSIX
ownership contract and rejects the optional file; use WSL for that setup.

Host firewalls enforce the same allowlist on both IPv4 and IPv6 and deny worker
connections through ordinary LAN interfaces too. Expose no public CI SSH,
dashboard, Pueue endpoint, database, container socket or metrics port, and no
router forwarding. For cloud workers use a private VPC, no external IPv4 or
IPv6 address, deny unsolicited ingress, and outbound NAT for required network
access. Do not assign a cloud service account or instance-creation privileges.

Run containers have no network. Image build, fetch and prepare need networking;
their host networking rules must permit only required public package downloads
and designated DNS, blocking private networks, host services, tailnet addresses
and cloud metadata on both IP versions. Worker hosts hold no submitter key.
Project deploy keys are read-only and fetch-only. Normal jobs cannot provision
machines. Owner-side creation does not impose a provider-wide billing cap; retain at most the owner's authorized
instance count, and remember that stopped instances may still have disk costs.

The credential-bearing fetch image is built from ci-buddy's installed recipe
and script alone, using a separate content hash and image lock. No project
image, ENTRYPOINT or tool runs with the key mounted. Only registry and Git
cache subdirectories are mounted into the fetch Cargo home, so mutable cached
Cargo-home configuration is absent. Its two regular native lock files are
shared too, retaining Cargo's concurrent-fetch locking. Prepare and run use the project image
without credentials; fetch uses the trusted entrypoint and neutralizes project
toolchain and Cargo configuration before invoking its installed toolchain.

Before enabling an endpoint, the owner verifies:

1. Approved submission, streaming, history and cancellation succeed using the
   example project. The actual SSH host fingerprint matches its independent pin.
2. An unapproved tailnet device and an approved device with the wrong SSH key
   cannot submit or read history. Public internet access fails over IPv4 and IPv6.
3. The worker cannot initiate connections to submitters, the coordinator, other
   workers or home subnets; build/fetch/prepare cannot reach those or metadata.
4. Effective SSH configuration has the restrictions above and listeners bind
   only approved private addresses. Audit the full tailnet and host-firewall
   policies; a successful SSH connection alone proves none of these denials.
5. Removing a key/device blocks new connections. End its existing SSH sessions
   explicitly. Do not terminate unrelated jobs or sessions.

Never execute returned worker files. The trusted local client and coordinator
validate result data and do not accept worker-provided commands or job identity.
No local preflight can prove a remote administrator's firewall or cloud policy;
failure or unavailable evidence leaves that worker disabled. This setup limits
access and the consequences of a hostile worker, not all possible vulnerabilities
in approved devices, SSH, the kernel or the container runtime.
