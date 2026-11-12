"""Which of a customer footprint's firewall contexts holds which of its rules (pure, no I/O).

Decided per footprint: internal + external, the external holds the internet rules and the internal the rest; one kind
holds all.
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
    """The share of a footprint's rules a context holds (``external``: its role; ``contexts``: the footprint's list)."""
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
    """The contexts a footprint's segments are steered to (PBR): the internal ones, else the external ones."""
    internal = [context for context in contexts if not is_external(context)]
    return internal or contexts
