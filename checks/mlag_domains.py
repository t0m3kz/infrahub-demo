"""An MLAG domain has two distinct members, a device is in at most one domain, peer_devices names them.

The two members also hold equal VLAN ids per segment deployment, and each MLAG-attached port-channel is mirrored.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.vlan_domain import MLAG_KIND, mlag_peer_devices

MLAG_MEMBERS = 2
ROW_KIND = "ManagedVlanDomainSegment"
LAG_KIND = "DcimLAGInterface"
PEER_LINK_ROLE = "mlag-peer"


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


def _row_label(row: dict[str, Any]) -> str:
    """The segment (or segment deployment id) a row realizes."""
    deployment = row.get("segment_deployment") or {}
    return str((deployment.get("segment") or {}).get("name") or deployment.get("id"))


def pair_row_errors(domains: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[str]:
    """M4: per MLAG and segment deployment, both members have a row and the two rows have the same vlan_id.

    A deployment on which neither member has a row is fine.
    """
    by_device: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        deployment_id = (row.get("segment_deployment") or {}).get("id")
        device_id = (row.get("device") or {}).get("id")
        if deployment_id and device_id:
            by_device[str(device_id)][str(deployment_id)] = row
    errors = []
    for domain in sorted(domains, key=lambda item: str(item.get("name"))):
        members = _members(domain)
        if len(members) != MLAG_MEMBERS:
            continue  # M2 reports it
        (first_id, first_name), (second_id, second_name) = sorted(members.items(), key=lambda item: item[1])
        first_rows, second_rows = by_device.get(first_id, {}), by_device.get(second_id, {})
        for deployment_id in sorted(first_rows.keys() | second_rows.keys()):
            first, second = first_rows.get(deployment_id), second_rows.get(deployment_id)
            if first and second:
                if first.get("vlan_id") != second.get("vlan_id"):
                    errors.append(
                        f"MLAG domain '{domain.get('name')}': segment '{_row_label(first)}' has VLAN "
                        f"{first.get('vlan_id')} on '{first_name}' and VLAN {second.get('vlan_id')} on "
                        f"'{second_name}' (rule: the two members of an MLAG hold the same VLAN id). "
                        "Renumber one row, or delete one and re-run add_vxlan_segment for the segment"
                    )
                continue
            have, missing_name = (first, second_name) if first else (second, first_name)
            if have:
                errors.append(
                    f"MLAG domain '{domain.get('name')}': segment '{_row_label(have)}' has VLAN {have.get('vlan_id')} "
                    f"on '{(have.get('device') or {}).get('name')}' but '{missing_name}' has no row for it "
                    "(rule: both members of an MLAG hold a row per segment). Re-run add_vxlan_segment for the segment"
                )
    return errors


def mlag_lag_errors(domains: list[dict[str, Any]], lags: list[dict[str, Any]]) -> list[str]:
    """M5: per MLAG and lag_id, exactly two port-channels, one per member, with equal lacp_mode and minimum_links.

    The peer-link port-channel and LAGs without an ``mlag_domain`` are skipped.
    """
    grouped: dict[tuple[str, Any], list[dict[str, Any]]] = defaultdict(list)
    for lag in lags:
        mlag_id = (lag.get("mlag_domain") or {}).get("id")
        if mlag_id and lag.get("role") != PEER_LINK_ROLE:
            grouped[(str(mlag_id), lag.get("lag_id"))].append(lag)
    errors = []
    for domain in sorted(domains, key=lambda item: str(item.get("name"))):
        members = _members(domain)
        for (mlag_id, lag_id), found in sorted(grouped.items(), key=lambda item: str(item[0][1])):
            if mlag_id != str(domain.get("id")):
                continue
            where = f"MLAG domain '{domain.get('name')}' lag_id {lag_id}"
            owners = sorted(str((lag.get("device") or {}).get("name")) for lag in found)
            device_ids = {str((lag.get("device") or {}).get("id")) for lag in found}
            if len(found) != MLAG_MEMBERS or len(device_ids) != MLAG_MEMBERS or not device_ids <= members.keys():
                errors.append(
                    f"{where}: {len(found)} port-channel(s) on {', '.join(owners)} "
                    f"(rule: exactly one per member, {', '.join(sorted(members.values()))})"
                )
                continue
            for attribute in ("lacp_mode", "minimum_links"):
                values = {str((lag.get("device") or {}).get("name")): lag.get(attribute) for lag in found}
                if len(set(values.values())) > 1:
                    detail = ", ".join(f"{name}={value}" for name, value in sorted(values.items()))
                    errors.append(f"{where}: {attribute} differs ({detail}) (rule: the two port-channels are equal)")
    return errors


class CheckMlagDomains(InfrahubCheck):
    """MLAG domains are well formed (ERRORs).

    A uniqueness constraint cannot use a many relationship and the server enforces `min_count` only on update:

    - a device is a member of at most one ManagedMLAG (which makes the peer pair unique across domains);
    - a domain has exactly two distinct member devices;
    - `peer_devices` (the unique mirror of the pair) equals the sorted member names;
    - M4: per segment deployment, both members have a row and the two rows have the same VLAN id (the late pairing
      of two devices that already numbered a segment differently is reported here, never renumbered);
    - M5: each MLAG-attached port-channel (`lag_id`) exists exactly once per member with equal `lacp_mode` and
      `minimum_links` (the peer-link port-channel is skipped).
    """

    query = "mlag_domains"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        domains = cleaned.get(MLAG_KIND) or []
        rows = cleaned.get(ROW_KIND) or []
        lags = cleaned.get(LAG_KIND) or []
        for message in (
            device_domain_errors(domains)
            + member_count_errors(domains)
            + peer_devices_errors(domains)
            + pair_row_errors(domains, rows)
            + mlag_lag_errors(domains, lags)
        ):
            self.log_error(message=message)
