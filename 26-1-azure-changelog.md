# cloud-init 26.1 Azure Changelog

Changes relevant to Azure between cloud-init 25.3 and 26.1.

This release includes 2 bug fixes and 3 features. Key improvements
include more reliable ephemeral networking by ensuring the primary NIC
is selected, retry handling for IMDS 500 errors during reprovisioning,
duration reporting on finish events with VM ID in KVP telemetry, and
more verbose dhcpcd output for debugging.

## Bug Fixes

### Ensure ephemeral networking uses primary NIC

Ensures the primary NIC is selected up front in the Azure datasource,
resolving the common and confusing `iface=None` in logs with a concrete
value like `iface=eth0`. This change lays the groundwork for faster NIC
selection without relying on the "eth0" naming convention in the future
for VMs featuring MANA NICs.

PR: [#6556](https://github.com/canonical/cloud-init/pull/6556)
Issue: [#6558](https://github.com/canonical/cloud-init/issues/6558)

### Retry on IMDS 500 errors for reprovision data

Previously, HTTP 500 errors from IMDS during reprovisioning were
retried only after tearing down and re-establishing DHCP. This change
aligns with typical HTTP retry behavior by retrying 500s with backoff
and logging similar to other retriable error codes.

PR: [#6563](https://github.com/canonical/cloud-init/pull/6563)
Issue: [#6562](https://github.com/canonical/cloud-init/issues/6562)

## Features

### Report duration on finish events

Ensures `ReportEventStack` computes elapsed time and passes it through
`FinishReportingEvent` so all reporting handlers can emit durations.
Captures the `duration` field in Hyper-V KVP metadata. Duration values
are rounded to four decimal places for cleaner telemetry output.

PR: [#6552](https://github.com/canonical/cloud-init/pull/6552), [#6709](https://github.com/canonical/cloud-init/pull/6709)

### Add VM ID to KVP telemetry event keys

Includes the VM ID in the KVP (Key-Value Pair) event key format to
improve telemetry tracking and debugging for Azure/Hyper-V deployments.

PR: [#6551](https://github.com/canonical/cloud-init/pull/6551)

### Enable --debug for dhcpcd

Adds `--debug` to dhcpcd invocations to capture detailed DHCP flow
including DISCOVER/REQUEST timing, useful for diagnosing Azure
networking issues.

PR: [#6693](https://github.com/canonical/cloud-init/pull/6693)

## Test Improvements

### Enable pubkey extraction and certificate parsing tests

Enables `test_pubkey_extract` and `test_parse_certificates` to run
in CI.

PR: [#6572](https://github.com/canonical/cloud-init/pull/6572)
Issue: [#6571](https://github.com/canonical/cloud-init/issues/6571)

### Skip ssh-keygen tests when not installed

Skips Azure ssh-keygen related tests in environments without
`ssh-keygen` installed.

PR: [#6612](https://github.com/canonical/cloud-init/pull/6612)

### Skip openssl tests on non-Linux

Handles openssl implementation differences across platforms.

PR: [#6473](https://github.com/canonical/cloud-init/pull/6473)

### Hardcode passlib usage in Azure test

Fixes test dependency on passlib detection.

PR: [#6473](https://github.com/canonical/cloud-init/pull/6473)
