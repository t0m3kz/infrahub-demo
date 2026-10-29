"""An MLAG domain has exactly two distinct member devices, a device is in at most one domain, peer_devices names them."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.vlan_domain import MLAG_KIND, mlag_peer_devices

MLAG_MEMBERS = 2


def _members(domain: dict[str, Any]) -> dict[str, str]:
    """Return the distinct member devices of a domain as id -> name."""
    return {str(member.get("id")): str(member.get("name")) for member in domain.get("capabilities") or []}


def device_domain_errors(domains: list[dict[str, Any]]) -> list[str]:
    """M1: a device is a member of at most one MLAG domain (this also makes the peer pair unique)."""
    in_domains: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}
    for domain in domains:
        for device_id, device_name in _members(domain).items():
            in_domains[device_id].append(str(domain.get("name")))
            names[device_id] = device_name
    return [
        f"device '{names[device_id]}' is a member of {len(found)} MLAG domains: {', '.join(sorted(found))} "
        "(rule: a device is in at most one MLAG domain)"
        for device_id, found in sorted(in_domains.items(), key=lambda item: names[item[0]])
        if len(found) > 1
    ]


def member_count_errors(domains: list[dict[str, Any]]) -> list[str]:
    """M2: a domain has exactly two distinct member devices (min_count is only enforced on update)."""
    errors = []
    for domain in sorted(domains, key=lambda item: str(item.get("name"))):
        count = len(_members(domain))
        if count != MLAG_MEMBERS:
            errors.append(
                f"MLAG domain '{domain.get('name')}' has {count} distinct member device(s) "
                f"(rule: a domain has exactly {MLAG_MEMBERS})"
            )
    return errors


def peer_devices_errors(domains: list[dict[str, Any]]) -> list[str]:
    """M3: `peer_devices` equals the sorted member names (catches a device rename or a hand edit)."""
    errors = []
    for domain in sorted(domains, key=lambda item: str(item.get("name"))):
        expected = mlag_peer_devices(_members(domain).values())
        stored = domain.get("peer_devices")
        if stored != expected:
            errors.append(
                f"MLAG domain '{domain.get('name')}' has peer_devices '{stored}', expected '{expected}' "
                "(rule: peer_devices is the sorted member device names joined with a comma). "
                "Re-run the pod or DC generator that created the pair (generators/domain_pairing.py) "
                "so peer_devices is rewritten"
            )
    return errors


class CheckMlagDomains(InfrahubCheck):
    """MLAG domains are well formed (ERRORs).

    A uniqueness constraint cannot use a many relationship and the server enforces `min_count` only on update:

    - a device is a member of at most one ManagedMLAG (which makes the peer pair unique across domains);
    - a domain has exactly two distinct member devices;
    - `peer_devices` (the unique mirror of the pair) equals the sorted member names.
    """

    query = "mlag_domains"

    def validate(self, data: Any) -> None:
        domains = clean_data(data).get(MLAG_KIND) or []
        for message in device_domain_errors(domains) + member_count_errors(domains) + peer_devices_errors(domains):
            self.log_error(message=message)
