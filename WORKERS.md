# Owner-managed workers

Normal submissions automatically use available approved machines. The owner
creates paid capacity explicitly:

```sh
bash "$HOME/.local/bin/ci-buddy" worker create project/zone/instance --profile "$HOME/.config/ci-buddy-worker"
```

This command creates one VM, waits for its reviewed first boot, installs the
locally pinned runtime, enrolls its separate identity, checks access and registers
it last. `worker pair` runs enrollment alone for a ready VM from the same template.
No job or server command can create a VM. The current provisioning profile is
Ubuntu 26.04 on Compute Engine; owner commands require Linux/WSL and the Google
CLI. The job client continues to support Windows.

## Prepare once

Use an owner-controlled dedicated cloud project/network, no external VM address,
no attached service account, outbound NAT and owner-only IAP tunnel permission.
Review the effective cloud firewall rules, including inherited rules. Allow IAP
management SSH only from its documented proxy range. The approved first boot
installs root-owned host restrictions; VPC rules do not filter provider metadata
or traffic inside the VM. Builds and tests always remain rootless.

Keep the existing tailnet. Export its full approved policy as native JSON and
preserve unrelated applications' grants, groups, SSH rules and tests. Existing laptop/coordinator device identities remain unchanged. CI workers use
a separate `tag:ci-worker`, owned only by your literal owner login. Merge the
fragment from `worker.policy.expected_policy(owner, peer_addresses)` once if
needed; it permits the approved existing peer addresses to worker SSH. Do not
replace a production tailnet with this fragment. Native CI tests check those
existing peer identities and reverse worker denials.
Owner device approval, account MFA and private SSH keys remain trust roots.

Create one dedicated coordinator transport key outside every checkout, in its
root's `secrets/worker-transport`; retain the private half there and use its public
half below. The worker receives public owner/coordinator keys only. Existing
application worker identities, credentials and state are never copied.

`worker profile` exports native instance-template, cloud-firewall and full tailnet
JSON and creates the owner's local SSH key. Source image IDs and disk sizing are
explicit. Obtain the immutable image self-link/ID and effective firewall JSON
through the authenticated provider API and approve them once. For example:

```sh
bash "$HOME/.local/bin/ci-buddy" worker profile \
  --output "$HOME/.config/ci-buddy-worker" --owner "$OWNER_LOGIN" --tailnet "$TAILNET" \
  --account "$WORKER_ACCOUNT" --root "$WORKER_ROOT" --host-conf host.conf \
  --image "$IMAGE_SELF_LINK" --image-id "$IMAGE_ID" --machine-type "$MACHINE_TYPE" \
  --disk-size-gb "$DISK_GB" --subnetwork "$SUBNETWORK_SELF_LINK" --firewalls firewalls.json \
  --policy-file tailnet-policy.json --owner-addresses "$OWNER_PRIVATE_ADDRESSES" \
  --coordinator-addresses "$COORDINATOR_PRIVATE_ADDRESSES" --dns-addresses "$DNS_ADDRESSES" \
  --coordinator "$COORDINATOR_ENDPOINT" --coordinator-key-file coordinator.pub --credentials
```

Profiles containing secrets must live outside Git repositories. Credentials are
prompted without echo and stored in owned mode-0600 files. The optional GitHub
token stays local; use a credential scoped to the repositories being enrolled.
The Tailscale credential must be an API access key/token, not a join key or OAuth
client secret; restrict its permissions to the required CI key/device operations
and policy reads wherever the provider supports those scopes. Google credentials
remain in the owner's normal Google CLI store. No private key or token enters
startup metadata, image layers, argv or job logs.

For an explicitly approved private GitHub dependency, add
`--dependency owner/repository` to create/pair while in its Rust project checkout
(or supply `--project name`). A new worker key is generated in that project's
fetch-only secrets and registered read-only through the owner's local GitHub
credential. Public projects need no dependency key. Other private providers still
use their reviewed project secrets provisioning; this GitHub helper does not
guess another provider's credential policy.

## Admission and failure

Enrollment independently checks native cloud metadata, the immutable image,
network interfaces, no service account, exact approved startup/firewall recipes,
and effective cloud firewalls. Google-delivered guest host keys establish the
first pin only for this freshly approved template, before untrusted jobs. Guest
attributes are not attestation of a used worker or its firewall. IAP provides the
initial owner-authenticated path; ordinary job SSH continues to reject proxies.

The owner API must return the exact approved full tailnet policy before/after
native access tests. Failed tests in an HTTP-success response are failures.
Existing grants remain intact. CI tests cover named operations and do not prove
every possible port; the owner's complete-policy approval and independent
template denial checks in [SECURITY.md](SECURITY.md) still apply.

Private SSH, wrong-key denial, rootless Podman and network denials must pass
before registration. Rollback is limited to identities and keys created by that
enrollment; existing nodes cannot be deleted by a supplied worker address.
No real cloud deployment or cross-machine isolation is implied by fixture tests.

Creation retains private per-profile/project claims and rejects another CI VM.
An ambiguous failure is never retried or followed by automatic VM deletion:
the VM may exist and keep billing, and the owner must inspect it. Enrollment
revokes its new node/keys on failure where possible and reports unresolved
cleanup. This is a guard on this command, not a provider-wide spending cap.
Stopping a VM is an explicit owner action and can leave chargeable disks.
Ordinary jobs never start paid machines because all configured hosts are down.
