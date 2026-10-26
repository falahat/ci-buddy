# Owner-managed workers

All machines run the same `install.sh` and rootless container runner. Existing
owner-administered hosts keep their queues, caches and native benchmarks.
There is no local VM supervisor, separate worker OS or migration machinery.
Approved SSH workers join a coordinator through `host.conf`; normal submissions
use available slots without creating paid machines.

## Prepare once

Keep the existing tailnet. Give `tag:ci-worker` only to separate worker devices,
with one TCP-22 grant from approved laptop/coordinator identities to that tag.
Workers get no grants back to submitters, other workers or the LAN. In the
console check that no catch-all grant, sharing, subnet or exit-route approval
bypasses this boundary. Preserve other applications and existing device identities.
No policy export, Tailscale administrator token or automated policy comparison
is required. Keep owner accounts protected by MFA and device approval.

Use a dedicated private cloud project/subnet with outbound NAT, no external VM
address or service account, and owner-only provisioning/serial-console IAM.
Review its ingress/egress rules once; a tailnet rule does not govern ordinary
VPC or LAN traffic. Budget alerts are not a hard spending cap. Ordinary jobs
cannot create, start or replace cloud instances.

## Create a Google worker

Run the owner command from Linux/WSL with the normal authenticated Google CLI.
Obtain a single-use, ephemeral, pre-tagged `tag:ci-worker` join key in the
Tailscale console. Save it without echo in an owned mode-0600 file inside an
owner-only `secrets` directory outside Git. Prepare the ordinary leaf host.conf
with explicit jobs, slots, memory, cpus, disk floors and `workers = []`.

```sh
bash "$HOME/.local/bin/ci-buddy" worker create PROJECT/ZONE/NAME \
  --machine-type "$MACHINE_TYPE" --subnet "$PRIVATE_SUBNET" \
  --disk-size-gb "$DISK_GB" --root "$WORKER_ROOT" \
  --host-conf worker.conf --auth-key-file "$SECRETS/join-key" \
  --pubkey-file coordinator.pub
```

Creation makes one explicit billable instance-creation call. Its public startup
script installs standard dependencies, a dedicated account and restricted SSH.
The join secret travels only through Google's authenticated serial connection,
after a fresh public challenge confirms terminal echo is disabled. It never
enters startup metadata, argv, environment, images or logs. Both attempted join
files are consumed even on failure. There is no IAP, guest-attribute API,
image-ID approval or firewall-export profile.

The same bound channel supplies the initial public SSH pin. Strict private SSH
installs the locally reviewed runtime archive and runs ordinary `install.sh`.
Startup/serial metadata is removed afterward. Generated transport keys and pins
remain in the owner's secrets directory. Additional public-key files authorize
other approved submitting machines without copying their private keys.

The command prints its endpoint and private SSH-config path. After the network
checks in [SECURITY.md](SECURITY.md), merge that block into the coordinator's
owned mode-0600 `<root>/ssh_config`, add `"NAME:WORKER_ROOT"` to `workers` in
host.conf and reinstall the coordinator to update Pueue's scheduling capacity.
The worker needs independently provisioned read-only project fetch credentials;
only the trusted fetch container receives them. Removing its entry and
reinstalling disables dispatch to it.

A local project claim and existing-instance check prevent this command from
silently making another CI VM. Ambiguous failures retain the claim and report
that the VM may still be billing. No VM is retried or automatically deleted;
inspect it explicitly. This is not a provider-wide spending cap.

No real cloud worker has been deployed or its cross-machine isolation verified
by fixture tests. Keep cloud workers disabled until the owner's access checks
pass. Owner/admin credentials never enter the worker.
