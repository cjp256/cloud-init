# Proposal: Replacing ovf-env.xml with WireServer Provisioning Endpoints

## Summary

Replace the OVF-over-CDROM provisioning mechanism (`ovf-env.xml` on
`/dev/sr0`) with HTTP-based delivery via WireServer. Three options are
presented, ranging from a minimal secrets-only endpoint to a full JSON
consolidation of all provisioning fields. Networking stays on IMDS in
all options (sourced from NMAgent, a separate host-side component).

## Background

### Current provisioning architecture

Azure Linux VMs are provisioned by `DataSourceAzure` in cloud-init, which
reads configuration from three separate sources during boot:

| Source | Endpoint / Mechanism | What it provides |
|---|---|---|
| **OVF** | `/dev/sr0` CDROM (XML) or IMDS `/reprovisiondata` for PPS | `adminUsername`, `adminPassword`, `hostname`, `customData`, `disablePasswordAuthentication`, `publicKeys`, `preprovisionedVm`, `preprovisionedVmType`, `provisionGuestProxyAgent` |
| **IMDS** | `http://169.254.169.254/metadata/instance` (JSON) | `network.*`, `vmId`, `location`, `platformFaultDomain`, `userData`, `osProfile`, `hasCustomData`, `ppsType` |
| **WireServer GoalState** | `http://168.63.129.16/machine/?comp=goalstate` (XML) | `containerId`, `instanceId`, `incarnation` (used for health reporting) |

`containerId` from GoalState is used only for legacy health reports
(`/machine?comp=health`). With the new `/provisioning/health` endpoint,
the server identifies the VM from request context, so this field is no
longer needed by the client.

### Problems with the current approach

1. **CDROM dependency** — When delivered via `/dev/sr0`, OVF requires ISO
   device discovery, UDF/ISO9660 filesystem mounting, and CDROM ejection.
   OVF can also be delivered over the network (IMDS `/reprovisiondata` for
   PPS), so media handling and XML parsing are separate concerns.

2. **XML complexity** — Regardless of delivery mechanism, OVF is XML.
   This differs from the JSON-based IMDS used for most metadata.

3. **Host-side resource hold** — The host must maintain an ISO image
   containing the OVF document and its dependencies for as long as the guest
   may need it (e.g. across reboots before provisioning completes). This ties
   up host-side storage and compute resources for an indeterminate period and
   is the most significant operational cost of the current approach.

4. **Three-source split** — Provisioning data is fragmented across OVF, IMDS,
   and GoalState, requiring cloud-init to coordinate three different protocols,
   parsers, and retry strategies.

> **Note:** Cloud-init is independently consolidating the legacy `comp=goalstate`
> and `comp=health` WireServer endpoints into a single `/provisioning/health`
> endpoint. This work is happening regardless of which option is chosen here.

### Provisioning field inventory

| Field Name | Description | Current Source Path(s) | Mutable? |
|---|---|---|---|
| `adminUsername` | Default admin/system user name. | IMDS: `compute.osProfile.adminUsername`<br>OVF: `LinuxProvisioningConfigurationSet/UserName` | No |
| `adminPassword` | Admin user password (plaintext). | OVF: `LinuxProvisioningConfigurationSet/UserPassword` | No |
| `customData` | User-provided provisioning payload (Base64-decoded on parse). | OVF: `LinuxProvisioningConfigurationSet/CustomData` | No |
| `disableAdminAccount` | Whether the admin account should be disabled. Default: `false`. | IMDS: `compute.osProfile.disableAdminAccount`<br>OVF: `LinuxProvisioningConfigurationSet/DisableAdminAccount` | No |
| `disablePasswordAuthentication` | Whether SSH password auth should be disabled. | IMDS: `compute.osProfile.disablePasswordAuthentication`<br>OVF: `LinuxProvisioningConfigurationSet/DisableSshPasswordAuthentication` | No |
| `hasCustomData` | Whether the VM was configured with custom data. | IMDS: `extended.compute.hasCustomData` | No |
| `hostname` | VM computer name (`compute.osProfile.computerName`). | IMDS: `compute.osProfile.computerName`<br>OVF: `LinuxProvisioningConfigurationSet/HostName` | Yes |
| `location` | Azure region. | IMDS: `compute.location` | No |
| `network.*` | Full NIC configuration (MACs, IPv4/IPv6, subnet prefixes). No secrets. | IMDS: `network.interface[]` | Yes |
| `platformFaultDomain` | Fault domain index. | IMDS: `compute.platformFaultDomain` | No |
| `preprovisionedVm` | Pre-provisioned VM mode. Default: `false`. | OVF: `PlatformSettings/PreprovisionedVm` | No |
| `preprovisionedVmType` | PPS type: `RUNNING`, `SAVABLE`, `OS_DISK`, `NONE`. | IMDS: `extended.compute.ppsType`<br>OVF: `PlatformSettings/PreprovisionedVMType` | No |
| `provisionGuestProxyAgent` | Whether Azure Guest Proxy Agent should be enabled. Default: `false`. | OVF: `PlatformSettings/ProvisionGuestProxyAgent` | No |
| `publicKeys.*` | SSH public keys. IMDS: `keyData` in OpenSSH format. OVF: `Fingerprint`, `Path`, `Value` per key. | IMDS: `compute.publicKeys[].keyData`<br>OVF: `SSH/PublicKeys/PublicKey/{Fingerprint,Path,Value}` | Yes |
| `userData` | User data (Base64-encoded). Fallback when no OVF `customData`. | IMDS: `compute.userData` | Yes |
| `vmId` | Unique VM identifier. | IMDS: `compute.vmId` | No |

Cloud-init re-reads the following fields from IMDS on every boot (not just
first provisioning): `adminUsername`, `disablePasswordAuthentication`,
`hostname`, `location`, `platformFaultDomain`, `publicKeys.*`, `userData`,
`vmId`, `hasCustomData`, `ppsType`, `network.*`.

---

## Proposal

All options share a new health reporting endpoint that replaces
`comp=goalstate` and `comp=health`:

```
POST http://168.63.129.16/provisioning/health?api-version=<version>
```

Health reporting uses JSON (`state=ready` or `state=error`) instead of the
legacy Health XML. WireServer identifies the VM from request context — no
`containerId` required from the client.

All options use the same HTTP status codes, security model, error handling,
and retry strategy (described in [Common infrastructure](#common-infrastructure)
below).

---

### Option 1 — Secrets only via WireServer

Extend IMDS with a `hasAdminPassword` flag. Add two WireServer endpoints
to serve the secrets that currently come from OVF. Everything else stays
on IMDS.

This is the minimum change needed to decouple secret delivery from OVF.

#### New endpoints

```
GET http://168.63.129.16/provisiondata/customData?api-version=<version>
GET http://168.63.129.16/provisiondata/adminPassword?api-version=<version>
```

Each returns the field's value as JSON (`Content-Type: application/json`)
by default. Use `format=text` for the raw value without JSON quoting
(`Content-Type: text/plain`). When the field is not configured, JSON
format returns `null`; text format returns an empty body.

```
GET /provisiondata/customData                →  "PGJhc2U2ND4="   (json, default)
GET /provisiondata/customData?format=text    →  PGJhc2U2ND4=     (text)
GET /provisiondata/adminPassword             →  null              (json, not configured)
GET /provisiondata/adminPassword?format=text →  (empty body)      (text, not configured)
```

#### New IMDS flag

| Flag | IMDS Path | Controls |
|---|---|---|
| `hasAdminPassword` | `extended.compute.hasAdminPassword` (**new**) | Fetch `/provisiondata/adminPassword` only when `true` |

(`hasCustomData` already exists at `extended.compute.hasCustomData`.)

#### Provisioning flow

```
 1. GET http://169.254.169.254/metadata/instance?extended=true
    → adminUsername, hostname, publicKeys,
      disablePasswordAuthentication, hasCustomData,
      network, vmId, location, platformFaultDomain,
      userData, ppsType, etc.
    On 401: run azure-proxy-agent --status --wait 120, retry.

 2. If hasCustomData == true:
      GET http://168.63.129.16/provisiondata/customData
    If hasAdminPassword == true:
      GET http://168.63.129.16/provisiondata/adminPassword
    On 401: run azure-proxy-agent --status --wait 120, retry.
    On 404: report failure, data has been deleted.

 3. POST http://168.63.129.16/provisioning/health
    Body: { "state": "ready" }

 4. Apply configuration
```

#### Host requirements

- Two new WireServer endpoints (`/provisiondata/customData`, `/provisiondata/adminPassword`).
- One new IMDS flag (`hasAdminPassword`).

---

### Option 2 — All non-networking config via WireServer (OVF XML format)

Serve the existing `ovf-env.xml` document over HTTP from WireServer instead
of mounting it from `/dev/sr0`. The response body is the same XML that would
have been on the CDROM. Networking continues to come from IMDS.

This eliminates the CDROM dependency and host-side resource hold with
minimal host-side changes — the same OVF document is served, just via a
different delivery mechanism. Cloud-init still parses OVF XML — no format
change.

#### New endpoint

```
GET http://168.63.129.16/provisiondata?api-version=<version>
Content-Type: application/xml
```

Returns the full `ovf-env.xml` document as-is.

#### Provisioning flow

```
 1. GET http://168.63.129.16/provisiondata?api-version=<version>
    → ovf-env.xml (same XML schema as /dev/sr0)
    On 401: run azure-proxy-agent --status --wait 120, retry.
    On 404: report failure — no cached data on a new VM.

 2. GET http://169.254.169.254/metadata/instance
    → network.*, vmId, location, platformFaultDomain,
      userData, etc.
    On 401: run azure-proxy-agent --status --wait 120, retry.

 3. POST http://168.63.129.16/provisioning/health
    Body: { "state": "ready" }

 4. Apply configuration
```

#### Host requirements

- One new WireServer endpoint serving the existing OVF document via HTTP.
  Similar to the existing `/reprovisiondata` endpoint used for PPS, but
  with the updated HTTP status codes defined in
  [Common infrastructure](#common-infrastructure).

---

### Option 3 — All non-networking config via WireServer (JSON format)

Serve all non-networking provisioning fields as a single JSON response
from WireServer. Networking continues to come from IMDS (sourced from
NMAgent, a different host-side component).

This is the most complete change — eliminates CDROM, XML, and the
multi-source split for provisioning data.

#### New endpoint

```
GET http://168.63.129.16/provisiondata?api-version=<version>
Content-Type: application/json
```

#### Response schema

```json
{
  "adminUsername": "azureuser",
  "adminPassword": "<plaintext-password-or-null>",
  "customData": "<base64-encoded-or-null>",
  "disableAdminAccount": false,
  "disablePasswordAuthentication": true,
  "hasCustomData": true,
  "hostname": "my-vm",
  "location": "eastus",
  "platformFaultDomain": "0",
  "publicKeys": [
    {
      "keyData": "ssh-rsa AAAA...",
      "path": "/home/azureuser/.ssh/authorized_keys"
    }
  ],
  "preprovisionedVm": false,
  "preprovisionedVmType": "None",
  "provisionGuestProxyAgent": false,
  "userData": "<base64-encoded-or-null>",
  "vmId": "d5e6f7a8-1b2c-3d4e-5f6a-7b8c9d0e1f2a"
}
```

| Field | Type | Nullable | Description |
|---|---|---|---|
| `adminUsername` | string | Yes | Default admin user name. `null` when `disableAdminAccount` is `true`. |
| `adminPassword` | string | Yes | Admin password (plaintext). `null` when not configured or account disabled. |
| `customData` | string | Yes | Provisioning payload (Base64-encoded). `null` when not configured. |
| `disableAdminAccount` | boolean | No | When `true`, no local admin account is created. Default: `false`. |
| `disablePasswordAuthentication` | boolean | No | Whether SSH password auth should be disabled. |
| `hasCustomData` | boolean | No | Whether the VM was configured with custom data. |
| `hostname` | string | No | VM computer name. |
| `location` | string | No | Azure region (e.g. `"eastus"`). |
| `platformFaultDomain` | string | No | Fault domain index. |
| `publicKeys` | array | No | SSH public keys (`keyData` + `path`). Empty array when none configured. |
| `preprovisionedVm` | boolean | No | Whether the VM is pre-provisioned. |
| `preprovisionedVmType` | string | No | `"None"`, `"Running"`, `"Savable"`, `"PreprovisionedOSDisk"`. |
| `provisionGuestProxyAgent` | boolean | No | Whether GPA should be enabled. |
| `userData` | string | Yes | User data (Base64-encoded). `null` when not configured. |
| `vmId` | string | No | Unique VM identifier. |

#### Provisioning flow

```
 1. GET http://168.63.129.16/provisiondata?api-version=<version>
    → all non-networking provisioning fields as JSON
    On 401: run azure-proxy-agent --status --wait 120, retry.
    On 404: report failure — no cached data on a new VM.

 2. GET http://169.254.169.254/metadata/instance
    → network.* (for NIC configuration)
    On 401: run azure-proxy-agent --status --wait 120, retry.

 3. POST http://168.63.129.16/provisioning/health
    Body: { "state": "ready" }

 4. Apply configuration
```

#### Host requirements

- One new WireServer endpoint serving all non-networking provisioning fields as JSON.
- WireServer must assemble fields from CRP (MediaContent, HostingEnvironmentConfiguration,
  InVMArtifactsProfileBlob) into a single JSON response.
- Networking remains on IMDS (NMAgent source, no change needed).

---

### Option comparison

| | Option 1 | Option 2 | Option 3 |
|---|---|---|---|
| **Delivery** | IMDS + WireServer (secrets only) | WireServer (OVF XML) + IMDS | WireServer (JSON) + IMDS |
| **Eliminates CDROM** | Bypassed (code retained) | Bypassed (code retained) | Bypassed (code retained) |
| **Eliminates XML** | Yes | No | Yes |
| **Eliminates host ISO hold** | Yes | Yes | Yes |
| **Guest-side change** | Small — add two fetch calls | Medium — change delivery path, keep parser | Large — new parser, remove XML |
| **Host-side change** | Small — two endpoints + one IMDS flag | Small — serve existing document via HTTP | Medium — assemble JSON from CRP sources |
| **Sources during provisioning** | 2 (IMDS + WireServer secrets) | 2 (WireServer XML + IMDS) | 2 (WireServer JSON + IMDS) |
| **Format consistency** | JSON everywhere | Mixed (XML + JSON) | JSON everywhere |
| **Three-source split** | Partially resolved (2 sources, clean split) | Partially resolved (2 sources, 2 parsers) | Mostly resolved (2 JSON sources, clean split) |

### Decision questions

1. **Can the host assemble a new JSON response from CRP sources, or only
   serve the existing OVF document?** If only the existing document, Option 2
   is the only choice. If a new response can be built, Options 1 and 3 are
   both viable.

2. **Is it acceptable to split non-network provisioning across two services
   (IMDS + WireServer)?** Option 1 keeps config fields on IMDS and moves
   only secrets to WireServer — two delivery paths the host must keep in
   sync. Option 3 consolidates everything into a single WireServer request.

3. **Does eliminating XML parsing on the guest matter?** Options 1 and 3
   achieve JSON everywhere. Option 2 preserves the existing XML schema and
   parser.

---

### Common infrastructure

The following applies to all three options for new endpoints.

#### HTTP headers and query parameters

| Parameter | Location | Value | Purpose |
|---|---|---|---|
| `x-ms-agent-name` | Header | `cloud-init` | Agent identification |
| `api-version` | Query | e.g. `2026-01-01` | API contract version. WireServer returns `400` for unrecognized versions. |
| `format` | Query | `text` \| `json` (default: `json`) | Response format for per-field sub-paths (Option 1 only). `json` returns JSON-typed values; `text` returns raw values without JSON quoting. |

#### HTTP status codes

| Code | Condition | Retryable? |
|---|---|---|
| `200 OK` | Provisioning data returned successfully. | N/A |
| `400 Bad Request` | Unrecognized `api-version` or malformed request. | No |
| `401 Unauthorized` | Secure channel not established. Cloud-init should start GPA (`azure-proxy-agent --status --wait 120`), then retry. | **Yes** — after GPA setup. |
| `404 Not Found` | Endpoint permanently disabled post-provisioning (VM already reported ready), or unknown path. | **No** — do not retry. On initial provisioning, report failure. |
| `429 Too Many Requests` | Rate-limited. | Yes |
| `500 Internal Server Error` | Transient server error. | Yes |
| `503 Service Unavailable` | Data not yet staged. May include `Retry-After` header. | Yes — honour `Retry-After` if present. |

#### Security model

| Concern | Mitigation |
|---|---|
| **Network access** | WireServer (`168.63.129.16`) reachable only from the guest via host-level networking. `iptables`/`nftables` rules restrict access to `root`-only processes (`-m owner --uid-owner 0`). |
| **Provisioning state gating** | WireServer returns `200` only before report-ready. After the ready signal, endpoint returns `404`. Secrets are not accessible post-provisioning. |
| **Secrets in transit** | Data travels over the host-guest virtual network fabric (not a physical network). Same trust boundary as current OVF-over-CDROM and WireServer. |

#### Backward compatibility

| Concern | Approach |
|---|---|
| **Detecting endpoint availability** | Cloud-init attempts `/provisiondata` first. If WireServer returns `400` or connection refused, fall back to OVF/CDROM. |
| **Legacy VMs** | Already-provisioned VMs never call `/provisiondata` (returns `404`). |
| **Mixed fleets** | Cloud-init ships both code paths. `datasource.Azure.provisioning_source` config key (`provisiondata` \| `ovf` \| `auto`) overrides detection. Default: `auto`. |

All options require the host to maintain the OVF/ISO pipeline for backward
compatibility with older cloud-init versions until the CDROM deprecation
roadmap reaches Phase 5.

---

## CDROM deprecation roadmap

None of the options can remove the CDROM code path immediately — older
cloud-init versions running on existing VMs still depend on `/dev/sr0`
for OVF delivery, and the host must continue staging the ISO for them.
The goal is to stop staging provisioning media by default while providing
an opt-in for VMs that still need it.

### Transition mechanism

New-generation VM sizes (e.g. v8 and later) stop attaching provisioning
media by default. For VMs that still require it — custom images with
older cloud-init, legacy provisioning agents, etc. — a CRP-level option
enables provisioning media on a per-VM basis:

```json
"osProfile": {
  "legacyProvisioningMedia": true   // default: false for v8+
}
```

### Phases

| Phase | Scope | Guest behaviour | Host behaviour |
|---|---|---|---|
| **0 — Status quo** | All VM sizes | Read OVF from `/dev/sr0`. | Stage ISO, attach provisioning media. |
| **1 — Dual path** | All VM sizes | Try `/provisiondata` first; fall back to CDROM on `400`/connection refused. `datasource.Azure.provisioning_source=auto`. | Serve both `/provisiondata` and ISO. |
| **2 — New sizes default off** | v8+ VM sizes | Same as Phase 1. CDROM fallback triggers deprecation warning. | v8+: no ISO unless `legacyProvisioningMedia=true`. Older sizes: unchanged. |
| **3 — Expand default off** | Progressively older sizes | Same as Phase 2. | Progressively stop staging ISO for older sizes. CRP opt-in remains. |
| **4 — ISO opt-in only** | All VM sizes | CDROM fallback only works when explicitly enabled. | ISO staged only when `legacyProvisioningMedia=true`. |
| **5 — Code removal** | Cloud-init major version bump | Remove CDROM/OVF code paths entirely. | Retire `legacyProvisioningMedia` option and ISO pipeline. |

### Key dependencies

- **Phase 0 → 1:** Cloud-init ships `/provisiondata` support. Host serves
  the new endpoint alongside ISO.
- **Phase 1 → 2:** Telemetry confirms `/provisiondata` works reliably.
  CRP `osProfile.legacyProvisioningMedia` option is available for opt-in.
- **Phase 2 → 3:** Marketplace images for v8+ sizes all ship Phase 1+
  cloud-init. Custom image owners notified via Azure Advisor.
- **Phase 3 → 4:** All first-party and endorsed images updated across
  all VM sizes. Final notice period for custom images.
- **Phase 4 → 5:** Sufficient bake time (12+ months) with ISO opt-in
  only. Confirm no regressions before removing code.
