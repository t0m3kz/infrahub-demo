"""Report what runs a software image that is not approved (SL1): vulnerable or end_of_life is an ERROR, deprecated a
WARNING.
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from utils.data_cleaning import clean_data

ERROR_LIFECYCLE = "end_of_life"
WARNING_LIFECYCLE = "deprecated"


def image_problems(image: dict[str, Any]) -> list[str]:
    """What is wrong with an image, in words; empty when approved and not vulnerable."""
    problems = []
    if image.get("vulnerable"):
        reason = image.get("vulnerability_reason")
        problems.append(f"vulnerable ({reason})" if reason else "vulnerable")
    lifecycle = image.get("lifecycle")
    if lifecycle in (ERROR_LIFECYCLE, WARNING_LIFECYCLE):
        problems.append(str(lifecycle).replace("_", " "))
    return problems


def lifecycle_findings(image: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(severity, message, runner) for each runner of an image."""
    problems = image_problems(image)
    if not problems:
        return []
    severity = "warning" if image.get("lifecycle") == WARNING_LIFECYCLE and not image.get("vulnerable") else "error"
    platform = (image.get("platform") or {}).get("name") or "<no platform>"
    label = f"{image.get('name')} ({platform} {image.get('version')})"
    return [
        (
            severity,
            f"{runner.get('display_label') or runner.get('id')} ({runner.get('typename')}) runs {label}, "
            f"which is {' and '.join(problems)}.",
            runner,
        )
        for runner in image.get("runners") or []
        if isinstance(runner, dict)
    ]


class CheckSoftwareLifecycle(WarningCheck):
    """Every runner of a vulnerable, end-of-life or deprecated image is reported (rule SL1)."""

    query = "software_lifecycle"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        images = {
            image["id"]: image for key in ("by_lifecycle", "by_vulnerability") for image in cleaned.get(key) or []
        }
        for image in images.values():
            for severity, message, runner in lifecycle_findings(image):
                if severity == "error":
                    self.log_error(message=message, object_id=runner.get("id"), object_type=runner.get("typename"))
                else:
                    self.log_warning(message=message)
