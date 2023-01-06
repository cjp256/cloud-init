# Copyright (C) 2022 Microsoft Corporation.
#
# This file is part of cloud-init. See LICENSE file for license information.

import logging
from typing import Dict, Optional

LOG = logging.getLogger(__name__)


class Nic:
    def __init__(self, name: str, mac: str, driver: Optional[str]) -> None:
        self.name = name
        self.mac = mac
        self.driver = driver

    def as_dict(self) -> Dict[str, str]:
        obj = {
            "name": self.name,
            "mac": self.mac,
        }

        if self.driver:
            obj["driver"] = self.driver

        return obj
