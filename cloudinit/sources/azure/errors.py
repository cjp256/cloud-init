# Copyright (C) 2022 Microsoft Corporation.
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
import logging
import json
import traceback
from enum import Enum
from typing import Any, Dict, Optional

import requests
from cloudinit.url_helper import UrlError

LOG = logging.getLogger(__name__)


class ReportableErrorCode(Enum):
    CLOUDINIT = "PROVISIONING_FAILED_CLOUDINIT"
    DHCP = "PROVISIONING_FAILED_DHCP"
    IMAGE = "PROVISIONING_FAILED_IMAGE"
    IMDS = "PROVISIONING_FAILED_INSTANCE_METADATA_SERVICE_ERROR"
    MEDIA = "PROVISIONING_FAILED_MEDIA_ERROR"
    PPS = "PROVISIONING_FAILED_PPS"
    WIRESERVER = "PROVISIONING_FAILED_WIRESERVER"


class ReportableError(Exception):
    def __init__(
        self, error_code: ReportableErrorCode, supporting_data: Dict[str, Any]
    ) -> None:
        self.error_code = error_code
        self.supporting_data = supporting_data

    def as_description(self) -> str:
        supporting_json = json.dumps(self.supporting_data, sort_keys=True)
        return (
            f"{self.error_code.value}: "
            f"{supporting_json}. For more information, check out "
            f"https://aka.ms/linuxprovisioningerror#{self.error_code.value}"
        )


class ReportableErrorCloudInitTestForcedFailure(ReportableError):
    def __init__(self) -> None:
        supporting_data = {
            "reason": "forced deployment failure for testing purposes"
        }
        super().__init__(ReportableErrorCode.CLOUDINIT, supporting_data)


class ReportableErrorCloudInitDmiFailure(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.CLOUDINIT, supporting_data)


class ReportableErrorCloudInitException(ReportableError):
    def __init__(self, exception: Exception, trace: str) -> None:
        trace_base64 = base64.b64encode(trace.encode("utf-8"))
        supporting_data = {
            "reason": "unhandled exception",
            "exception": repr(exception),
            "traceback_base64": trace_base64,
        }
        super().__init__(ReportableErrorCode.CLOUDINIT, supporting_data)


class ReportableErrorCloudInitUnsupportedOs(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.CLOUDINIT, supporting_data)


class ReportableErrorDhcpFailure(ReportableError):
    def __init__(self) -> None:
        supporting_data = {"reason": "failed to obtain DHCP lease"}
        super().__init__(ReportableErrorCode.DHCP, supporting_data)


class ReportableErrorImageMissingDhclient(ReportableError):
    def __init__(self) -> None:
        supporting_data = {
            "reason": "image is missing dhclient which is required for cloud-init"
        }
        super().__init__(ReportableErrorCode.IMAGE, supporting_data)


class ReportableErrorImdsApiVersionUnsupported(ReportableError):
    def __init__(
        self, *, error: UrlError, retries: int, timeout_seconds: float
    ) -> None:
        supporting_data = {
            "reason": "server returned API version unsupported",
            "error": str(error),
            "code": error.code,
            "url": error.url,
            "retries": retries,
            "timeout_seconds": timeout_seconds,
        }
        super().__init__(ReportableErrorCode.IMDS, supporting_data)


class ReportableErrorImdsConnectionError(ReportableError):
    def __init__(
        self, *, error: UrlError, retries: int, timeout_seconds: float
    ) -> None:
        supporting_data = {
            "reason": "exceeded retry limit due to connection errors",
            "error": str(error),
            "url": error.url,
            "retries": retries,
            "timeout_seconds": timeout_seconds,
        }
        super().__init__(ReportableErrorCode.IMDS, supporting_data)


class ReportableErrorImdsHttpError(ReportableError):
    def __init__(
        self, *, error: UrlError, retries: int, timeout_seconds: float
    ) -> None:
        supporting_data = {
            "reason": "exceeded retry limit due to http status",
            "error": str(error),
            "code": error.code,
            "url": error.url,
            "retries": retries,
            "timeout_seconds": timeout_seconds,
        }
        super().__init__(ReportableErrorCode.IMDS, supporting_data)


class ReportableErrorImdsInvalidMetadata(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.IMDS, supporting_data)


class ReportableErrorMediaInvalidOvfEnvXml(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.MEDIA, supporting_data)


class ReportableErrorMediaNotFound(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.MEDIA, supporting_data)


class ReportableErrorPreprovisioning(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.PPS, supporting_data)


class ReportableErrorWireserverInvalidGoalState(ReportableError):
    def __init__(self, reason: str) -> None:
        supporting_data = {"reason": reason}
        super().__init__(ReportableErrorCode.WIRESERVER, supporting_data)
