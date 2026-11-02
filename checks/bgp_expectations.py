"""The BGP route protection is closed: no filter derived from the model drops what the model says must flow."""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from checks.bgp_closure import analyse
from utils.data_cleaning import clean_data


class CheckBgpExpectations(WarningCheck):
    """Compare the two ends of every BGP session with the filters the transforms derive (rules E1 to E5).

    Targetless: a session has two devices and the facts are fleet-wide. A finding is an ERROR where the hosting
    deployment enforces (``routing_filter_enforce``), a WARNING where it stages: turn the switch on only when a
    deployment shows none. The rules are in ``checks/bgp_closure.py``.
    """

    query = "bgp_expectations"

    def validate(self, data: Any) -> None:
        findings = analyse(clean_data(data))
        for finding in findings:
            message = f"{finding.rule}: {finding.message}"
            if finding.severity == "error":
                self.log_error(message=message)
            else:
                self.log_warning(message=message)
        self.log_info(message=f"{len(findings)} finding(s) over the BGP sessions")
