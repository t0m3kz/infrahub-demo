"""What the platform device views share: refusals, CLI names, content digests and the device scope."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from transforms.helpers.group_policy.model import FALLBACK, UNAWARE, UNCLASSIFIED, DataCenterContracts, Group

_UNVERIFIED = "documented-not-lab-verified"
_RESERVED_LABELS = {UNCLASSIFIED: "UNCLASSIFIED", UNAWARE: "UNAWARE"}
_RESERVED_NAMES = frozenset({UNCLASSIFIED, UNAWARE, FALLBACK})


def _refuse(who: str, why: str) -> ValueError:
    return ValueError(f"{who}: {why}; refusing unprotected config")


def _digest(payload: Any) -> str:
    """The first 10 hex of the sha256 of the canonical JSON of ``payload`` (never hash()): the content name of an object."""
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:10]


def _cli_name(name: str) -> str:
    """A SecurityTag name as a CLI-safe group name: ``[A-Za-z0-9_-]`` (the guide's examples; its rules are U)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _label(name: str) -> str:
    return _RESERVED_LABELS.get(name) or _cli_name(name)


def _refuse_cli_clash(who: str, names: Iterable[str], noun: str, called: str) -> None:
    """Two names that become one CLI name (``_cli_name``) would collapse into one object on the device."""
    safe: dict[str, str] = {}
    for name in sorted(names):
        if safe.setdefault(_cli_name(name), name) != name:
            raise _refuse(who, f"{noun} {safe[_cli_name(name)]!r} and {name!r} share the {called} {_cli_name(name)!r}")


def _refuse_ambiguous_groups(who: str, groups: Iterable[Group]) -> None:
    """A tag shared by two segments or named like a pseudo-group would collapse into one entry of the views."""
    first: dict[str, Group] = {}
    for group in groups:
        if group.name.lower() in _RESERVED_NAMES:
            raise _refuse(who, f"group {group.name!r} is named like the reserved pseudo-group {group.name.upper()}")
        other = first.setdefault(group.name, group)
        if (other.segment, other.vrf) != (group.segment, group.vrf):
            raise _refuse(
                who,
                f"group {group.name!r} is the tag of segment {other.segment} (VRF {other.vrf}) "
                f"and of segment {group.segment} (VRF {group.vrf})",
            )


def _one_line(text: str) -> str:
    """A name-bearing text as one comment line: whitespace (a newline in a name) collapses to single spaces."""
    return " ".join(text.split())


def _staged_reason(contracts: DataCenterContracts, dc_id: str, consequence: str) -> str | None:
    """Why a data center configures but does not enforce (``group_policy_enforce`` false), None when it enforces."""
    if contracts.enforce:
        return None
    return f"data center {contracts.name or dc_id} stages group policy (group_policy_enforce is false): {consequence}"


@dataclass(frozen=True)
class _DeviceScope:
    """What a device builder reads: the device, its data center's contracts and what the device carries."""

    who: str
    kind: str
    dc_id: str
    fleet: dict[str, list[dict[str, Any]]]
    contracts: DataCenterContracts
    device_name: str
    activations: list[dict[str, Any]]
    vrfs: frozenset[str]
    interfaces: list[dict[str, Any]]
