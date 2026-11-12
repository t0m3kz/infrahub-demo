"""What a Proposed Change touches, in operator terms: the devices, the links, the firewall rules and the network
segments, read from the data diff and the rows of change_request_facts.gql. Pure: no I/O.

``build_facts`` is the one reader; utils/change_text.py caps the lists and renders them through templates/change/. A
removed object is gone from the branch, so it is described from its diff label only. A row the diff does not list is
not described (the ids asked for come from the diff).
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from utils.change_rules import (
    OWNER_DIFF_KINDS,
    STATUS_ORDER,
    UPDATED,
    ArtifactChange,
    ObjectDiff,
    device_facts,
)

CABLE_KIND = "DcimCable"
CARRIER_KIND = "TopologyCarrierCircuit"
SESSION_KIND = "TopologySessionCircuit"
LINK_FAMILIES = {CABLE_KIND: "cable", CARRIER_KIND: "carrier circuit", SESSION_KIND: "session circuit"}
RULE_KIND = "SecurityPolicyRule"
SEGMENT_KINDS = frozenset({"ManagedVxlanSegment", "CloudNetworkSegment"})
FAMILY_KINDS = LINK_FAMILIES.keys() | {RULE_KIND} | SEGMENT_KINDS
"""The kinds the description names one by one."""
OBJECT_DIFF_KINDS = FAMILY_KINDS | OWNER_DIFF_KINDS
"""The kinds of the data diff read with their values (utils/change_sources.py): the families, and the footprints and
applications whose owner is an affected customer. Every other changed kind is only counted."""

NO_SITE = "no site"
ANY = "any"
POLICY_SEGMENT = "the policy's segment"
TRUE = "True"

NEW = "new"
CONFIG_UPDATED = "updated"
CONFIG_REMOVED = "removed"


@dataclass(frozen=True)
class DeviceChange:
    """One affected device: its place and what happens to its configuration."""

    name: str
    role: str | None
    site: str
    action: str
    pair: str | None = None
    artifacts: tuple[ArtifactChange, ...] = ()


@dataclass(frozen=True)
class Link:
    """A cable, a carrier circuit or a session circuit."""

    status: str
    family: str
    name: str
    link_type: str = ""
    provider: str = ""
    ends: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    cable_id: str = ""
    vrf: str = ""
    changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    """A firewall rule."""

    status: str
    name: str
    policy: str = ""
    index: int | None = None
    source: str = ""
    destination: str = ""
    service: str = ""
    action: str = ""
    enabled: bool = True
    changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Segment:
    """A network segment."""

    status: str
    name: str
    owner: str = ""
    environment: str = ""
    ip_version: str = ""
    prefix: str = ""
    gateway: str = ""
    vnis: tuple[int, ...] = ()
    vlan_scope: str = ""
    stretch_scope: str = ""
    firewall_gateway: bool = False
    footprints: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeFacts:
    """The four families, each in display order (by status, then name)."""

    devices: tuple[DeviceChange, ...] = ()
    links: tuple[Link, ...] = ()
    rules: tuple[Rule, ...] = ()
    segments: tuple[Segment, ...] = ()


# --- readers of cleaned rows ----------------------------------------------------------------------------------------


def _name(value: Any) -> str:
    """The name of a cleaned relationship value (a node dict, or a bare string)."""
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("display_label") or "")
    return str(value) if value else ""


def _names(values: Any, key: str = "name") -> list[str]:
    return sorted(str(v.get(key)) for v in values or [] if isinstance(v, Mapping) and v.get(key))


def _order(status: str) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)


def _end(node: Any) -> str:
    """``device:interface`` of an interface; the label of any other endpoint (a patch panel port)."""
    if not isinstance(node, Mapping):
        return ""
    device = _name(node.get("device"))
    port = str(node.get("name") or "")
    if device and port:
        return f"{device}:{port}"
    return port or str(node.get("display_label") or "")


def _config_action(artifacts: Sequence[ArtifactChange]) -> str:
    actions = {a.action for a in artifacts}
    if actions == {"removed"}:
        return CONFIG_REMOVED
    if actions == {"added"}:
        return NEW
    return CONFIG_UPDATED


# --- families -------------------------------------------------------------------------------------------------------


def _devices(rows: Iterable[Mapping[str, Any]], artifacts: Sequence[ArtifactChange]) -> tuple[DeviceChange, ...]:
    """Every artifact target, with its place from the branch when it still exists (a removed device is not there)."""
    by_name = {str(row.get("name")): row for row in rows if isinstance(row, Mapping)}
    configs: dict[str, list[ArtifactChange]] = defaultdict(list)
    for artifact in artifacts:
        configs[artifact.target].append(artifact)
    found = []
    for name, changes in configs.items():
        row = by_name.get(name) or {}
        found.append(
            DeviceChange(
                name=name,
                role=row.get("role"),
                site=_name(row.get("site") or row.get("deployment")) or NO_SITE,
                action=_config_action(changes),
                pair=device_facts(row).peer if row else None,
                artifacts=tuple(changes),
            )
        )
    return tuple(sorted(found, key=lambda d: (d.site, d.role or "", d.name)))


NOISE_ATTRIBUTES = frozenset({"display_label", "human_friendly_id"})


def _changed(o: ObjectDiff) -> tuple[str, ...]:
    """What an update changed: ``attribute previous -> new`` and the peers it dropped (nothing for the other statuses)."""
    if o.status != UPDATED:
        return ()
    values = [f"{n} {p or '-'} -> {v or '-'}" for n, (p, v) in sorted(o.changes.items()) if n not in NOISE_ATTRIBUTES]
    return tuple(values + [f"no longer {n} {peer}" for n, peers in sorted(o.dropped.items()) for peer in peers])


def _first(object_diff: ObjectDiff, relationship: str) -> str:
    peers = object_diff.peers.get(relationship) or ()
    return peers[0] if peers else ""


def _address(label: str) -> str:
    """The address or prefix of an IPAM label (``10.0.0.1/24 - default``: the namespace follows)."""
    return label.split(" - ", 1)[0]


def _link(o: ObjectDiff) -> Link:
    family = LINK_FAMILIES[o.kind]
    if o.kind == CABLE_KIND:
        return Link(
            status=o.status,
            family=family,
            name=o.label,
            link_type=o.values.get("type") or "",
            ends=o.peers.get("endpoints") or (),
            places=o.peers.get("deployment") or (),
            cable_id=o.values.get("cable_id") or "",
            changed=_changed(o),
        )
    return Link(
        status=o.status,
        family=family,
        name=o.values.get("circuit_id") or o.label,
        link_type=o.values.get("circuit_type") or "",
        provider=_first(o, "provider"),
        ends=o.peers.get("capabilities") or (),
        places=o.peers.get("locations") or (),
        vrf=_first(o, "vrf"),
        changed=_changed(o),
    )


def _selector(o: ObjectDiff, side: str) -> str:
    parts = [f"segment {s}" for s in o.peers.get(f"{side}_segment") or ()]
    parts += [f"zone {z}" for z in o.peers.get(f"{side}_zone") or ()]
    parts += [f"prefix {_address(p)}" for p in o.peers.get(f"{side}_prefixes") or ()]
    parts += [f"address {_address(a)}" for a in o.peers.get(f"{side}_ip_addresses") or ()]
    parts += [f"tag {t}" for t in o.peers.get(f"{side}_security_tag") or ()]
    return ", ".join(parts)


def _service(o: ObjectDiff) -> str:
    protocol = o.values.get("protocol") or ANY
    start, end = o.values.get("port_start"), o.values.get("port_end")
    if not start:
        return protocol
    return f"{protocol}/{start}-{end}" if end and end != start else f"{protocol}/{start}"


def _rule(o: ObjectDiff) -> Rule:
    index = o.values.get("index")
    return Rule(
        status=o.status,
        name=o.values.get("name") or o.label,
        policy=_first(o, "policy"),
        index=int(index) if index and index.isdigit() else None,
        source=_selector(o, "source") or (POLICY_SEGMENT if o.peers.get("policy") else ANY),
        destination=_selector(o, "destination") or ANY,
        service=_service(o),
        action=o.values.get("action") or "",
        enabled=o.values.get("disabled") != TRUE,
        changed=_changed(o),
    )


def _prefix(gateway: str) -> str:
    try:
        return str(ipaddress.ip_interface(_address(gateway)).network) if gateway else ""
    except ValueError:
        return ""


def _segment(o: ObjectDiff) -> Segment:
    gateway = _first(o, "gateway")
    return Segment(
        status=o.status,
        name=o.values.get("name") or o.label,
        owner=_first(o, "owner"),
        environment=o.values.get("environment") or "",
        ip_version=o.values.get("ip_version") or "",
        prefix=_prefix(gateway),
        gateway=_address(gateway).split("/")[0],
        vlan_scope=o.values.get("vlan_scope") or "",
        stretch_scope=o.values.get("stretch_scope") or "",
        firewall_gateway=o.values.get("firewall_gateway") == TRUE,
        footprints=o.peers.get("customer_deployments") or (),
        changed=_changed(o),
    )


def build_facts(
    rows: Mapping[str, Any], artifacts: Sequence[ArtifactChange], objects: Sequence[ObjectDiff]
) -> ChangeFacts:
    """The facts of the change: ``rows`` are the cleaned roots of change_request_facts.gql (the place of the devices,
    may be empty), ``objects`` the data diff read with its values."""
    ordered = sorted(objects, key=lambda o: (_order(o.status), o.kind, o.label))
    rules = (_rule(o) for o in ordered if o.kind == RULE_KIND)
    return ChangeFacts(
        devices=_devices(rows.get("DcimDeviceManaged") or [], artifacts),
        links=tuple(_link(o) for o in ordered if o.kind in LINK_FAMILIES),
        rules=tuple(sorted(rules, key=lambda r: (_order(r.status), r.policy, r.index or 0, r.name))),
        segments=tuple(_segment(o) for o in ordered if o.kind in SEGMENT_KINDS),
    )
