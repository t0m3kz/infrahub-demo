"""Which of a customer footprint's firewall contexts holds which of its rules (pure, no I/O).

A footprint lists every firewall context that enforces its policy (``serving_firewall_contexts``, the reverse of the
context's ``served_deployments``): its internal one(s) and, when it has an Internet edge, the edge context. The role
of a context is the role of its member firewalls (``shared_devices``, utils/fabric_roles.py). The split is decided PER
FOOTPRINT, never per context (one shared internal context serves footprints with and without an edge):

| The footprint lists   | its rules on an internal context | its rules on an external context |
| --------------------- | -------------------------------- | -------------------------------- |
| internal + external   | all except the internet rules    | only the internet rules          |
| internal only         | all                              | -                                |
| external only         | -                                | all                              |

An internet rule is one that crosses the internet edge in either direction (transforms/helpers/policy.py
``is_internet_rule``). The transform (transforms/helpers/firewall_contexts.py), the internet egress check
(checks/internet_egress.py) and the leaf PBR (transforms/helpers/firewall_contexts.py
``_flatten_deployment_firewall_contexts``) read the split from here.

The input is cleaned query data: a context with ``shared_devices`` (each a device with its ``role``), a footprint with
``serving_firewall_contexts`` (such contexts).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Any

from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE


class RuleShare(Enum):
    """The rules of one footprint a context of one role holds."""

    ALL = "all"
    INTERNET = "internet"
    NON_INTERNET = "non_internet"

    def holds(self, internet_rule: bool) -> bool:
        """Whether a rule (``internet_rule``: it crosses the internet edge) is in this share."""
        if self is RuleShare.ALL:
            return True
        return internet_rule == (self is RuleShare.INTERNET)


def is_external(context: dict[str, Any]) -> bool:
    """Whether the context stands on an Internet edge pair: a member firewall has the ``external-firewall`` role."""
    return any(
        isinstance(device, dict) and device.get("role") == EXTERNAL_FIREWALL_ROLE
        for device in context.get("shared_devices") or []
    )


def footprint_contexts(footprint: dict[str, Any]) -> list[dict[str, Any]]:
    """The firewall contexts a footprint lists (``serving_firewall_contexts``)."""
    return [context for context in footprint.get("serving_firewall_contexts") or [] if isinstance(context, dict)]


def rule_share(external: bool, contexts: Iterable[dict[str, Any]]) -> RuleShare:
    """The share of a footprint's rules a context holds: ``external`` is the context's role, ``contexts`` the
    footprint's list (the table in the module docstring)."""
    roles = {is_external(context) for context in contexts}
    if external and False in roles:
        return RuleShare.INTERNET
    if not external and True in roles:
        return RuleShare.NON_INTERNET
    return RuleShare.ALL


def internet_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The contexts of a footprint's list that hold its internet rules: the external ones, else every context."""
    external = [context for context in contexts if is_external(context)]
    return external or contexts


def steering_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The contexts a footprint's segments are steered to (PBR): the internal ones, else the external ones (then the
    footprint's only firewall)."""
    internal = [context for context in contexts if not is_external(context)]
    return internal or contexts
