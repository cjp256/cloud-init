# Copyright (C) 2013 Canonical Ltd.
#
# Author: Scott Moser <scott.moser@canonical.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
import crypt
import os
import os.path
import socket
import re
import xml.etree.ElementTree as ET
from collections import namedtuple
from enum import Enum
from time import sleep, time
from typing import List, Optional, Tuple
from xml.dom import minidom

import requests

from cloudinit import dmi
from cloudinit import log as logging
from cloudinit import net, sources, ssh_util, subp, util
from cloudinit.event import EventScope, EventType
from cloudinit.net import device_driver, find_candidate_nics
from cloudinit.net.dhcp import EphemeralDHCPv4, NoDHCPLeaseError
from cloudinit.reporting import events
from cloudinit.sources.helpers import netlink
from cloudinit.sources.helpers.azure import (
    DEFAULT_REPORT_FAILURE_USER_VISIBLE_MESSAGE,
    DEFAULT_WIRESERVER_ENDPOINT,
    azure_ds_reporter,
    azure_ds_telemetry_reporter,
    build_minimal_ovf,
    dhcp_log_cb,
    get_boot_telemetry,
    get_metadata_from_fabric,
    get_system_info,
    is_byte_swapped,
    push_log_to_kvp,
    report_diagnostic_event,
    report_failure_to_fabric,
    WALinuxAgentShim,
)
from cloudinit.url_helper import UrlError, readurl

LOG = logging.getLogger(__name__)

DS_NAME = "Azure"
DEFAULT_METADATA = {"instance-id": "iid-AZURE-NODE"}

# azure systems will always have a resource disk, and 66-azure-ephemeral.rules
# ensures that it gets linked to this path.
RESOURCE_DISK_PATH = "/dev/disk/cloud/azure_resource"
LEASE_FILE = "/var/lib/dhcp/dhclient.eth0.leases"
DEFAULT_FS = "ext4"
# DMI chassis-asset-tag is set static for all azure instances
AZURE_CHASSIS_ASSET_TAG = "7783-7084-3265-9085-8269-3286-77"
REPORTED_READY_MARKER_FILE = "/var/lib/cloud/data/reported_ready"
AGENT_SEED_DIR = "/var/lib/waagent"
DEFAULT_PROVISIONING_ISO_DEV = "/dev/sr0"

# In the event where the IMDS primary server is not
# available, it takes 1s to fallback to the secondary one
IMDS_TIMEOUT_IN_SECONDS = 2
IMDS_URL = "http://169.254.169.254/metadata"
IMDS_VER_MIN = "2019-06-01"
IMDS_VER_WANT = "2021-08-01"
IMDS_EXTENDED_VER_MIN = "2021-03-01"

# This holds SSH key data including if the source was
# from IMDS, as well as the SSH key data itself.
SSHKeys = namedtuple("SSHKeys", ("keys_from_imds", "ssh_keys"))


class MetadataType(Enum):
    ALL = "{}/instance".format(IMDS_URL)
    NETWORK = "{}/instance/network".format(IMDS_URL)
    REPROVISIONDATA = "{}/reprovisiondata".format(IMDS_URL)


class PPSType(Enum):
    NONE = None
    SAVABLE = "Savable"
    RUNNING = "Running"
    UNKNOWN = "Unknown"


PLATFORM_ENTROPY_SOURCE: Optional[str] = "/sys/firmware/acpi/tables/OEM0"

# List of static scripts and network config artifacts created by
# stock ubuntu suported images.
UBUNTU_EXTENDED_NETWORK_SCRIPTS = [
    "/etc/netplan/90-hotplug-azure.yaml",
    "/usr/local/sbin/ephemeral_eth.sh",
    "/etc/udev/rules.d/10-net-device-added.rules",
    "/run/network/interfaces.ephemeral.d",
]

# This list is used to blacklist devices that will be considered
# for renaming or fallback interfaces.
#
# On Azure network devices using these drivers are automatically
# configured by the platform and should not be configured by
# cloud-init's network configuration.
#
# Note:
# Azure Dv4 and Ev4 series VMs always have mlx5 hardware.
# https://docs.microsoft.com/en-us/azure/virtual-machines/dv4-dsv4-series
# https://docs.microsoft.com/en-us/azure/virtual-machines/ev4-esv4-series
# Earlier D and E series VMs (such as Dv2, Dv3, and Ev3 series VMs)
# can have either mlx4 or mlx5 hardware, with the older series VMs
# having a higher chance of coming with mlx4 hardware.
# https://docs.microsoft.com/en-us/azure/virtual-machines/dv2-dsv2-series
# https://docs.microsoft.com/en-us/azure/virtual-machines/dv3-dsv3-series
# https://docs.microsoft.com/en-us/azure/virtual-machines/ev3-esv3-series
BLACKLIST_DRIVERS = ["mlx4_core", "mlx5_core"]


def find_storvscid_from_sysctl_pnpinfo(sysctl_out, deviceid):
    # extract the 'X' from dev.storvsc.X. if deviceid matches
    """
    dev.storvsc.1.%pnpinfo:
        classid=32412632-86cb-44a2-9b5c-50d1417354f5
        deviceid=00000000-0001-8899-0000-000000000000
    """
    for line in sysctl_out.splitlines():
        if re.search(r"pnpinfo", line):
            fields = line.split()
            if len(fields) >= 3:
                columns = fields[2].split("=")
                if (
                    len(columns) >= 2
                    and columns[0] == "deviceid"
                    and columns[1].startswith(deviceid)
                ):
                    comps = fields[0].split(".")
                    return comps[2]
    return None


def find_busdev_from_disk(camcontrol_out, disk_drv):
    # find the scbusX from 'camcontrol devlist -b' output
    # if disk_drv matches the specified disk driver, i.e. blkvsc1
    """
    scbus0 on ata0 bus 0
    scbus1 on ata1 bus 0
    scbus2 on blkvsc0 bus 0
    scbus3 on blkvsc1 bus 0
    scbus4 on storvsc2 bus 0
    scbus5 on storvsc3 bus 0
    scbus-1 on xpt0 bus 0
    """
    for line in camcontrol_out.splitlines():
        if re.search(disk_drv, line):
            items = line.split()
            return items[0]
    return None


def find_dev_from_busdev(camcontrol_out, busdev):
    # find the daX from 'camcontrol devlist' output
    # if busdev matches the specified value, i.e. 'scbus2'
    """
    <Msft Virtual CD/ROM 1.0>          at scbus1 target 0 lun 0 (cd0,pass0)
    <Msft Virtual Disk 1.0>            at scbus2 target 0 lun 0 (da0,pass1)
    <Msft Virtual Disk 1.0>            at scbus3 target 1 lun 0 (da1,pass2)
    """
    for line in camcontrol_out.splitlines():
        if re.search(busdev, line):
            items = line.split("(")
            if len(items) == 2:
                dev_pass = items[1].split(",")
                return dev_pass[0]
    return None


def execute_or_debug(cmd, fail_ret=None):
    try:
        return subp.subp(cmd)[0]
    except subp.ProcessExecutionError:
        LOG.debug("Failed to execute: %s", " ".join(cmd))
        return fail_ret


def get_dev_storvsc_sysctl():
    return execute_or_debug(["sysctl", "dev.storvsc"], fail_ret="")


def get_camcontrol_dev_bus():
    return execute_or_debug(["camcontrol", "devlist", "-b"])


def get_camcontrol_dev():
    return execute_or_debug(["camcontrol", "devlist"])


def get_resource_disk_on_freebsd(port_id):
    g0 = "00000000"
    if port_id > 1:
        g0 = "00000001"
        port_id = port_id - 2
    g1 = "000" + str(port_id)
    g0g1 = "{0}-{1}".format(g0, g1)

    # search 'X' from
    #  'dev.storvsc.X.%pnpinfo:
    #      classid=32412632-86cb-44a2-9b5c-50d1417354f5
    #      deviceid=00000000-0001-8899-0000-000000000000'
    sysctl_out = get_dev_storvsc_sysctl()

    storvscid = find_storvscid_from_sysctl_pnpinfo(sysctl_out, g0g1)
    if not storvscid:
        LOG.debug("Fail to find storvsc id from sysctl")
        return None

    camcontrol_b_out = get_camcontrol_dev_bus()
    camcontrol_out = get_camcontrol_dev()
    # try to find /dev/XX from 'blkvsc' device
    blkvsc = "blkvsc{0}".format(storvscid)
    scbusx = find_busdev_from_disk(camcontrol_b_out, blkvsc)
    if scbusx:
        devname = find_dev_from_busdev(camcontrol_out, scbusx)
        if devname is None:
            LOG.debug("Fail to find /dev/daX")
            return None
        return devname
    # try to find /dev/XX from 'storvsc' device
    storvsc = "storvsc{0}".format(storvscid)
    scbusx = find_busdev_from_disk(camcontrol_b_out, storvsc)
    if scbusx:
        devname = find_dev_from_busdev(camcontrol_out, scbusx)
        if devname is None:
            LOG.debug("Fail to find /dev/daX")
            return None
        return devname
    return None


# update the FreeBSD specific information
if util.is_FreeBSD():
    LEASE_FILE = "/var/db/dhclient.leases.hn0"
    DEFAULT_FS = "freebsd-ufs"
    res_disk = get_resource_disk_on_freebsd(1)
    if res_disk is not None:
        LOG.debug("resource disk is not None")
        RESOURCE_DISK_PATH = "/dev/" + res_disk
    else:
        LOG.debug("resource disk is None")
    # TODO Find where platform entropy data is surfaced
    PLATFORM_ENTROPY_SOURCE = None

BUILTIN_DS_CONFIG = {
    "data_dir": AGENT_SEED_DIR,
    "disk_aliases": {"ephemeral0": RESOURCE_DISK_PATH},
    "dhclient_lease_file": LEASE_FILE,
    "apply_network_config": True,  # Use IMDS published network configuration
}
# RELEASE_BLOCKER: Xenial and earlier apply_network_config default is False

BUILTIN_CLOUD_EPHEMERAL_DISK_CONFIG = {
    "disk_setup": {
        "ephemeral0": {
            "table_type": "gpt",
            "layout": [100],
            "overwrite": True,
        },
    },
    "fs_setup": [{"filesystem": DEFAULT_FS, "device": "ephemeral0.1"}],
}

DS_CFG_PATH = ["datasource", DS_NAME]
DS_CFG_KEY_PRESERVE_NTFS = "never_destroy_ntfs"
DEF_EPHEMERAL_LABEL = "Temporary Storage"

# The redacted password fails to meet password complexity requirements
# so we can safely use this to mask/redact the password in the ovf-env.xml
DEF_PASSWD_REDACTION = "REDACTED"


class DataSourceAzure(sources.DataSource):

    dsname = "Azure"
    default_update_events = {
        EventScope.NETWORK: {
            EventType.BOOT_NEW_INSTANCE,
            EventType.BOOT,
        }
    }
    _metadata_imds = sources.UNSET
    _ci_pkl_version = 1

    def __init__(self, sys_cfg, distro, paths):
        sources.DataSource.__init__(self, sys_cfg, distro, paths)
        self.cfg = {}
        self.metadata = {}
        self.ds_cfg = util.mergemanydict(
            [util.get_cfg_by_path(sys_cfg, DS_CFG_PATH, {}), BUILTIN_DS_CONFIG]
        )
        self._ephemeral_dhcp_ctx = {}
        self._iso_dev = None
        self._negotiated = False
        self._network_config = None
        self._ovf = None
        self._seed = None
        self._seed_dir = os.path.join(paths.seed_dir, "azure")
        self._wireserver_address = DEFAULT_WIRESERVER_ENDPOINT

    def _unpickle(self, ci_pkl_version: int) -> None:
        super()._unpickle(ci_pkl_version)

        self._ephemeral_dhcp_ctx = {}
        self._iso_dev = None
        if not hasattr(self, "_negotiated"):
            self._negotiated = False
        self._ovf = None
        self._seed = None
        if not hasattr(self, "wireserver_address"):
            self._wireserver_address = DEFAULT_WIRESERVER_ENDPOINT

    def __str__(self):
        root = sources.DataSource.__str__(self)
        return "%s [seed=%s]" % (root, self._seed)

    def _get_subplatform(self):
        """Return the subplatform metadata source details."""
        if self._seed.startswith("/dev"):
            subplatform_type = "config-disk"
        elif self._seed.lower() == "imds":
            subplatform_type = "imds"
        else:
            subplatform_type = "seed-dir"
        return "%s (%s)" % (subplatform_type, self._seed)

    @azure_ds_telemetry_reporter
    def _load_azure_ds_dir(self, source_dir: str):
        ovf_file = os.path.join(source_dir, "ovf-env.xml")

        if not os.path.isfile(ovf_file):
            raise NonAzureDataSource("No ovf-env file found")

        with open(ovf_file, "rb") as fp:
            self._ovf = fp.read()

        self.metadata, self.userdata_raw, self.cfg = read_azure_ovf(self._ovf)

    @azure_ds_telemetry_reporter
    def _load_local_metadata_source(
        self,
    ) -> Optional[str]:
        """Find local metadata source (OVF).

        Azure removes/ejects the cdrom containing the ovf-env.xml file on
        reboot.  So, in order to successfully reboot we need to look in the
        data directory which will contain the previously saved config.
        """
        for path in list_possible_azure_ds(
            self._seed_dir, self.ds_cfg["data_dir"]
        ):
            LOG.debug("Checking %s for Azure datasource", path)
            try:
                if path.startswith("/dev/"):
                    if util.is_FreeBSD():
                        util.mount_cb(
                            path, self._load_azure_ds_dir, mtype="udf"
                        )
                    else:
                        util.mount_cb(path, self._load_azure_ds_dir)

                    self._iso_dev = path
                else:
                    self._load_azure_ds_dir(path)

                report_diagnostic_event(
                    "Found provisioning metadata in %s" % path,
                    logger_func=LOG.debug,
                )
                return path
            except NonAzureDataSource:
                report_diagnostic_event(
                    "Did not find Azure data source in %s" % path,
                    logger_func=LOG.debug,
                )
            except util.MountFailedError:
                report_diagnostic_event(
                    "%s was not mountable" % path, logger_func=LOG.debug
                )
            except BrokenAzureDataSource as exc:
                msg = "BrokenAzureDataSource: %s" % exc
                report_diagnostic_event(msg, logger_func=LOG.error)
                raise sources.InvalidMetaDataException(msg)

        # No local metadata found, must resort to populating from IMDS.
        return None

    @azure_ds_telemetry_reporter
    def _setup_ephemeral_networking(self) -> Optional[dict]:
        """Setup ephemeral networking.

        Bring up each interface until we have either have found primary NIC or
        connectivity is established with IMDS.  An interface is determined to
        be primary if it has a static route for IMDS or Wireserver in DHCP
        lease.

        Repeated calls will only perform DHCP on new interfaces that have not
        already been brought up (unless teardown is invoked first).

        :return: IMDS metadata, if fetched as part of check.
        """
        ifaces = find_candidate_nics(BLACKLIST_DRIVERS)
        while not ifaces:
            report_diagnostic_event("Waiting for NIC to come online...")
            sleep(1)
            ifaces = find_candidate_nics(BLACKLIST_DRIVERS)

        for iface in ifaces:
            if iface in self._ephemeral_dhcp_ctx:
                # This NIC was already brought up, ignore.
                continue

            LOG.info("Bringing up ephemeral DHCP for NIC %s", iface)
            dhcp_ctx = perform_dhcp(iface=iface)
            self._ephemeral_dhcp_ctx[iface] = dhcp_ctx

            # Update known wireserver address, if specified.
            if dhcp_ctx and dhcp_ctx.lease and "unknown-245" in dhcp_ctx.lease:
                self._wireserver_address = (
                    WALinuxAgentShim.get_ip_from_lease_value(
                        dhcp_ctx.lease["unknown-245"]
                    )
                )

            # NIC is primary if we have the routes to IMDS/Wireserver.
            imds_network = "169.254.169.254/32"
            wireserver_network = self._wireserver_address + "/32"
            routes = dhcp_ctx._ephipv4.static_routes
            LOG.debug(
                "NIC iface=%s lease=%s routes=%s",
                iface,
                dhcp_ctx.lease,
                routes,
            )
            route_networks = (r[0] for r in routes)
            if (
                imds_network in route_networks
                or wireserver_network in route_networks
            ):
                LOG.debug("NIC %s is primary interface", iface)
                return None

            # If we can get metadata from IMDS, then primary is up.
            imds_md = get_imds_metadata(retries=0)
            if imds_md:
                return imds_md

        return None

    @azure_ds_telemetry_reporter
    def _restart_ephemeral_networking(
        self, iface: Optional[str] = None
    ) -> None:
        """Invalidate existing configuration and restart networking."""
        self._teardown_ephemeral_networking()
        self._setup_ephemeral_networking()

    @azure_ds_telemetry_reporter
    def _teardown_ephemeral_networking(self) -> None:
        """Tear down ephemeral networking."""
        for iface, dhcp_ctx in self._ephemeral_dhcp_ctx.items():
            LOG.debug("Tearing down ephemeral networking for iface %s", iface)
            dhcp_ctx.clean_network()

        self._ephemeral_dhcp_ctx = {}

    @azure_ds_telemetry_reporter
    def _execute_pps(self, pps_type: PPSType) -> None:
        """Execute pre-provisioning protocol.

        Report ready and wait according to protocol, unless we have already
        done so on a prior boot.  Then refresh OVF and metadata to replace
        the initial pre-provisioning configuration.
        """
        # XXX: is this just because of dependency on netlink?
        if util.is_FreeBSD():
            msg = "Free BSD is not supported for PPS VMs"
            report_diagnostic_event(msg, logger_func=LOG.error)
            raise sources.InvalidMetaDataException(msg)

        if not os.path.isfile(REPORTED_READY_MARKER_FILE):
            nl_sock = None
            try:
                # Open netlink socket before reporting ready to ensure we do
                # not have a race condition in detecting any network events
                # after reporting ready.
                nl_sock = netlink.create_bound_netlink_socket()
            except (netlink.NetlinkCreateSocketError) as e:
                report_diagnostic_event(str(e), logger_func=LOG.warning)

            self._report_ready()
            self._write_pps_marker()

            if nl_sock:
                self._wait_to_resume_pps(nl_sock, pps_type)
                nl_sock.close()

            # Refresh DHCP lease for new/updated interface(s).
            self._imds_md = self._setup_ephemeral_networking()

        # Update OVF metadata from reprovisioning data endpoint.
        self._poll_for_reprovision_data()

        # Update metadata from IMDS, if needed.
        if not self._imds_md:
            self._imds_md = get_imds_metadata()

    @azure_ds_telemetry_reporter
    def _wait_to_resume_pps(
        self, nl_sock: socket.socket, pps_type: PPSType
    ) -> None:
        """Wait to resume provisioning until VM has been assigned to customer.

        For Running PPS, this means we wait for media to be reconnected to a
        customer's network.

        For Savable PPS, this means waiting for the customer's NIC to be
        attached.  The provisioning NIC will be detached first.

        If other (unknown) PPS, do not wait, we're limited to polling.
        """
        if pps_type == PPSType.RUNNING:
            iface = self.fallback_interface
            LOG.info(
                "Waiting for media to be reconnected for %s interface", iface
            )
            netlink.wait_for_media_disconnect_connect(nl_sock, iface)
            LOG.info("Media reconnected for %s interface", iface)
        elif pps_type == PPSType.SAVABLE:
            LOG.info("Waiting for NIC to be attached")
            iface = netlink.wait_for_nic_attach_event(nl_sock, [])
            LOG.info("Interface %s attached", iface)

    @azure_ds_telemetry_reporter
    def _cleanup_markers(self):
        """Cleanup any markers used for provisioning."""
        util.del_file(REPORTED_READY_MARKER_FILE)

    @azure_ds_telemetry_reporter
    def _update_metadata_with_imds_data(self) -> None:
        """Compose instance metadata."""
        self.metadata = util.mergemanydict(
            [self.metadata, {"imds": self._imds_md}]
        )

        imds_hostname = _hostname_from_imds(self._imds_md)
        if imds_hostname:
            LOG.debug("Hostname retrieved from IMDS: %s", imds_hostname)
            self.metadata["local-hostname"] = imds_hostname

        imds_disable_password = _disable_password_from_imds(self._imds_md)
        if imds_disable_password:
            LOG.debug(
                "Disable password retrieved from IMDS: %s",
                imds_disable_password,
            )
            self.metadata["disable_password"] = imds_disable_password

        random_seed = _get_random_seed()
        if random_seed:
            self.metadata["random_seed"] = random_seed
        self.metadata["instance-id"] = self._iid()

    @azure_ds_telemetry_reporter
    def _update_config_with_imds_data(self) -> None:
        """Update cloud config from IMDS metadata."""
        imds_username = _username_from_imds(self._imds_md)
        if imds_username:
            LOG.debug("Username retrieved from IMDS: %s", imds_username)
            self.cfg["system_info"]["default_user"]["name"] = imds_username

    @azure_ds_telemetry_reporter
    def crawl_metadata(self) -> None:
        """Walk all instance metadata sources returning a dict on success.

        Possible states on boot:
        (1) Provisioning with PPS v1 (Running).
        (2) Provisioning with PPS v2 (Savable).
        (3) Provisioning with PPS vX (Future/Unknown).
        (4) Rebooted occured after initial reporting ready during PPS,
            but provisioning never completed.
        (5) Provisioning without PPS.
        (6) Normal boot, not provisioning.

        @return: A dictionary of any metadata content for this instance.
        @raise: InvalidMetaDataException when the expected metadata service is
            unavailable, broken or disabled.
        """
        # Find and read local OVF, if available.  Otherwise we must rely on IMDS.
        self._seed = self._load_local_metadata_source()
        if not self._seed:
            self._seed = "IMDS"
            self.cfg = {}
            self._ovf = None

        # Bring up networking on primary NIC.
        self._imds_md = self._setup_ephemeral_networking()

        # Query IMDS data, if needed.
        if not self._imds_md:
            self._imds_md = get_imds_metadata()

        if not self.metadata and not self._imds_md:
            msg = "No OVF or IMDS available"
            report_diagnostic_event(msg)
            raise sources.InvalidMetaDataException(msg)

        pps_type = self._ppstype_from_ovf()
        if pps_type == PPSType.UNKNOWN:
            pps_type = self._ppstype_from_imds()

        if pps_type != PPSType.NONE or os.path.isfile(
            REPORTED_READY_MARKER_FILE
        ):
            self._execute_pps(pps_type)

        if self._seed == "IMDS" and not self._imds_md:
            msg = "No Azure metadata found"
            report_diagnostic_event(msg, logger_func=LOG.error)
            raise sources.InvalidMetaDataException(msg)

        report_diagnostic_event(
            "Found datasource in %s" % self._seed, logger_func=LOG.debug
        )

        # XXX: why, if we have metadata from OVF?
        if self._imds_md:
            self._update_metadata_with_imds_data()
            self._update_config_with_imds_data()

        if not self.userdata_raw and self._imds_md:
            self.userdata_raw = _userdata_from_imds(self._imds_md)  # type: ignore

        # Build minimal OVF if none present.
        if not self._ovf and self._imds_md:
            username = _username_from_imds(self._imds_md) or ""
            hostname = _hostname_from_imds(self._imds_md) or ""
            disable_ssh_pw = (
                _disable_password_from_imds(self._imds_md) or "true"
            )
            self._ovf = build_minimal_ovf(
                username=username,
                hostname=hostname,
                disableSshPwd=disable_ssh_pw,
            )

        # Report ready, if not already done so.
        if not self._negotiated:
            self._report_ready()
            self._negotiated = True

        self._cleanup_markers()
        self._teardown_ephemeral_networking()

    def _is_platform_viable(self):
        """Check platform environment to report if this datasource may run."""
        return _is_platform_viable(self._seed_dir)

    def clear_cached_attrs(self, attr_defaults=()):
        """Reset any cached class attributes to defaults."""
        super(DataSourceAzure, self).clear_cached_attrs(attr_defaults)
        self._metadata_imds = sources.UNSET

    @azure_ds_telemetry_reporter
    def _get_data(self):
        """Crawl and process datasource metadata caching metadata as attrs.

        @return: True on success, False on error, invalid or disabled
            datasource.
        """
        if not self._is_platform_viable():
            return False
        try:
            get_boot_telemetry()
        except Exception as e:
            LOG.warning("Failed to get boot telemetry: %s", e)

        try:
            get_system_info()
        except Exception as e:
            LOG.warning("Failed to get system information: %s", e)

        self.distro.networking.blacklist_drivers = BLACKLIST_DRIVERS

        try:
            util.log_time(
                logfunc=LOG.debug,
                msg="Crawl of metadata service",
                func=self.crawl_metadata,
            )
        except Exception as e:
            import traceback

            track = traceback.format_exc()
            report_diagnostic_event(
                "Could not crawl Azure metadata: %s %r" % (e, track),
                logger_func=LOG.error,
            )
            self._report_failure(
                description=DEFAULT_REPORT_FAILURE_USER_VISIBLE_MESSAGE
            )
            self._teardown_ephemeral_networking()
            return False

        if (
            self.distro
            and self.distro.name == "ubuntu"
            and self.ds_cfg.get("apply_network_config")
        ):
            maybe_remove_ubuntu_network_config_scripts()

        # Process crawled data and augment with various config defaults

        # Only merge in default cloud config related to the ephemeral disk
        # if the ephemeral disk exists
        devpath = RESOURCE_DISK_PATH
        if os.path.exists(devpath):
            report_diagnostic_event(
                "Ephemeral resource disk '%s' exists. "
                "Merging default Azure cloud ephemeral disk configs."
                % devpath,
                logger_func=LOG.debug,
            )
            self.cfg = util.mergemanydict(
                [self.cfg, BUILTIN_CLOUD_EPHEMERAL_DISK_CONFIG]
            )
        else:
            report_diagnostic_event(
                "Ephemeral resource disk '%s' does not exist. "
                "Not merging default Azure cloud ephemeral disk configs."
                % devpath,
                logger_func=LOG.debug,
            )

        self._metadata_imds = self.metadata["imds"]
        self.metadata = util.mergemanydict([self.metadata, DEFAULT_METADATA])

        user_ds_cfg = util.get_cfg_by_path(self.cfg, DS_CFG_PATH, {})
        self.ds_cfg = util.mergemanydict([user_ds_cfg, self.ds_cfg])

        # walinux agent writes files world readable, but expects
        # the directory to be protected.
        # XXX: why write this on every boot?
        write_files(
            self.ds_cfg["data_dir"], {"ovf-env.xml": self._ovf}, dirmode=0o700
        )

        self._teardown_ephemeral_networking()
        return True

    def device_name_to_device(self, name):
        return self.ds_cfg["disk_aliases"].get(name)

    @azure_ds_telemetry_reporter
    def get_public_ssh_keys(self):
        """
        Retrieve public SSH keys.
        """

        return self._get_public_ssh_keys_and_source().ssh_keys

    def _get_public_ssh_keys_and_source(self):
        """
        Try to get the ssh keys from IMDS first, and if that fails
        (i.e. IMDS is unavailable) then fallback to getting the ssh
        keys from OVF.

        The benefit to getting keys from IMDS is a large performance
        advantage, so this is a strong preference. But we must keep
        OVF as a second option for environments that don't have IMDS.
        """

        LOG.debug("Retrieving public SSH keys")
        ssh_keys = []
        keys_from_imds = True
        LOG.debug("Attempting to get SSH keys from IMDS")
        try:
            ssh_keys = [
                public_key["keyData"]
                for public_key in self.metadata["imds"]["compute"][
                    "publicKeys"
                ]
            ]
            for key in ssh_keys:
                if not _key_is_openssh_formatted(key=key):
                    keys_from_imds = False
                    break

            if not keys_from_imds:
                log_msg = "Keys not in OpenSSH format, using OVF"
            else:
                log_msg = "Retrieved {} keys from IMDS".format(
                    len(ssh_keys) if ssh_keys is not None else 0
                )
        except KeyError:
            log_msg = "Unable to get keys from IMDS, falling back to OVF"
            keys_from_imds = False
        finally:
            report_diagnostic_event(log_msg, logger_func=LOG.debug)

        if not keys_from_imds:
            LOG.debug("Attempting to get SSH keys from OVF")
            try:
                ssh_keys = self.metadata["public-keys"]
                log_msg = "Retrieved {} keys from OVF".format(len(ssh_keys))
            except KeyError:
                log_msg = "No keys available from OVF"
            finally:
                report_diagnostic_event(log_msg, logger_func=LOG.debug)

        return SSHKeys(keys_from_imds=keys_from_imds, ssh_keys=ssh_keys)

    def get_config_obj(self):
        return self.cfg

    def check_instance_id(self, sys_cfg):
        # quickly (local check only) if self.instance_id is still valid
        return sources.instance_id_matches_system_uuid(self.get_instance_id())

    def _iid(self, previous=None):
        prev_iid_path = os.path.join(
            self.paths.get_cpath("data"), "instance-id"
        )
        # Older kernels than 4.15 will have UPPERCASE product_uuid.
        # We don't want Azure to react to an UPPER/lower difference as a new
        # instance id as it rewrites SSH host keys.
        # LP: #1835584
        iid = dmi.read_dmi_data("system-uuid").lower()
        if os.path.exists(prev_iid_path):
            previous = util.load_file(prev_iid_path).strip()
            if previous.lower() == iid:
                # If uppercase/lowercase equivalent, return the previous value
                # to avoid new instance id.
                return previous
            if is_byte_swapped(previous.lower(), iid):
                return previous
        return iid

    @azure_ds_telemetry_reporter
    def _report_failure(self, description=None) -> bool:
        """Tells the Azure fabric that provisioning has failed.

        @param description: A description of the error encountered.
        @return: The success status of sending the failure signal.
        """
        reported = False
        self._setup_ephemeral_networking()
        try:
            report_failure_to_fabric(
                endpoint=self._wireserver_address,
                description=description,
            )
            reported = True
        except Exception as e:
            report_diagnostic_event(
                "Failed to report failure using "
                "cached ephemeral dhcp context: %s" % e,
                logger_func=LOG.error,
            )

        if not reported:
            self._restart_ephemeral_networking()

            try:
                report_diagnostic_event(
                    "Using new ephemeral dhcp to report failure to Azure",
                    logger_func=LOG.debug,
                )
                report_failure_to_fabric(
                    endpoint=self._wireserver_address,
                    description=description,
                )
                reported = True
            except Exception as e:
                report_diagnostic_event(
                    "Failed to report failure using new ephemeral dhcp: %s"
                    % e,
                    logger_func=LOG.debug,
                )

        self._teardown_ephemeral_networking()
        return reported

    def _report_ready(self) -> bool:
        """Tells the fabric provisioning has completed.

        @param lease: dhcp lease to use for sending the ready signal.
        @return: The success status of sending the ready signal.
        """
        try:
            get_metadata_from_fabric(
                endpoint=self._wireserver_address,
                iso_dev=self._iso_dev,
            )
            return True
        except Exception as e:
            report_diagnostic_event(
                "Error communicating with Azure fabric; You may experience "
                "connectivity issues: %s" % e,
                logger_func=LOG.warning,
            )
            return False

    @azure_ds_telemetry_reporter
    def _write_pps_marker(self) -> None:
        """Write PPS marker file to indicate we reported ready during PPS."""
        util.write_file(
            REPORTED_READY_MARKER_FILE,
            "{pid}: {time}\n".format(pid=os.getpid(), time=time()),
        )
        report_diagnostic_event(
            "Successfully created reported ready marker file "
            "while in the preprovisioning pool.",
            logger_func=LOG.debug,
        )

    def _ppstype_from_imds(self) -> PPSType:
        try:
            return PPSType(self._imds_md["extended"]["compute"]["ppsType"])
        except KeyError as e:
            report_diagnostic_event(
                "Could not retrieve pps configuration from IMDS: %s" % e,
                logger_func=LOG.debug,
            )
            return PPSType.UNKNOWN
        except ValueError as error:
            report_diagnostic_event(
                "Unknown PPS type from OVF: %s", logger_func=LOG.error
            )
            return PPSType.UNKNOWN

    def _ppstype_from_ovf(self) -> PPSType:
        if self.cfg.get("PreprovisionedVm") is True:
            return PPSType.RUNNING

        pps_type = self.cfg.get("PreprovisionedVMType", "None")
        try:
            return PPSType(pps_type)
        except ValueError as error:
            report_diagnostic_event(
                "Unknown PPS type from OVF: %s", logger_func=LOG.error
            )
            return PPSType.UNKNOWN

    @azure_ds_telemetry_reporter
    def _poll_for_reprovision_data(self) -> None:
        """Poll indefinitely for reprovision data."""
        while True:
            try:
                self._ovf = _fetch_imds_data(
                    api_version=IMDS_VER_MIN,
                    md_type=MetadataType.REPROVISIONDATA,
                    retries=3600,
                    max_retries_for_connection_errors=4,
                )
                break
            except UrlError as e:
                report_diagnostic_event(
                    "Error fetching reprovision data: %s" % e,
                    logger_func=LOG.error,
                )

                # If we don't have an HTTP error code, restart networking as
                # a timeout / connection failure indicates that either we
                # have some sort of misconfiguration, or the primary NIC was
                # not available prior entering this polling loop.
                if not e.code:
                    self._restart_ephemeral_networking()

        report_diagnostic_event(
            "Succesfully fetched reprovision data", logger_func=LOG.info
        )
        self.metadata, self.userdata_raw, self.cfg = read_azure_ovf(self._ovf)

    @azure_ds_telemetry_reporter
    def activate(self, cfg, is_new_instance):
        try:
            address_ephemeral_resize(
                is_new_instance=is_new_instance,
                preserve_ntfs=self.ds_cfg.get(DS_CFG_KEY_PRESERVE_NTFS, False),
            )
        finally:
            push_log_to_kvp(self.sys_cfg["def_log_file"])
        return

    @property
    def availability_zone(self):
        return (
            self.metadata.get("imds", {})
            .get("compute", {})
            .get("platformFaultDomain")
        )

    @property
    def network_config(self):
        """Generate a network config like net.generate_fallback_network() with
        the following exceptions.

        1. Probe the drivers of the net-devices present and inject them in
           the network configuration under params: driver: <driver> value
        2. Generate a fallback network config that does not include any of
           the blacklisted devices.
        """
        if not self._network_config or self._network_config == sources.UNSET:
            if self.ds_cfg.get("apply_network_config"):
                nc_src = self._metadata_imds
            else:
                nc_src = None
            self._network_config = parse_network_config(nc_src)
        return self._network_config

    @property
    def region(self):
        return self.metadata.get("imds", {}).get("compute", {}).get("location")


def _username_from_imds(imds_data) -> Optional[str]:
    try:
        return imds_data["compute"]["osProfile"]["adminUsername"]
    except KeyError:
        return None


def _userdata_from_imds(imds_data) -> Optional[bytes]:
    """Get decoded userdata byte string from IMDS metadata."""
    try:
        encoded_userdata = imds_data["compute"]["userData"]
    except KeyError:
        return None

    if encoded_userdata:
        LOG.debug("Retrieved userdata from IMDS")
        try:
            return base64.b64decode("".join(encoded_userdata.split()))
        except Exception:
            report_diagnostic_event(
                "Bad userdata in IMDS", logger_func=LOG.warning
            )
    return None


def _hostname_from_imds(imds_data):
    try:
        return imds_data["compute"]["osProfile"]["computerName"]
    except KeyError:
        return None


def _disable_password_from_imds(imds_data):
    try:
        return (
            imds_data["compute"]["osProfile"]["disablePasswordAuthentication"]
            == "true"
        )
    except KeyError:
        return None


def _key_is_openssh_formatted(key):
    """
    Validate whether or not the key is OpenSSH-formatted.
    """
    # See https://bugs.launchpad.net/cloud-init/+bug/1910835
    if "\r\n" in key.strip():
        return False

    parser = ssh_util.AuthKeyLineParser()
    try:
        akl = parser.parse(key)
    except TypeError:
        return False

    return akl.keytype is not None


def _partitions_on_device(devpath, maxnum=16):
    # return a list of tuples (ptnum, path) for each part on devpath
    for suff in ("-part", "p", ""):
        found = []
        for pnum in range(1, maxnum):
            ppath = devpath + suff + str(pnum)
            if os.path.exists(ppath):
                found.append((pnum, os.path.realpath(ppath)))
        if found:
            return found
    return []


@azure_ds_telemetry_reporter
def _has_ntfs_filesystem(devpath):
    ntfs_devices = util.find_devs_with("TYPE=ntfs", no_cache=True)
    LOG.debug("ntfs_devices found = %s", ntfs_devices)
    return os.path.realpath(devpath) in ntfs_devices


@azure_ds_telemetry_reporter
def can_dev_be_reformatted(devpath, preserve_ntfs):
    """Determine if the ephemeral drive at devpath should be reformatted.

    A fresh ephemeral disk is formatted by Azure and will:
      a.) have a partition table (dos or gpt)
      b.) have 1 partition that is ntfs formatted, or
          have 2 partitions with the second partition ntfs formatted.
          (larger instances with >2TB ephemeral disk have gpt, and will
           have a microsoft reserved partition as part 1.  LP: #1686514)
      c.) the ntfs partition will have no files other than possibly
          'dataloss_warning_readme.txt'

    User can indicate that NTFS should never be destroyed by setting
    DS_CFG_KEY_PRESERVE_NTFS in dscfg.
    If data is found on NTFS, user is warned to set DS_CFG_KEY_PRESERVE_NTFS
    to make sure cloud-init does not accidentally wipe their data.
    If cloud-init cannot mount the disk to check for data, destruction
    will be allowed, unless the dscfg key is set."""
    if preserve_ntfs:
        msg = "config says to never destroy NTFS (%s.%s), skipping checks" % (
            ".".join(DS_CFG_PATH),
            DS_CFG_KEY_PRESERVE_NTFS,
        )
        return False, msg

    if not os.path.exists(devpath):
        return False, "device %s does not exist" % devpath

    LOG.debug(
        "Resolving realpath of %s -> %s", devpath, os.path.realpath(devpath)
    )

    # devpath of /dev/sd[a-z] or /dev/disk/cloud/azure_resource
    # where partitions are "<devpath>1" or "<devpath>-part1" or "<devpath>p1"
    partitions = _partitions_on_device(devpath)
    if len(partitions) == 0:
        return False, "device %s was not partitioned" % devpath
    elif len(partitions) > 2:
        msg = "device %s had 3 or more partitions: %s" % (
            devpath,
            " ".join([p[1] for p in partitions]),
        )
        return False, msg
    elif len(partitions) == 2:
        cand_part, cand_path = partitions[1]
    else:
        cand_part, cand_path = partitions[0]

    if not _has_ntfs_filesystem(cand_path):
        msg = "partition %s (%s) on device %s was not ntfs formatted" % (
            cand_part,
            cand_path,
            devpath,
        )
        return False, msg

    @azure_ds_telemetry_reporter
    def count_files(mp):
        ignored = set(["dataloss_warning_readme.txt"])
        return len([f for f in os.listdir(mp) if f.lower() not in ignored])

    bmsg = "partition %s (%s) on device %s was ntfs formatted" % (
        cand_part,
        cand_path,
        devpath,
    )

    with events.ReportEventStack(
        name="mount-ntfs-and-count",
        description="mount-ntfs-and-count",
        parent=azure_ds_reporter,
    ) as evt:
        try:
            file_count = util.mount_cb(
                cand_path,
                count_files,
                mtype="ntfs",
                update_env_for_mount={"LANG": "C"},
            )
        except util.MountFailedError as e:
            evt.description = "cannot mount ntfs"
            if "unknown filesystem type 'ntfs'" in str(e):
                return (
                    True,
                    (
                        bmsg + " but this system cannot mount NTFS,"
                        " assuming there are no important files."
                        " Formatting allowed."
                    ),
                )
            return False, bmsg + " but mount of %s failed: %s" % (cand_part, e)

        if file_count != 0:
            evt.description = "mounted and counted %d files" % file_count
            LOG.warning(
                "it looks like you're using NTFS on the ephemeral"
                " disk, to ensure that filesystem does not get wiped,"
                " set %s.%s in config",
                ".".join(DS_CFG_PATH),
                DS_CFG_KEY_PRESERVE_NTFS,
            )
            return False, bmsg + " but had %d files on it." % file_count

    return True, bmsg + " and had no important files. Safe for reformatting."


@azure_ds_telemetry_reporter
def address_ephemeral_resize(
    devpath=RESOURCE_DISK_PATH, is_new_instance=False, preserve_ntfs=False
):
    if not os.path.exists(devpath):
        report_diagnostic_event(
            "Ephemeral resource disk '%s' does not exist." % devpath,
            logger_func=LOG.debug,
        )
        return
    else:
        report_diagnostic_event(
            "Ephemeral resource disk '%s' exists." % devpath,
            logger_func=LOG.debug,
        )

    result = False
    msg = None
    if is_new_instance:
        result, msg = (True, "First instance boot.")
    else:
        result, msg = can_dev_be_reformatted(devpath, preserve_ntfs)

    LOG.debug("reformattable=%s: %s", result, msg)
    if not result:
        return

    for mod in ["disk_setup", "mounts"]:
        sempath = "/var/lib/cloud/instance/sem/config_" + mod
        bmsg = 'Marker "%s" for module "%s"' % (sempath, mod)
        if os.path.exists(sempath):
            try:
                os.unlink(sempath)
                LOG.debug("%s removed.", bmsg)
            except Exception as e:
                # python3 throws FileNotFoundError, python2 throws OSError
                LOG.warning("%s: remove failed! (%s)", bmsg, e)
        else:
            LOG.debug("%s did not exist.", bmsg)
    return


@azure_ds_telemetry_reporter
def write_files(datadir, files, dirmode=None):
    def _redact_password(cnt, fname):
        """Azure provides the UserPassword in plain text. So we redact it"""
        try:
            root = ET.fromstring(cnt)
            for elem in root.iter():
                if (
                    "UserPassword" in elem.tag
                    and elem.text != DEF_PASSWD_REDACTION
                ):
                    elem.text = DEF_PASSWD_REDACTION
            return ET.tostring(root)
        except Exception:
            LOG.critical("failed to redact userpassword in %s", fname)
            return cnt

    if not datadir:
        return
    if not files:
        files = {}
    util.ensure_dir(datadir, dirmode)
    for (name, content) in files.items():
        fname = os.path.join(datadir, name)
        if "ovf-env.xml" in name:
            content = _redact_password(content, fname)
        util.write_file(filename=fname, content=content, mode=0o600)


def find_child(node, filter_func):
    ret = []
    if not node.hasChildNodes():
        return ret
    for child in node.childNodes:
        if filter_func(child):
            ret.append(child)
    return ret


@azure_ds_telemetry_reporter
def load_azure_ovf_pubkeys(sshnode):
    # This parses a 'SSH' node formatted like below, and returns
    # an array of dicts.
    #  [{'fingerprint': '6BE7A7C3C8A8F4B123CCA5D0C2F1BE4CA7B63ED7',
    #    'path': '/where/to/go'}]
    #
    # <SSH><PublicKeys>
    #   <PublicKey><Fingerprint>ABC</FingerPrint><Path>/x/y/z</Path>
    #   ...
    # </PublicKeys></SSH>
    # Under some circumstances, there may be a <Value> element along with the
    # Fingerprint and Path. Pass those along if they appear.
    results = find_child(sshnode, lambda n: n.localName == "PublicKeys")
    if len(results) == 0:
        return []
    if len(results) > 1:
        raise BrokenAzureDataSource(
            "Multiple 'PublicKeys'(%s) in SSH node" % len(results)
        )

    pubkeys_node = results[0]
    pubkeys = find_child(pubkeys_node, lambda n: n.localName == "PublicKey")

    if len(pubkeys) == 0:
        return []

    found = []
    text_node = minidom.Document.TEXT_NODE

    for pk_node in pubkeys:
        if not pk_node.hasChildNodes():
            continue

        cur = {"fingerprint": "", "path": "", "value": ""}
        for child in pk_node.childNodes:
            if child.nodeType == text_node or not child.localName:
                continue

            name = child.localName.lower()

            if name not in cur.keys():
                continue

            if (
                len(child.childNodes) != 1
                or child.childNodes[0].nodeType != text_node
            ):
                continue

            cur[name] = child.childNodes[0].wholeText.strip()
        found.append(cur)

    return found


@azure_ds_telemetry_reporter
def read_azure_ovf(contents):
    try:
        dom = minidom.parseString(contents)
    except Exception as e:
        error_str = "Invalid ovf-env.xml: %s" % e
        report_diagnostic_event(error_str, logger_func=LOG.warning)
        raise BrokenAzureDataSource(error_str) from e

    results = find_child(
        dom.documentElement, lambda n: n.localName == "ProvisioningSection"
    )

    if len(results) == 0:
        raise NonAzureDataSource("No ProvisioningSection")
    if len(results) > 1:
        raise BrokenAzureDataSource(
            "found '%d' ProvisioningSection items" % len(results)
        )
    provSection = results[0]

    lpcs_nodes = find_child(
        provSection,
        lambda n: n.localName == "LinuxProvisioningConfigurationSet",
    )

    if len(lpcs_nodes) == 0:
        raise NonAzureDataSource("No LinuxProvisioningConfigurationSet")
    if len(lpcs_nodes) > 1:
        raise BrokenAzureDataSource(
            "found '%d' %ss"
            % (len(lpcs_nodes), "LinuxProvisioningConfigurationSet")
        )
    lpcs = lpcs_nodes[0]

    if not lpcs.hasChildNodes():
        raise BrokenAzureDataSource("no child nodes of configuration set")

    md_props = "seedfrom"
    md = {"azure_data": {}}
    cfg = {}
    ud = ""
    password = None
    username = None

    for child in lpcs.childNodes:
        if child.nodeType == dom.TEXT_NODE or not child.localName:
            continue

        name = child.localName.lower()

        simple = False
        value = ""
        if (
            len(child.childNodes) == 1
            and child.childNodes[0].nodeType == dom.TEXT_NODE
        ):
            simple = True
            value = child.childNodes[0].wholeText

        attrs = dict([(k, v) for k, v in child.attributes.items()])

        # we accept either UserData or CustomData.  If both are present
        # then behavior is undefined.
        if name == "userdata" or name == "customdata":
            if attrs.get("encoding") in (None, "base64"):
                ud = base64.b64decode("".join(value.split()))
            else:
                ud = value
        elif name == "username":
            username = value
        elif name == "userpassword":
            password = value
        elif name == "hostname":
            md["local-hostname"] = value
        elif name == "dscfg":
            if attrs.get("encoding") in (None, "base64"):
                dscfg = base64.b64decode("".join(value.split()))
            else:
                dscfg = value
            cfg["datasource"] = {DS_NAME: util.load_yaml(dscfg, default={})}
        elif name == "ssh":
            cfg["_pubkeys"] = load_azure_ovf_pubkeys(child)
        elif name == "disablesshpasswordauthentication":
            cfg["ssh_pwauth"] = util.is_false(value)
        elif simple:
            if name in md_props:
                md[name] = value
            else:
                md["azure_data"][name] = value

    defuser = {}
    if username:
        defuser["name"] = username
    if password:
        defuser["lock_passwd"] = False
        if DEF_PASSWD_REDACTION != password:
            defuser["passwd"] = cfg["password"] = encrypt_pass(password)

    if defuser:
        cfg["system_info"] = {"default_user": defuser}

    if "ssh_pwauth" not in cfg and password:
        cfg["ssh_pwauth"] = True

    preprovisioning_cfg = _get_preprovisioning_cfgs(dom)
    cfg = util.mergemanydict([cfg, preprovisioning_cfg])

    return (md, ud, cfg)


@azure_ds_telemetry_reporter
def _get_preprovisioning_cfgs(dom):
    """Read the preprovisioning related flags from ovf and populates a dict
    with the info.

    Two flags are in use today: PreprovisionedVm bool and
    PreprovisionedVMType enum. In the long term, the PreprovisionedVm bool
    will be deprecated in favor of PreprovisionedVMType string/enum.

    Only these combinations of values are possible today:
        - PreprovisionedVm=True and PreprovisionedVMType=Running
        - PreprovisionedVm=False and PreprovisionedVMType=Savable
        - PreprovisionedVm is missing and PreprovisionedVMType=Running/Savable
        - PreprovisionedVm=False and PreprovisionedVMType is missing

    More specifically, this will never happen:
        - PreprovisionedVm=True and PreprovisionedVMType=Savable
    """
    cfg = {"PreprovisionedVm": False, "PreprovisionedVMType": None}

    platform_settings_section = find_child(
        dom.documentElement, lambda n: n.localName == "PlatformSettingsSection"
    )
    if not platform_settings_section or len(platform_settings_section) == 0:
        LOG.debug("PlatformSettingsSection not found")
        return cfg
    platform_settings = find_child(
        platform_settings_section[0],
        lambda n: n.localName == "PlatformSettings",
    )
    if not platform_settings or len(platform_settings) == 0:
        LOG.debug("PlatformSettings not found")
        return cfg

    # Read the PreprovisionedVm bool flag. This should be deprecated when the
    # platform has removed PreprovisionedVm and only surfaces
    # PreprovisionedVMType.
    cfg["PreprovisionedVm"] = _get_preprovisionedvm_cfg_value(
        platform_settings
    )

    cfg["PreprovisionedVMType"] = _get_preprovisionedvmtype_cfg_value(
        platform_settings
    )
    return cfg


@azure_ds_telemetry_reporter
def _get_preprovisionedvm_cfg_value(platform_settings):
    preprovisionedVm = False

    # Read the PreprovisionedVm bool flag. This should be deprecated when the
    # platform has removed PreprovisionedVm and only surfaces
    # PreprovisionedVMType.
    preprovisionedVmVal = find_child(
        platform_settings[0], lambda n: n.localName == "PreprovisionedVm"
    )
    if not preprovisionedVmVal or len(preprovisionedVmVal) == 0:
        LOG.debug("PreprovisionedVm not found")
        return preprovisionedVm
    preprovisionedVm = util.translate_bool(
        preprovisionedVmVal[0].firstChild.nodeValue
    )

    report_diagnostic_event(
        "PreprovisionedVm: %s" % preprovisionedVm, logger_func=LOG.info
    )

    return preprovisionedVm


@azure_ds_telemetry_reporter
def _get_preprovisionedvmtype_cfg_value(platform_settings):
    preprovisionedVMType = None

    # Read the PreprovisionedVMType value from the ovf. It can be
    # 'Running' or 'Savable' or not exist. This enum value is intended to
    # replace PreprovisionedVm bool flag in the long term.
    # A Running VM is the same as preprovisioned VMs of today. This is
    # equivalent to having PreprovisionedVm=True.
    # A Savable VM is one whose nic is hot-detached immediately after it
    # reports ready the first time to free up the network resources.
    # Once assigned to customer, the customer-requested nics are
    # hot-attached to it and reprovision happens like today.
    preprovisionedVMTypeVal = find_child(
        platform_settings[0], lambda n: n.localName == "PreprovisionedVMType"
    )
    if (
        not preprovisionedVMTypeVal
        or len(preprovisionedVMTypeVal) == 0
        or preprovisionedVMTypeVal[0].firstChild is None
    ):
        LOG.debug("PreprovisionedVMType not found")
        return preprovisionedVMType

    preprovisionedVMType = preprovisionedVMTypeVal[0].firstChild.nodeValue

    report_diagnostic_event(
        "PreprovisionedVMType: %s" % preprovisionedVMType, logger_func=LOG.info
    )

    return preprovisionedVMType


def encrypt_pass(password, salt_id="$6$"):
    return crypt.crypt(password, salt_id + util.rand_str(strlen=16))


@azure_ds_telemetry_reporter
def _check_freebsd_cdrom(cdrom_dev):
    """Return boolean indicating path to cdrom device has content."""
    try:
        with open(cdrom_dev) as fp:
            fp.read(1024)
            return True
    except IOError:
        LOG.debug("cdrom (%s) is not configured", cdrom_dev)
    return False


@azure_ds_telemetry_reporter
def _get_random_seed(source=PLATFORM_ENTROPY_SOURCE):
    """Return content random seed file if available, otherwise,
    return None."""
    # azure / hyper-v provides random data here
    # now update ds_cfg to reflect contents pass in config
    if source is None:
        return None
    seed = util.load_file(source, quiet=True, decode=False)

    # The seed generally contains non-Unicode characters. load_file puts
    # them into a str (in python 2) or bytes (in python 3). In python 2,
    # bad octets in a str cause util.json_dumps() to throw an exception. In
    # python 3, bytes is a non-serializable type, and the handler load_file
    # uses applies b64 encoding *again* to handle it. The simplest solution
    # is to just b64encode the data and then decode it to a serializable
    # string. Same number of bits of entropy, just with 25% more zeroes.
    # There's no need to undo this base64-encoding when the random seed is
    # actually used in cc_seed_random.py.
    seed = base64.b64encode(seed).decode()

    return seed


@azure_ds_telemetry_reporter
def list_possible_azure_ds(seed, cache_dir):
    yield seed
    yield DEFAULT_PROVISIONING_ISO_DEV
    if util.is_FreeBSD():
        cdrom_dev = "/dev/cd0"
        if _check_freebsd_cdrom(cdrom_dev):
            yield cdrom_dev
    else:
        for fstype in ("iso9660", "udf"):
            yield from util.find_devs_with("TYPE=%s" % fstype)
    if cache_dir:
        yield cache_dir


@azure_ds_telemetry_reporter
def parse_network_config(imds_metadata) -> dict:
    """Convert imds_metadata dictionary to network v2 configuration.
    Parses network configuration from imds metadata if present or generate
    fallback network config excluding mlx4_core devices.

    @param: imds_metadata: Dict of content read from IMDS network service.
    @return: Dictionary containing network version 2 standard configuration.
    """
    if imds_metadata != sources.UNSET and imds_metadata:
        try:
            return _generate_network_config_from_imds_metadata(imds_metadata)
        except Exception as e:
            LOG.error(
                "Failed generating network config from IMDS network metadata: %s",
                str(e),
            )
    try:
        return net.generate_fallback_config(
            blacklist_drivers=BLACKLIST_DRIVERS, config_driver=True
        )
    except Exception as e:
        LOG.error("Failed generating fallback network config: %s", str(e))
    return {}


@azure_ds_telemetry_reporter
def _generate_network_config_from_imds_metadata(imds_metadata) -> dict:
    """Convert imds_metadata dictionary to network v2 configuration.
    Parses network configuration from imds metadata.

    @param: imds_metadata: Dict of content read from IMDS network service.
    @return: Dictionary containing network version 2 standard configuration.
    """
    netconfig = {"version": 2, "ethernets": {}}
    network_metadata = imds_metadata["network"]
    for idx, intf in enumerate(network_metadata["interface"]):
        has_ip_address = False
        # First IPv4 and/or IPv6 address will be obtained via DHCP.
        # Any additional IPs of each type will be set as static
        # addresses.
        nicname = "eth{idx}".format(idx=idx)
        dhcp_override = {"route-metric": (idx + 1) * 100}
        dev_config = {
            "dhcp4": True,
            "dhcp4-overrides": dhcp_override,
            "dhcp6": False,
        }
        for addr_type in ("ipv4", "ipv6"):
            addresses = intf.get(addr_type, {}).get("ipAddress", [])
            # If there are no available IP addresses, then we don't
            # want to add this interface to the generated config.
            if not addresses:
                continue
            has_ip_address = True
            if addr_type == "ipv4":
                default_prefix = "24"
            else:
                default_prefix = "128"
                if addresses:
                    dev_config["dhcp6"] = True
                    # non-primary interfaces should have a higher
                    # route-metric (cost) so default routes prefer
                    # primary nic due to lower route-metric value
                    dev_config["dhcp6-overrides"] = dhcp_override
            for addr in addresses[1:]:
                # Append static address config for ip > 1
                netPrefix = intf[addr_type]["subnet"][0].get(
                    "prefix", default_prefix
                )
                privateIp = addr["privateIpAddress"]
                if not dev_config.get("addresses"):
                    dev_config["addresses"] = []
                dev_config["addresses"].append(  # type: ignore
                    "{ip}/{prefix}".format(ip=privateIp, prefix=netPrefix)
                )
        if dev_config and has_ip_address:
            mac = ":".join(re.findall(r"..", intf["macAddress"]))
            dev_config.update(
                {"match": {"macaddress": mac.lower()}, "set-name": nicname}
            )
            # With netvsc, we can get two interfaces that
            # share the same MAC, so we need to make sure
            # our match condition also contains the driver
            driver = device_driver(nicname)
            if driver and driver == "hv_netvsc":
                dev_config["match"]["driver"] = driver  # type: ignore
            netconfig["ethernets"][nicname] = dev_config  # type: ignore
    return netconfig


@azure_ds_telemetry_reporter
def _fetch_imds_data(
    retries: int = 10,
    md_type: MetadataType = MetadataType.ALL,
    api_version: str = IMDS_VER_MIN,
    max_retries_for_connection_errors: Optional[int] = None,
) -> bytes:
    """Fetch metadata from IMDS.

    :param retries: Number of retries to attempt. -1 for infinite.
    :param md_type: Type of metadata to fetch.
    :param api_version: Version of API to request.
    :param max_retries_for_connection_errors: Max number of retries to allow on timeout.

    :raises UrlError: On error.

    :return: Fetched data.
    """
    url = "{}?api-version={}".format(md_type.value, api_version)
    headers = {"Metadata": "true"}

    # Support for extended metadata begins with 2021-03-01
    if api_version >= IMDS_EXTENDED_VER_MIN and md_type == MetadataType.ALL:
        url = url + "&extended=true"

    cb_error_count = 0
    cb_last_log = None
    cb_logging_threshold_count = 1
    cb_max_retries_for_connection_errors = max_retries_for_connection_errors

    def exception_cb(request_args: dict, error: UrlError) -> bool:
        nonlocal cb_error_count, cb_last_log, cb_logging_threshold_count, cb_max_retries_for_connection_errors
        cb_error_count += 1

        # Log using backoff threshold unless error is different from previous.
        log = "Error requesting IMDS data. Args: %s Exception: %s Code: %s" % (
            request_args,
            error.cause,
            error.code,
        )
        # Remove obj refs for reliable comparison.
        log = re.sub(r" object at 0x[0-9a-f]+", "", log)
        if log != cb_last_log or cb_error_count == cb_logging_threshold_count:
            cb_last_log = log
            log += " (failed %d attempts)" % cb_error_count
            report_diagnostic_event(log, logger_func=LOG.warning)
            cb_logging_threshold_count *= 2

        # Retry for:
        # - timeout & unreachable errors
        # - code 404 = not found (yet)
        # - code 410 = gone / unavailable (yet)
        # - code 429 = rate-limited/throttled
        # - code 500 = server error
        # Never retry for:
        # - code 400 = bad request (unsupported API version or malformed request)
        if error.code in (404, 410, 429, 500):
            return True

        # If no HTTP code, this is a connection failure such as
        # requests.Timeout, requests.ConnectionError, etc.
        if cb_max_retries_for_connection_errors and not error.code:
            cb_max_retries_for_connection_errors -= 1
            return cb_max_retries_for_connection_errors >= 0

        return False

    try:
        return readurl(
            url,
            timeout=IMDS_TIMEOUT_IN_SECONDS,
            headers=headers,
            retries=retries,
            exception_cb=exception_cb,
            infinite=False,
            log_req_resp=False,
        ).contents
    except UrlError as error:
        report_diagnostic_event(
            "Failed to request IMDS data.  URL: %s Args: %s Exception: %s Code: %s"
            % (url, error, error.cause, error.code),
            logger_func=LOG.warning,
        )
        raise


@azure_ds_telemetry_reporter
def _parse_imds_metadata(data: bytes) -> Optional[dict]:
    """Parse IMDS metadata.

    :return: Parsed metadata dictionary or None on error.
    """
    parsed_data = None
    if data:
        try:
            parsed_data = util.load_json(data)
        except (ValueError, TypeError) as e:
            report_diagnostic_event(
                "Ignoring non-json IMDS instance metadata response: %s. "
                "Loading non-json IMDS response failed: %s" % (str(data), e),
                logger_func=LOG.error,
            )
            return None

    if isinstance(parsed_data, dict):
        return parsed_data

    report_diagnostic_event(
        "Failed to parse IMDS metadata: %r" % data,
        logger_func=LOG.error,
    )
    return None


@azure_ds_telemetry_reporter
def get_imds_metadata(
    retries: int = 10, api_version: str = IMDS_VER_WANT
) -> Optional[dict]:
    """Fetch metadata from IMDS using IMDS_VER_WANT API version.

    Falls back to IMDS_VER_MIN version if IMDS returns a 400 error code.

    :return: Parsed metadata dictionary or None on error.
    """
    data = None
    try:
        data = _fetch_imds_data(
            retries=retries,
            md_type=MetadataType.ALL,
            api_version=IMDS_VER_WANT,
        )
    except UrlError as err:
        # Raise any error not related to API support.
        if err.code != 400:
            return None

    if not data:
        report_diagnostic_event(
            "Falling back to IMDS api-version: %s" % (IMDS_VER_MIN),
            logger_func=LOG.warning,
        )
        try:
            data = _fetch_imds_data(
                retries=retries,
                md_type=MetadataType.ALL,
                api_version=IMDS_VER_MIN,
            )
        except UrlError as err:
            report_diagnostic_event(
                "Error fetching IMDS metadata: %s" % err,
                logger_func=LOG.error,
            )
            return None

    return _parse_imds_metadata(data)


@azure_ds_telemetry_reporter
def perform_dhcp(iface: str) -> EphemeralDHCPv4:
    """Perform DHCP on specified interface.

    :param iface: Iinterface to use.

    :raises NoDHCPLeaseError: on error.

    :return: DHCP context of NIC.
    """
    try:
        dhcp_ctx = EphemeralDHCPv4(iface=iface, dhcp_log_func=dhcp_log_cb)
        dhcp_ctx.obtain_lease()
        return dhcp_ctx
    except NoDHCPLeaseError as e:
        report_diagnostic_event(
            "Giving up. Failed to obtain dhcp lease for %s due to %s."
            % (iface, e),
            logger_func=LOG.error,
        )
        raise


@azure_ds_telemetry_reporter
def maybe_remove_ubuntu_network_config_scripts(paths=None):
    """Remove Azure-specific ubuntu network config for non-primary nics.

    @param paths: List of networking scripts or directories to remove when
        present.

    In certain supported ubuntu images, static udev rules or netplan yaml
    config is delivered in the base ubuntu image to support dhcp on any
    additional interfaces which get attached by a customer at some point
    after initial boot. Since the Azure datasource can now regenerate
    network configuration as metadata reports these new devices, we no longer
    want the udev rules or netplan's 90-hotplug-azure.yaml to configure
    networking on eth1 or greater as it might collide with cloud-init's
    configuration.

    Remove the any existing extended network scripts if the datasource is
    enabled to write network per-boot.
    """
    if not paths:
        paths = UBUNTU_EXTENDED_NETWORK_SCRIPTS
    logged = False
    for path in paths:
        if os.path.exists(path):
            if not logged:
                LOG.info(
                    "Removing Ubuntu extended network scripts because"
                    " cloud-init updates Azure network configuration on the"
                    " following events: %s.",
                    [EventType.BOOT.value, EventType.BOOT_LEGACY.value],
                )
                logged = True
            if os.path.isdir(path):
                util.del_dir(path)
            else:
                util.del_file(path)


def _is_platform_viable(seed_dir):
    """Check platform environment to report if this datasource may run."""
    with events.ReportEventStack(
        name="check-platform-viability",
        description="found azure asset tag",
        parent=azure_ds_reporter,
    ) as evt:
        asset_tag = dmi.read_dmi_data("chassis-asset-tag")
        if asset_tag == AZURE_CHASSIS_ASSET_TAG:
            return True
        msg = "Non-Azure DMI asset tag '%s' discovered." % asset_tag
        evt.description = msg
        report_diagnostic_event(msg, logger_func=LOG.debug)
        if os.path.exists(os.path.join(seed_dir, "ovf-env.xml")):
            return True
        return False


class BrokenAzureDataSource(Exception):
    pass


class NonAzureDataSource(Exception):
    pass


# Legacy: Must be present in case we load an old pkl object
DataSourceAzureNet = DataSourceAzure

# Used to match classes to dependencies
datasources = [
    (DataSourceAzure, (sources.DEP_FILESYSTEM,)),
]


# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)


# vi: ts=4 expandtab
