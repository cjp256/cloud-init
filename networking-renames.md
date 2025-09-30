# Guidance for Azure Linux VM Networking

**Applies to:** Azure Linux virtual machines, on distributions
using systemd ≥ 245 (Ubuntu 20.04+, Debian 11+, RHEL 9+,
SLES 15+, and equivalent derivatives).

**Audience:** Azure Linux VM operators, image publishers, and
support engineers responsible for image build, provisioning,
or networking configuration.

**Scope:** Configuration of NICs visible inside the guest —
interface naming, MAC + driver matching, route metrics, DNS,
IPv6, primary-NIC selection, and the `set-name` / udev rename
race that affects multi-NIC VMs. Out of scope: Azure-side
networking (VNets, NSGs, load balancers, route tables,
accelerated networking enablement at the NIC resource level).

## Summary

Two approaches to network configuration on an Azure Linux VM —
the [Rules](#rules) below apply to either:

- **[Cloud-init managed networking](#cloud-init-managed-networking)
  — the default.** Cloud-init regenerates `/etc/netplan/50-
  cloud-init.yaml` from IMDS on every boot. By default it
  renames interfaces into IMDS order with `set-name`; this can
  be turned off with `apply_network_config_set_name: false`,
  which is expected to ship in cloud-init 26.2 and which
  eliminates the per-boot rename churn and gives stable
  kernel-assigned secondary names.
- **[Self-managed networking](#self-managed-networking-cloud-init-disabled)
  — cloud-init networking disabled.** Disable cloud-init
  networking and write a self-managed configuration (netplan,
  `.link`, `.network`, NetworkManager, or `systemd-networkd`). Use this when
  business or operational requirements call for specific
  interface names or non-default networking, or until the
  datasource option above ships in cloud-init 26.2.

In both approaches the primary NIC's name is stable.
Secondary `ethN` names are stable across reboots when
cloud-init runs with `apply_network_config_set_name: false`,
and stable by construction in a self-managed configuration.
They are *not* stable in the default cloud-init configuration:
IMDS does not guarantee secondary ordering, and cloud-init
renumbers secondaries to match IMDS on every boot.

Neither approach guarantees that a particular kernel
interface name maps to a particular NIC by host contract —
the kernel/VMBus probe order is stable in practice but is not
guaranteed. Matching each configuration stanza by MAC +
driver makes the policy independent of which name a NIC ends
up with.

## Background: how Azure presents NICs to Linux

A multi-NIC Azure VM presents the following devices to the guest:

| NIC kind | Driver | Notes |
|---|---|---|
| Synthetic (primary and secondary) | `hv_netvsc` | Hyper-V VMBus NICs. Always named `ethN` in VMBus probe order (predictable naming does not apply over VMBus). The lowest-probed synthetic NIC is `eth0` and is the primary NIC. |
| Accelerated networking VF | `mlx5_core` or `mana` | SR-IOV VF paired with each synthetic NIC over PCI. Gets a predictable name (e.g. `enP1s1`). Transparently enslaved to the synthetic NIC (`master` symlink in sysfs) — **do not configure directly**; the VF shares its synthetic NIC's MAC. |
| MANA-only NIC (future SKUs) | `mana` | Attached directly over PCI (no synthetic layer). Gets a predictable name (e.g. `ens1`, `ens1d1`) derived from PCI BDF and `dev_port`. |

## Rules

These rules apply to both cloud-init managed and self-managed
configurations. Cloud-init enforces them automatically; a
self-managed configuration must encode them by hand.

- **Match each NIC by `macaddress` and `driver`, not by
  interface name.** Pin `driver: hv_netvsc` on the synthetic
  NIC stanza. A MAC-only match would non-deterministically
  bind to the accelerated-networking VF, which shares the
  synthetic NIC's MAC. On future mana-only VM sizes that
  present `mana` NICs directly with no synthetic layer and no
  VF bonding, `driver: mana` is optional — include it for
  symmetry if desired.
- **Do not configure the accelerated-networking VF directly.**
  The VF is transparently enslaved to its synthetic NIC; only
  the synthetic stanza is needed.
- **Do not rename into the kernel `eth*` namespace.** No
  `set-name: eth1`, `eth2`, … — it races with the kernel and is
  unsupported upstream. `set-name` rename targets must use a
  non-reserved prefix (not `eth`, `en`, `wl`, `ww`); see [What
  is not supported](#what-is-not-supported) and [Failure mode:
  rename collisions](#failure-mode-rename-collisions).
  Renaming into `eth*` only works through cloud-init managed
  networking, which uses a two-phase `cirename<N>` placeholder
  to avoid the race.
- **The primary NIC's name must be `eth0`, or otherwise
  natural-sort before every secondary name.** To find the
  primary, cloud-init picks `eth0` if it exists, and otherwise
  picks the lowest naturally-sorted NIC name (for example
  `ens1` from `ens1`, `ens1d1`, `ens1d2`). It then DHCPs that
  one NIC and attempts to connect to IMDS; if that NIC is not
  the primary, and there may be a delay in booting while attempting
  to unsuccesfully contact IMDS for latest instance metadata. See
  [References](#cloud-init-mechanism-and-fix) for the
  selection logic.

## Cloud-init managed networking

Cloud-init regenerates `/etc/netplan/50-cloud-init.yaml` (or the
distro equivalent) from IMDS on every boot, matches each NIC by
`macaddress` and `driver`, and applies any renames through an
internal two-phase placeholder so the upstream udev rename race
is avoided. See [References](#cloud-init-mechanism-and-fix) for
the implementation.

The `driver:` match is required because on Azure the synthetic
NIC (`hv_netvsc`) and its accelerated-networking VF
(`mlx5_core` / `mana`) share a MAC; a MAC-only match would
non-deterministically bind to the VF. Cloud-init pins the
synthetic driver automatically as needed.

### Default behaviour

By default, cloud-init emits `set-name` directives to renumber
the kernel `ethN` interfaces into IMDS order: the primary
becomes `eth0`, the second IMDS entry becomes `eth1`, and so on.

The primary NIC always ends up as `eth0` because IMDS guarantees
it is the first entry returned. The order of the remaining NICs,
however, is not guaranteed stable: IMDS may enumerate secondaries
in a different order on the next boot, in which case cloud-init
will rename them differently and a given secondary's `ethN` name
will change.

### Disabling `set-name` (`apply_network_config_set_name: false`)

Azure-datasource option added in
[canonical/cloud-init#6807](https://github.com/canonical/cloud-init/pull/6807)
and expected to ship in **cloud-init 26.2**. When set to
`false`, cloud-init keeps networking enabled and still matches
each interface by MAC + driver — but emits no `set-name`, so
each interface keeps the name the kernel assigned it. Default
is `true` to preserve historical behaviour.

The primary NIC is still `eth0` in practice because the VMBus
bus probes the primary synthetic NIC first; secondaries keep
stable kernel-assigned names across reboots (the kernel's VMBus
/ PCI probe order is stable in practice on Azure). The per-boot
rename churn of the default behaviour is eliminated and the
upstream udev rename race cannot be triggered at all.

Enable on the Azure datasource:

```yaml
# /etc/cloud/cloud.cfg.d/10-azure-no-rename.cfg
datasource:
  Azure:
    apply_network_config_set_name: false
```

With this setting cloud-init emits, for each NIC:

- A stanza keyed by `enx<MAC>` (MAC without colons — an
  identifier only; does not rename the device).
- `match: { macaddress, driver }`.
- No `set-name`.
- `dhcp4-overrides.route-metric` set to `(idx + 1) * 100` so the
  primary wins the default route.
- `dhcp4-overrides.use-dns: false` on every non-primary interface
  (DNS through secondary NICs is not supported on Azure).
- `dhcp6: true` or `false` per IMDS, depending on whether IPv6
  is configured for the subnet.

Not yet available in any released cloud-init — until 26.2
ships, [Self-managed networking](#self-managed-networking-cloud-init-disabled)
is the way to produce the equivalent layout. See
[References](#cloud-init-mechanism-and-fix) for the reference
implementation.

## Self-managed networking (cloud-init disabled)

Use this when the cloud-init-managed layouts above do not fit —
for example because business-meaningful interface names are
required, or other custom policies / configuration is required
that cloud-init does not directly support.

Disable cloud-init networking:

```
# /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
network: {config: disabled}
```

Remove any cloud-init-generated netplan files
(`/etc/netplan/50-cloud-init.yaml`,
`/etc/netplan/90-cloud-init-*.yaml`) before writing the
replacement. The configuration must encode every rule in
[Rules](#rules) by hand.

### Recommended

These settings replicate what cloud-init would emit:

- Set the DHCP route metric to `(idx + 1) * 100` so the primary
  wins the default route. Netplan: `dhcp4-overrides.route-metric`.
  `systemd-networkd`: `RouteMetric=` under `[DHCPv4]`.
- Suppress DNS servers from non-primary interfaces. Netplan:
  `dhcp4-overrides.use-dns: false`. `systemd-networkd`:
  `UseDNS=false` under `[DHCPv4]`. (DNS through secondary NICs
  is not supported on Azure.)
- Disable IPv6 unless explicitly enabled. Netplan:
  `dhcp6: false`. `systemd-networkd`: `DHCP=ipv4` rather than
  `DHCP=yes`.

### Naming recommendations

The choice of `set-name` value is open within the rules above.
The following recommendations make self-managed configurations
easier to operate and audit:

- **Pick a single non-reserved prefix and number from `0`.**
  Examples: `mgmt0`, `mgmt1`, `mgmt2`; or `nic0`, `nic1`,
  `nic2`. A single prefix keeps natural sort obvious and avoids
  ambiguity about which interface is primary.
- **Reserve a prefix per role only when roles are stable.** If
  the VM has clearly distinct NIC roles (`mgmt0`, `storage0`,
  `data0`), use role prefixes — but the primary's chosen
  name must still satisfy the natural-sort rule in
  [Rules](#rules). If roles can shift across
  deployments, prefer the single-prefix pattern.
- **Honour the natural-sort rule in [Rules](#rules)
  when picking secondary prefixes.** In particular, prefixes
  that sort before `eth` (`app`, `aux`, `bond`, `br`,
  `cluster`, `data`, …) are only safe on secondaries when the
  primary is `eth0` or is renamed to a name that still sorts
  before them.
- **Avoid prefixes used by other kernel subsystems** (`bond`,
  `br`, `vlan`, `vxlan`, `wg`, `tun`, `tap`, …) even though
  they are not in the kernel `eth*` namespace, to keep `ip
  link` output unambiguous.
- **Keep names short** (the kernel limit is 15 characters; many
  monitoring tools display names in narrower columns).

Two illustrative patterns follow. Each example is shown for
both netplan and `systemd-networkd`; pick the renderer the
distribution uses.

### Example: two NICs, primary keeps `eth0`, secondary renamed

<details open>
<summary><strong>netplan</strong></summary>

```yaml
# /etc/netplan/10-stable.yaml
network:
  version: 2
  ethernets:
    eth0:                       # primary — keep kernel name
      match:
        macaddress: 7c:1e:52:cf:08:f2
        driver: hv_netvsc
      dhcp4: true
      dhcp4-overrides:
        route-metric: 100
      dhcp6: false
    mgmt0:                      # secondary — renamed out of eth* namespace
      match:
        macaddress: 7c:1e:52:cf:0b:28
        driver: hv_netvsc
      set-name: mgmt0
      dhcp4: true
      dhcp4-overrides:
        route-metric: 200
        use-dns: false
      dhcp6: false
```

</details>

<details>
<summary><strong>systemd-networkd</strong></summary>

Renames go in a `.link` file (a `.network` file cannot rename
interfaces). Number the `.link` file lower than any
distribution-shipped `.link` so it wins.

```ini
# /etc/systemd/network/10-mgmt0.link
[Match]
MACAddress=7c:1e:52:cf:0b:28
Driver=hv_netvsc

[Link]
Name=mgmt0
```

```ini
# /etc/systemd/network/20-eth0.network
[Match]
MACAddress=7c:1e:52:cf:08:f2
Driver=hv_netvsc

[Network]
DHCP=ipv4

[DHCPv4]
RouteMetric=100
```

```ini
# /etc/systemd/network/20-mgmt0.network
[Match]
Name=mgmt0

[Network]
DHCP=ipv4

[DHCPv4]
RouteMetric=200
UseDNS=false
```

</details>

### Example: three NICs, primary and secondaries all renamed

<details open>
<summary><strong>netplan</strong></summary>

```yaml
# /etc/netplan/10-stable.yaml
network:
  version: 2
  ethernets:
    mgmt0:                      # primary — natural-sorts before mgmt1, mgmt2
      match:
        macaddress: 7c:1e:52:cf:08:f2
        driver: hv_netvsc
      set-name: mgmt0
      dhcp4: true
      dhcp4-overrides:
        route-metric: 100
      dhcp6: false
    mgmt1:                      # secondary
      match:
        macaddress: 7c:1e:52:cf:0b:28
        driver: hv_netvsc
      set-name: mgmt1
      dhcp4: true
      dhcp4-overrides:
        route-metric: 200
        use-dns: false
      dhcp6: false
    mgmt2:                      # secondary
      match:
        macaddress: 7c:1e:52:cf:0b:30
        driver: hv_netvsc
      set-name: mgmt2
      dhcp4: true
      dhcp4-overrides:
        route-metric: 300
        use-dns: false
      dhcp6: false
```

</details>

<details>
<summary><strong>systemd-networkd</strong></summary>

One `.link` file per NIC for the rename, plus one `.network`
file per renamed interface for the addressing policy.

```ini
# /etc/systemd/network/10-mgmt0.link
[Match]
MACAddress=7c:1e:52:cf:08:f2
Driver=hv_netvsc

[Link]
Name=mgmt0
```

```ini
# /etc/systemd/network/10-mgmt1.link
[Match]
MACAddress=7c:1e:52:cf:0b:28
Driver=hv_netvsc

[Link]
Name=mgmt1
```

```ini
# /etc/systemd/network/10-mgmt2.link
[Match]
MACAddress=7c:1e:52:cf:0b:30
Driver=hv_netvsc

[Link]
Name=mgmt2
```

```ini
# /etc/systemd/network/20-mgmt0.network
[Match]
Name=mgmt0

[Network]
DHCP=ipv4

[DHCPv4]
RouteMetric=100
```

```ini
# /etc/systemd/network/20-mgmt1.network
[Match]
Name=mgmt1

[Network]
DHCP=ipv4

[DHCPv4]
RouteMetric=200
UseDNS=false
```

```ini
# /etc/systemd/network/20-mgmt2.network
[Match]
Name=mgmt2

[Network]
DHCP=ipv4

[DHCPv4]
RouteMetric=300
UseDNS=false
```

</details>

For larger NIC counts, repeat the secondary stanza/file pattern
with incrementing `route-metric` / `RouteMetric` (`400`, `500`,
…).

## What is not supported

Configurations that rename interfaces into the kernel-reserved
`eth*` namespace fail intermittently and are not supported upstream.
Specifically, do **not**:

- Use `set-name: eth0`, `set-name: eth1`, … in netplan.
- Use `Name=eth0`, `Name=eth1`, … in `systemd.link(5)` files.
- Pin udev rules that rename interfaces to `eth*` based on MAC.

These configurations can race with the kernel's own naming and
produce the failure mode below. Two consecutive boots of the same
VM may produce different outcomes, so a working test pass does
not indicate a working configuration. If business-meaningful
names are required, use a non-reserved prefix outside the `eth*`
namespace as described in [Self-managed
networking](#self-managed-networking-cloud-init-disabled).

## Failure mode: rename collisions

When a configuration violates the guidance above, the symptoms are:

```
systemd-udevd[…]: ethN: Failed to rename network interface <ifindex>
    from 'ethX' to 'ethY': File exists
systemd-udevd[…]: ethN: Failed to process device, ignoring: File exists
udev-worker: Failed to rename network interface … : File exists
```

Resulting state:

- One or more interfaces left under whatever name they had before
  the rename was attempted (typically the kernel `ethN` name).
- Netplan / NetworkManager / `systemd-networkd` failing to bring up
  links because the expected name does not exist.
- Intermittent reproduction across boots and across VMs of the same
  image.

The root cause is that the Linux kernel assigns initial names from
the shared `eth*` namespace as drivers probe; when udev later tries
to rename one interface to a name still held by another, the
parallel udev workers race and one rename fails with `EEXIST`. There
is no atomic swap. This is documented in `systemd.link(5)` under
`Name=`:

> Note that specifying a name that the kernel might use for another
> interface (for example "eth0") is dangerous because the name
> assignment done by udev will race with the assignment done by the
> kernel, and only one will succeed.

cloud-init's two-phase rename through a placeholder name is
what hides this race when cloud-init networking is enabled
(see [References](#cloud-init-mechanism-and-fix)). The PR
[canonical/cloud-init#6807](https://github.com/canonical/cloud-init/pull/6807)
removes the rename step on Azure entirely by dropping `set-name`
from the generated config — the layout this page recommends.

### Why the two-NIC tutorial appears to work

The official Azure two-NIC tutorial,
[Configure Linux VMs with multiple
NICs](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/linux-vm-multiple-virtual-network-interfaces-configuration),
uses `set-name: eth0` / `set-name: eth1` and works reliably in
practice because both renames are no-ops: the primary is already
`eth0`, and the secondary is the only other interface so `eth1`
is free. The race requires a rename to *swap* through a name
another interface currently holds — which effectively requires
three or more NICs. Treat the tutorial as a special case; for
three or more NICs, use [Cloud-init managed
networking](#cloud-init-managed-networking) (with
`apply_network_config_set_name: false` if available) or [Self-managed
networking](#self-managed-networking-cloud-init-disabled).

## Diagnostics checklist

When triaging a suspected naming problem on a multi-NIC Azure VM:

1. Check whether cloud-init networking is enabled or disabled:
   `grep -r 'config: disabled' /etc/cloud/cloud.cfg.d/`.
2. Inspect the generated netplan / link configuration for any
   `set-name:` or `Name=` value inside the kernel `eth*` namespace.
3. Collect `journalctl -b -u systemd-udevd` and search for
   `Failed to rename`.
4. For each NIC, capture
   `udevadm info /sys/class/net/<iface>` to see which `.link` file
   matched and what `ID_NET_NAME` was selected.
5. Confirm every netplan stanza matches by `macaddress` rather than
   by interface name, and that synthetic NIC stanzas additionally
   match by `driver: hv_netvsc` so they cannot bind to an
   accelerated-networking VF. (`driver:` is optional on future
   mana-only SKUs that have no synthetic layer and no VF
   bonding.)
6. Confirm each non-primary interface has `use-dns: false` and a
   higher `route-metric` than the primary.

## References

### The udev rename race (upstream systemd)

- [`systemd.link(5)` — `Name=`](https://www.freedesktop.org/software/systemd/man/systemd.link.html#Name=):
  > "Note that specifying a name that the kernel might use for
  > another interface (for example \"eth0\") is dangerous because
  > the name assignment done by udev will race with the assignment
  > done by the kernel, and only one will succeed."
- [systemd #16665](https://github.com/systemd/systemd/issues/16665#issuecomment-669167184)
  — closed by Lennart Poettering:
  > "Using names that collide with the nomenclature of the kernel
  > is racy and not supported."
- [systemd #29073](https://github.com/systemd/systemd/issues/29073)
  — reordering / renaming within the kernel `eth*` namespace
  cannot be made race-free.
- [systemd #29957](https://github.com/systemd/systemd/issues/29957#issuecomment-1803126839)
  — closed *not planned*:
  > "any renaming done by udev is predestined to run into races
  > … there's absolutely no warranty."

### Azure platform behaviour

- [Azure Instance Metadata Service](https://learn.microsoft.com/en-us/azure/virtual-machines/instance-metadata-service?tabs=linux)
  — the official IMDS reference. From the Network schema note:
  > "The nics returned by the network call are not guaranteed to
  > be in order."
- [Configure Linux VMs with multiple
  NICs](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/linux/linux-vm-multiple-virtual-network-interfaces-configuration)
  — the official two-NIC tutorial. Works as written for two NICs
  only because both `set-name` directives are no-ops in that
  topology; do not scale this pattern to three or more NICs.
  Background in [Why the two-NIC tutorial appears to
  work](#why-the-two-nic-tutorial-appears-to-work).

### Distribution-level confirmation

- [Debian #1037160](https://bugs.debian.org/1037160) —
  `systemd-udevd` rename collisions on multi-NIC hosts.
- [Netplan bugs on Launchpad](https://bugs.launchpad.net/netplan/+bugs?field.searchtext=Failed+to+rename+network+interface)
  — multiple user reports against netplan / `systemd-networkd`
  with the same `File exists` signature.

### cloud-init mechanism and fix

- [canonical/cloud-init#6807](https://github.com/canonical/cloud-init/pull/6807)
  — adds `apply_network_config_set_name` to the Azure datasource,
  expected in **cloud-init 26.2**. When `false`, cloud-init drops
  `set-name` from generated config and identifies NICs by MAC.
  Not yet available in any released cloud-init.
- [`generate_network_config_from_instance_network_metadata()`](cloudinit/sources/DataSourceAzure.py)
  — reference implementation of the layout this page recommends.
- Cloud-init's primary-NIC selection on Azure is a two-step
  process. [`find_candidate_nics_on_linux()` in
  cloudinit/net/__init__.py](cloudinit/net/__init__.py#L471)
  builds an ordered candidate list: `eth0` first if present,
  then the remaining interfaces in natural sort, with NICs
  that have carrier ahead of NICs that do not.
  [`find_primary_nic()` in
  cloudinit/sources/DataSourceAzure.py](cloudinit/sources/DataSourceAzure.py#L2031)
  picks the head of that list and cloud-init DHCPs only that
  one NIC, then [`_check_if_primary()`](cloudinit/sources/DataSourceAzure.py#L388)
  confirms it is the primary by checking that the lease
  contains static routes to IMDS (`169.254.169.254`) or the
  wireserver. Only the primary NIC's lease carries those
  routes on Azure, so the check is independent of kernel
  interface name and IMDS ordering — but if the head of the
  candidate list is *not* the primary, cloud-init does not
  fall through to the next candidate; it reports
  `DhcpOnNonPrimaryInterface` and the boot fails.
- [`_rename_interfaces()` in cloudinit/net/__init__.py](cloudinit/net/__init__.py#L708)
  — cloud-init's two-phase `cirename<N>` rename that masks the
  underlying udev race when networking is enabled.
