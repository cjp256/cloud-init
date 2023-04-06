# This file is part of cloud-init. See LICENSE file for license information.

import base64
import datetime

import pytest

from cloudinit import version
from cloudinit.sources.azure import errors


@pytest.fixture()
def agent_string():
    yield f"agent=Cloud-Init/{version.version_string()}"


def quote_csv_value(value: str) -> str:
    """Match quoting behavior, if needed for given string."""
    if any([x in value for x in ("\n", "\r", "'")]):
        value = value.replace("'", "''")
        value = f"'{value}'"

    return value


@pytest.mark.parametrize("reason", ["foo", "foo bar", "foo'bar"])
@pytest.mark.parametrize(
    "supporting_data",
    [
        {},
        {
            "foo": "bar",
        },
        {
            "foo": "bar",
            "count": 4,
        },
        {
            "csvcheck": "",
        },
        {
            "csvcheck": "trailingspace ",
        },
        {
            "csvcheck": "\r\n",
        },
        {
            "csvcheck": "\n",
        },
        {
            "csvcheck": "\t",
        },
        {
            "csvcheck": "x\nx",
        },
        {
            "csvcheck": "x\rx",
        },
        {
            "csvcheck": '"',
        },
        {
            "csvcheck": '""',
        },
        {
            "csvcheck": "'",
        },
        {
            "csvcheck": "''",
        },
        {
            "csvcheck": "xx'xx'xx",
        },
        {
            "csvcheck": ",'|~!@#$%^&*()[]\\{}|;':\",./<>?x\nnew\r\nline",
        },
    ],
)
def test_reportable_errors(
    reason,
    supporting_data,
):
    timestamp = datetime.datetime.utcnow()

    error = errors.ReportableError(
        reason=reason,
        supporting_data=supporting_data,
        timestamp=timestamp,
    )

    data = [
        "PROVISIONING_ERROR: " + quote_csv_value(f"reason={reason}"),
        f"agent=Cloud-Init/{version.version_string()}",
    ]
    data += [quote_csv_value(f"{k}={v}") for k, v in supporting_data.items()]
    data += [
        f"timestamp={timestamp.isoformat()}",
        "documentation_url=https://aka.ms/linuxprovisioningerror",
    ]

    assert error.as_description() == "|".join(data)


def test_unhandled_exception():
    source_error = None
    try:
        raise ValueError("my value error")
    except Exception as exception:
        source_error = exception

    error = errors.ReportableErrorUnhandledException(source_error)
    trace = base64.b64decode(error.supporting_data["traceback_base64"]).decode(
        "utf-8"
    )

    quoted_value = quote_csv_value(f"exception={source_error!r}")
    assert f"|{quoted_value}|" in error.as_description()
    assert trace.startswith("Traceback")
    assert "raise ValueError" in trace
    assert trace.endswith("ValueError: my value error\n")
