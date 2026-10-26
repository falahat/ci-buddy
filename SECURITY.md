# Private worker setup

ci-buddy uses SSH, not a public job service. Installing it does not configure
your network, SSH server, cloud account or firewall. Keep workers disabled until
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
For a dedicated CI network, the complete Tailscale policy is:

```json
{
  "tagOwners": {
    "tag:ci-submitter": ["<owner-login>"],
    "tag:ci-coordinator": ["<owner-login>"],
    "tag:ci-worker": ["<owner-login>"]
  },
  "grants": [
    {
      "src": ["tag:ci-submitter"],
      "dst": ["tag:ci-coordinator", "tag:ci-worker"],
      "ip": ["tcp:22"]
    },
    {
      "src": ["tag:ci-coordinator"],
      "dst": ["tag:ci-worker"],
      "ip": ["tcp:22"]
    }
  ],
  "ssh": []
}
```

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
Project deploy keys are read-only and fetch-only. ci-buddy neither provisions
machines nor imposes a hard billing cap; retain at most the owner's authorized
instance count, and remember that stopped instances may still have disk costs.

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
