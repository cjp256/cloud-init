# Copyright (C) 2022 Microsoft Corporation.
#
# This file is part of cloud-init. See LICENSE file for license information.

import functools
from typing import Dict

import requests

from cloudinit import log as logging
from cloudinit import util
from cloudinit.sources.helpers.azure import report_diagnostic_event
from cloudinit.url_helper import UrlError, readurl, retry_on_url_exc

LOG = logging.getLogger(__name__)

IMDS_URL = "http://169.254.169.254/metadata"


def _fetch_url(
    url: str,
    *,
    log_response: bool = True,
    retries: int = 10,
    retry_codes=(
        404,  # not found (yet)
        410,  # gone / unavailable (yet)
        429,  # rate-limited/throttled
        500,  # server error
    ),
    retry_instances=(
        requests.ConnectionError,
        requests.Timeout,
    ),
    timeout: int = 2
) -> bytes:
    """Fetch URL from IMDS.

    :raises UrlError: on error fetching metadata.
    """
    imds_readurl_exception_callback = functools.partial(
        retry_on_url_exc,
        retry_codes=retry_codes,
        retry_instances=retry_instances,
    )

    try:
        response = readurl(
            url,
            log_req_resp=log_response,
            timeout=timeout,
            headers={"Metadata": "true"},
            retries=retries,
            exception_cb=imds_readurl_exception_callback,
            infinite=False,
        )
    except UrlError as error:
        report_diagnostic_event(
            "Failed to fetch metadata from IMDS: %s" % error,
            logger_func=LOG.warning,
        )
        raise

    return response.contents


def _fetch_metadata(
    url: str,
) -> Dict:
    """Fetch IMDS metadata.

    :raises UrlError: on error fetching metadata.
    :raises ValueError: on error parsing metadata.
    """
    metadata = _fetch_url(url)

    try:
        return util.load_json(metadata)
    except ValueError as error:
        report_diagnostic_event(
            "Failed to parse metadata from IMDS: %s" % error,
            logger_func=LOG.warning,
        )
        raise


def fetch_metadata_with_api_fallback() -> Dict:
    """Fetch extended metadata, falling back to non-extended as required.

    :raises UrlError: on error fetching metadata.
    :raises ValueError: on error parsing metadata.
    """
    try:
        url = IMDS_URL + "/instance?api-version=2021-03-01&extended=true"
        return _fetch_metadata(url)
    except UrlError as error:
        if error.code == 400:
            report_diagnostic_event(
                "Falling back to IMDS api-version: 2019-06-01",
                logger_func=LOG.warning,
            )
            url = IMDS_URL + "/instance?api-version=2019-06-01"
            return _fetch_metadata(url)
        raise


def fetch_reprovisiondata() -> bytes:
    """Fetch extended metadata, falling back to non-extended as required.

    :raises UrlError: on error.
    """
    url = IMDS_URL + "/reprovisiondata?api-version=2019-06-01"
    return _fetch_url(url, log_response=False)
