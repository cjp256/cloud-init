# Copyright (C) 2022 Microsoft Corporation.
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
import csv
import logging
import traceback
from datetime import datetime
from io import StringIO
from typing import Any, Dict, Optional

import requests

from cloudinit import version
from cloudinit.url_helper import UrlError

LOG = logging.getLogger(__name__)


class ReportableError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        supporting_data: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        self.documentation_url = "https://aka.ms/linuxprovisioningerror"
        self.reason = reason

        if supporting_data:
            self.supporting_data = supporting_data
        else:
            self.supporting_data = {}

        if timestamp:
            self.timestamp = timestamp
        else:
            self.timestamp = datetime.utcnow()

    def as_description(
        self, *, delimiter: str = "|", quotechar: str = "'"
    ) -> str:
        data = [
            f"reason={self.reason}",
            f"agent=Cloud-Init/{version.version_string()}",
        ]
        data += [f"{k}={v}" for k, v in self.supporting_data.items()]
        data += [
            f"timestamp={self.timestamp.isoformat()}",
            f"documentation_url={self.documentation_url}",
        ]

        with StringIO() as io:
            csv.writer(
                io,
                delimiter=delimiter,
                quotechar=quotechar,
                quoting=csv.QUOTE_MINIMAL,
            ).writerow(data)

            # strip trailing \r\n
            csv_data = io.getvalue()[:-2]

        return f"PROVISIONING_ERROR: {csv_data}"

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, ReportableError)
            and self.timestamp == other.timestamp
            and self.reason == other.reason
            and self.supporting_data == other.supporting_data
        )

    def __repr__(self) -> str:
        return self.as_description()


class ReportableErrorUnhandledException(ReportableError):
    def __init__(self, exception: Exception) -> None:
        super().__init__("unhandled exception")

        trace = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        trace_base64 = base64.b64encode(trace.encode("utf-8"))

        self.supporting_data["exception"] = repr(exception)
        self.supporting_data["traceback_base64"] = trace_base64


class ReportableErrorDhcpLease(ReportableError):
    def __init__(self) -> None:
        super().__init__("failure to obtain DHCP lease")


class ReportableErrorImageMissingDhclient(ReportableError):
    def __init__(self) -> None:
        super().__init__("image missing dhclient executable")


class ReportableErrorImdsUrlError(ReportableError):
    def __init__(self, *, exception: UrlError, retries: int) -> None:
        # ConnectTimeout sub-classes ConnectError so order is important.
        if isinstance(exception.cause, requests.ConnectTimeout):
            reason = "connection timeout querying IMDS"
        elif isinstance(exception.cause, requests.ConnectionError):
            reason = "connection error querying IMDS"
        elif isinstance(exception.cause, requests.ReadTimeout):
            reason = "read timeout querying IMDS"
        elif exception.code:
            reason = "http error querying IMDS"
        else:
            reason = "unexpected error querying IMDS"

        super().__init__(reason)

        if exception.code:
            self.supporting_data["http_code"] = exception.code

        self.supporting_data["exception"] = str(exception)
        self.supporting_data["url"] = exception.url
        self.supporting_data["retries"] = retries


class ReportableErrorImdsMetadataParsingException(ReportableError):
    def __init__(self, exception: ValueError) -> None:
        super().__init__("error parsing IMDS metadata")

        self.supporting_data["exception"] = str(exception)
