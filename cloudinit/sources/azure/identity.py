# Copyright (C) 2023 Microsoft Corporation.
#
# This file is part of cloud-init. See LICENSE file for license information.

import enum
import logging
from typing import Optional

from cloudinit import dmi
from cloudinit.sources.helpers.azure import report_diagnostic_event

LOG = logging.getLogger(__name__)


def convert_system_uuid_to_vm_id(system_uuid: str) -> str:
    """Convert system uuid to vm id."""
    parts = system_uuid.split("-")

    # Swap endianness for first three parts.
    for i in [0, 1, 2]:
        try:
            parts[i] = bytearray.fromhex(parts[i])[::-1].hex()
        except (IndexError, ValueError) as error:
            msg = "Failed to parse system uuid %r due to error: %r" % (
                system_uuid,
                error,
            )
            report_diagnostic_event(msg, logger_func=LOG.error)
            raise RuntimeError(msg) from error

    vm_id = "-".join(parts)
    report_diagnostic_event(
        "Azure VM identifier: %s" % vm_id,
        logger_func=LOG.debug,
    )
    return vm_id


def query_system_uuid() -> str:
    """Query system uuid in lower-case."""
    system_uuid = dmi.read_dmi_data("system-uuid")
    if system_uuid is None:
        raise RuntimeError("failed to read system-uuid")

    # Kernels older than 4.15 will have upper-case system uuid.
    system_uuid = system_uuid.lower()
    LOG.debug("Read product uuid: %s", system_uuid)
    return system_uuid


class ChassisAssetTag(enum.Enum):
    AZURE_CLOUD = "7783-7084-3265-9085-8269-3286-77"

    @classmethod
    def query_system(cls) -> Optional["ChassisAssetTag"]:
        """Check platform environment to report if this datasource may run.

        :returns: ChassisAssetTag if matching tag found, else None.
        """
        asset_tag = dmi.read_dmi_data("chassis-asset-tag")
        try:
            tag = cls(asset_tag)
        except ValueError:
            report_diagnostic_event(
                "Non-Azure chassis asset tag: %r" % asset_tag,
                logger_func=LOG.debug,
            )
            return None

        report_diagnostic_event(
            "Azure chassis asset tag: %r (%s)" % (asset_tag, tag.name),
            logger_func=LOG.debug,
        )
        return tag
