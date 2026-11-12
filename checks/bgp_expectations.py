"""The BGP route protection is closed: no filter derived from the model drops what the model says must flow."""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from checks.bgp_closure import analyse
from utils.data_cleaning import clean_data


class CheckBgpExpectations(WarningCheck):
    """Compare both ends of every BGP session with the derived filters (E1-E5, see checks/bgp_closure.py)."""

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
