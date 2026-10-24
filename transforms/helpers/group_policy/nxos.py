"""The NX-OS VXLAN GPO view and the device builder of its VTEP configs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any

from netutils.interface import sort_interface_list

from transforms.helpers.group_policy.common import (
    _UNVERIFIED,
    _DeviceScope,
    _digest,
    _label,
    _one_line,
    _refuse,
    _refuse_ambiguous_groups,
    _refuse_cli_clash,
    _staged_reason,
)
from transforms.helpers.group_policy.model import (
    DEFAULT_ACTION,
    REPLIES,
    UNAWARE,
    UNCLASSIFIED,
    DataCenterContracts,
    Entry,
    Group,
    _entries,
    _entry_set,
    segment_vrf,
)
from transforms.helpers.segments import is_inline_terminated
from utils.app_instances import HOSTED_INSTANCE_TYPENAMES, physical_host
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import reserved_groups


def vxlan_gpo_view(
    groups: Iterable[Group],
    rows: Iterable[dict[str, Any]],
    *,
    reserved: dict[str, int | None],
    reference_sites: set[str],
    local: set[str] | None = None,
) -> dict[str, Any]:
    """The contract set by group ID, with the reserved groups of the profile (module docstring).

    ``groups`` are every group the rows name; ``reserved`` the profile's
    ``unclassified`` / ``unaware`` group IDs (None: the row cannot be rendered
    until the profile names one); ``reference_sites`` the site ids whose
    VTEPs are policy-aware; ``local`` the groups whose stretched halves get
    their intra-group row (None: all). A group's own ID is on the wire where
    it is active on a reference site; where it is also (or only) active
    elsewhere, it arrives as the unaware group. A row whose ends would both be
    reserved is dropped.
    """
    by_name = {group.name: group for group in groups}
    unaware = (UNAWARE, reserved.get("unaware"), True)

    def _ends(name: str) -> list[tuple[str, int | None, bool]]:
        """(name, group id, reserved) of every group a row end arrives as."""
        if name == UNCLASSIFIED:
            return [(UNCLASSIFIED, reserved.get("unclassified"), True)]
        group = by_name[name]
        aware, unaware_too = bool(group.sites & reference_sites), bool(group.sites - reference_sites)
        return [
            *([(name, group.group_id, False)] if aware or not group.sites else []),
            *([unaware] if unaware_too else []),
        ]

    merged: dict[tuple[str, str, str], tuple[int | None, int | None, set[Entry], set[str]]] = {}

    def _add(
        vrf: str | None, src: tuple[str, int | None, bool], dst: tuple[str, int | None, bool], row: dict[str, Any]
    ) -> None:
        if src[2] and dst[2]:
            return
        _src_id, _dst_id, entries, rules = merged.setdefault(
            (vrf or "", src[0], dst[0]), (src[1], dst[1], set(), set())
        )
        entries.update(_entry_set(row["entries"]))
        rules.update(row["rules"])

    for row in rows:
        for src, dst in product(_ends(row["src"]), _ends(row["dst"])):
            _add(row["vrf"], src, dst, row)
    permit_all = {"entries": [{"protocol": "any", "port_start": None, "port_end": None}], "rules": []}
    for group in by_name.values():
        stretched = group.sites & reference_sites and group.sites - reference_sites
        if stretched and group.intra_group == "permit" and (local is None or group.name in local):
            own = (group.name, group.group_id, False)
            _add(group.vrf, unaware, own, permit_all)
            _add(group.vrf, own, unaware, permit_all)

    return {
        "default_action": DEFAULT_ACTION,
        "reserved": dict(sorted(reserved.items())),
        "replies": REPLIES,
        "contracts": [
            {
                "vrf": vrf or None,
                "src": src,
                "dst": dst,
                "src_group_id": src_id,
                "dst_group_id": dst_id,
                "entries": _entries(entries),
                "rules": sorted(rules),
            }
            for (vrf, src, dst), (src_id, dst_id, entries, rules) in sorted(merged.items())
        ],
    }


# Class-map match line per protocol of an entry without ports, from ONE table, with why it
# is not verified. A tcp/udp entry with a port is D (the guide's `match ipv4|ipv6 tcp dport N
# [to M]`, built in _class_map); the rest is the guide's silence (spec 4.1/4.3/4.12): the
# sample `show run` prints `match ip tcp` / `match ip udp` without saying how to configure
# them, and icmp and a permit-all class are not documented at all. None of these forms has
# an ipv6 variant, so IPv6 for such an entry is denied by the VRF's default deny.
_UNVERIFIED_CLASS_LINES: dict[str, tuple[str, str]] = {
    "tcp": ("match ip tcp", "only in the guide's sample show running-config"),
    "udp": ("match ip udp", "only in the guide's sample show running-config"),
    "icmp": ("match ip icmp", "not in the guide"),
    "any": ("match ip", "not in the guide"),
}
_FAMILIES = ("ipv4", "ipv6")


def _entry_key(entry: dict[str, Any]) -> tuple[str, int, int]:
    return entry["protocol"], entry["port_start"] or 0, entry["port_end"] or 0


def _port_span(start: int, end: int | None, separator: str) -> str:
    """``443`` for a single port, ``8000<separator>8010`` for a range."""
    return f"{start}" if end in (None, start) else f"{start}{separator}{end}"


def _entry_text(entry: dict[str, Any]) -> str:
    """``tcp/443``, ``udp/5000-5010``, ``tcp`` (port-less), ``any``."""
    protocol, start = entry["protocol"], entry["port_start"]
    return protocol if start is None else f"{protocol}/{_port_span(start, entry['port_end'], '-')}"


def class_map_name(entries: Iterable[dict[str, Any]]) -> str:
    """``GPO-`` and the digest of the sorted (protocol, start, end) set."""
    return "GPO-" + _digest(
        [[protocol, start or None, end or None] for protocol, start, end in sorted(map(_entry_key, entries))]
    )


def _class_map(who: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """One class-map: name, entry list and `match` lines (tcp/udp with ports for ipv4 and ipv6)."""
    lines: list[str] = []
    notes: list[str] = []
    for entry in sorted(entries, key=_entry_key):
        protocol, start, end = entry["protocol"], entry["port_start"], entry["port_end"]
        if protocol in ("tcp", "udp") and start is not None:
            if end is not None and end < start:
                raise _refuse(who, f"{protocol} port range {start}-{end} ends before it starts")
            lines.extend(f"match {family} {protocol} dport {_port_span(start, end, ' to ')}" for family in _FAMILIES)
        else:
            line, why = _UNVERIFIED_CLASS_LINES[protocol]
            lines.append(line)
            notes.append(
                f"`{line}` for {_entry_text(entry)} is {why}; it has no ipv6 form here, "
                "so IPv6 for this entry is denied by the default deny (fail closed)"
            )
    return {
        "name": class_map_name(entries),
        "entries": ", ".join(_entry_text(entry) for entry in sorted(entries, key=_entry_key)),
        "lines": lines,
        "notes": sorted(set(notes)),
    }


def _validate_group_ids(
    who: str, rows: list[dict[str, Any]], defined: set[str], by_name: dict[str, Group]
) -> dict[str, int]:
    """Group name -> its ID for every group the rows name or the VRFs define; refuses what cannot be numbered.

    A group without an ID (tag or profile reserved group not allocated), two groups on one ID and two groups
    with one CLI name are refused. A row end carries the ID the view gave it (a reserved group's, a stretched
    group's unaware half).
    """
    named_ids: dict[str, int | None] = {name: by_name[name].group_id for name in defined}
    for row in rows:
        named_ids[row["src"]], named_ids[row["dst"]] = row["src_group_id"], row["dst_group_id"]
    group_ids: dict[str, int] = {}
    for name, group_id in sorted(named_ids.items()):
        if group_id is None:
            raise _refuse(who, f"group {name} has no group id (tag or profile reserved group not allocated)")
        group_ids[name] = group_id
    by_id: dict[int, set[str]] = {}
    for name, group_id in group_ids.items():
        by_id.setdefault(group_id, set()).add(name)
    for group_id, names in sorted(by_id.items()):
        if len(names) > 1:
            raise _refuse(who, f"groups {sorted(names)} share group id {group_id}")
    _refuse_cli_clash(who, defined, "groups", "group name")
    return group_ids


@dataclass(frozen=True)
class ComponentClaim:
    """One host-facing port a component group classifies: ``match vlan <segment's VLAN> interface <port>`` on ``vtep``."""

    group: str
    segment_id: str
    vtep: str
    port: str


@dataclass(frozen=True)
class ComponentCoverage:
    """Where the component groups of a data center's segments are classified, and what stops them from being.

    ``claims``: the (VTEP, port) pairs, sorted. ``blockers``: component group name -> why it cannot be
    classified completely (sorted texts); the VRFs naming such a group are left unenforced.
    """

    claims: list[ComponentClaim]
    blockers: dict[str, list[str]]


def _capability_ids(interface: dict[str, Any]) -> set[str]:
    """The ids of what an interface is tagged with (segments, among others)."""
    return {str(item["id"]) for item in interface.get("capabilities") or [] if item.get("id")}


def _place_instance(
    instance: dict[str, Any], segment_id: str, *, vtep_names: set[str], other_vtep_names: set[str]
) -> tuple[set[tuple[str, str]], list[str]]:
    """((VTEP, port) pairs carrying the segment, problems) of one instance of a component of that segment.

    The host (utils/app_instances.physical_host) has its uplink/bond-member NICs; the far end of each NIC's
    cable (the near end skipped by id) is a switch port. A far end that is a VTEP of this data center is
    claimed when it, or its port-channel for a LAG member (never the member port: a LAG member without a
    port-channel is a problem), carries the segment; when it does not, that is a problem too (the component's
    traffic enters the VTEP unclassified). A cable that does not have exactly one far end is a problem. A far end that is a VTEP of another data center is that
    data center's to classify and is ignored; a far end that is a VTEP of none (a leaf below a VTEP, a
    firewall) cannot classify, so it is a problem. An instance reaching no VTEP at all is a problem too, in
    every data center of its segment: where it sits is not known.
    """
    typename = instance.get("typename")
    label = str(instance.get("name") or instance.get("id") or "?")
    host = physical_host(instance)
    if host is None:
        if typename in HOSTED_INSTANCE_TYPENAMES:
            return set(), [f"instance {label} has no hosting device"]
        return set(), [f"instance {label} ({typename or 'unknown kind'}) is not an on-prem device"]
    host_label = str(host.get("name") or host.get("id") or "?")
    ports: set[tuple[str, str]] = set()
    problems: list[str] = []
    placed = False
    for nic in host.get("interfaces") or []:
        nic_label = f"{host_label} {nic.get('name') or '?'}"
        endpoints = (nic.get("cable") or {}).get("endpoints") or []
        if not endpoints:
            continue
        # The near end is the NIC itself (by id); an endpoint with no id did not match the query's
        # inline fragment and is no end of anything. A cable that does not leave exactly one other end
        # (a bundle, a patch panel, a half-loaded cable) cannot say where the NIC lands.
        far_ends = [end for end in endpoints if end.get("id") and end["id"] != nic.get("id")]
        if len(far_ends) != 1:
            problems.append(f"{nic_label}: cable has {len(far_ends)} far ends")
            continue
        far_end = far_ends[0]
        switch = (far_end.get("device") or {}).get("name")
        if not switch or not far_end.get("name"):
            problems.append(f"{nic_label} is cabled to something that is not a switch port")
        elif switch in vtep_names:
            placed = True
            if far_end.get("role") == "lag":
                port_channel = far_end.get("lag") or {}
                if not port_channel.get("name"):
                    problems.append(
                        f"{nic_label} reaches {switch} {far_end['name']}, a LAG member with no port-channel"
                    )
                    continue
                port, carried = str(port_channel["name"]), _capability_ids(port_channel)
            else:
                port, carried = str(far_end["name"]), _capability_ids(far_end)
            if segment_id in carried:
                ports.add((str(switch), port))
            else:
                # Cabled to a VTEP of this data center that does not carry the segment on that port: the
                # component's traffic arrives there unclassified, so the group cannot be enforced.
                problems.append(f"{nic_label} reaches {switch} {port}, which is not tagged with the segment")
        elif switch in other_vtep_names:
            placed = True
        else:
            problems.append(f"{nic_label} is cabled to {switch}, which is a VTEP of no data center")
    if not placed and not problems:
        problems.append(f"instance {label} (host {host_label}) is not cabled to a VTEP")
    return ports, problems


def component_coverage(
    contracts: DataCenterContracts, *, vtep_names: set[str], other_vtep_names: set[str]
) -> ComponentCoverage:
    """The ports each component group of this data center's segments is classified on, and the blockers.

    Per component group (a tagged AppComponent of a microsegmented segment active in this data center):
    the (VTEP, port) pairs its instances' hosts are cabled to (``_place_instance``), keyed by
    (VTEP, port, segment): the interface selector cannot tell two components of one segment apart, so a
    port two of them land on is claimed by neither and blocks both. The same port in two segments is fine
    (a selector names the segment's VLAN). A component with no instance in this data center, and the
    component groups of a remote segment (the far end of a local row), have no blocker: nothing to
    classify here. An instance whose host resolves nowhere blocks every data center of its segment, since
    where it sits is not known (conservative).

    ``vtep_names`` are this data center's VTEPs, ``other_vtep_names`` those of the rest of the fleet.
    Computed from the fleet roots alone, so every VTEP of the data center gets the same coverage.
    """
    components_of = contracts.components_of
    owners: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    problems: dict[str, set[str]] = defaultdict(set)
    for segment in contracts.segments:
        segment_id = str(segment.get("id"))
        component_groups = {
            group.name for group in contracts.by_segment.get(segment_id, []) if group.kind == "component"
        }
        for tag in components_of.get(segment_id, []):
            group = str(tag.get("name"))
            if group not in component_groups:
                continue
            for component in tag.get("components") or []:
                if (component.get("network_segment") or {}).get("id") != segment_id:
                    continue
                for instance in component.get("instances") or []:
                    ports, found = _place_instance(
                        instance, segment_id, vtep_names=vtep_names, other_vtep_names=other_vtep_names
                    )
                    problems[group].update(found)
                    for vtep, port in ports:
                        owners[(vtep, port, segment_id)].add(group)
    claims: list[ComponentClaim] = []
    for (vtep, port, segment_id), groups in sorted(owners.items()):
        if len(groups) > 1:
            names = ", ".join(_label(group) for group in sorted(groups))
            for group in groups:
                problems[group].add(
                    f"port {port} of {vtep} is shared by components {names} of one segment "
                    "(an interface selector cannot tell them apart)"
                )
        else:
            claims.append(ComponentClaim(next(iter(groups)), segment_id, vtep, port))
    return ComponentCoverage(
        claims=sorted(claims, key=lambda claim: (claim.group, claim.vtep, claim.port)),
        blockers={group: sorted(found) for group, found in sorted(problems.items()) if found},
    )


@dataclass
class _Selectors:
    """What one device classifies: ``vlans`` (segment group -> its VLAN here), ``ports`` (component group -> its
    ``{vlan_id, name}`` host ports here), ``unclassified`` (VRF -> its segments with no classifier here) and
    ``problems`` (component group -> why a claimed port of this device cannot be rendered)."""

    vlans: dict[str, int]
    ports: dict[str, list[dict[str, Any]]]
    unclassified: dict[str, set[str]]
    problems: dict[str, list[str]]


def _selectors(
    contracts: DataCenterContracts,
    activations: Iterable[dict[str, Any]],
    *,
    claims: Iterable[ComponentClaim],
    device_name: str,
    interfaces: Iterable[dict[str, Any]],
) -> _Selectors:
    """The selectors of one device (``_Selectors``).

    A routed tenant segment activated on the device is classified by ``match vlan`` only when it has a
    group and a VLAN here and is not inline-terminated (pure L2 on a fabric switch). A component group is
    classified by ``match vlan <its segment's VLAN here> interface <port>`` on each port the coverage
    claims on THIS device, provided the device has that interface, tagged with the segment, and the
    segment a VLAN here; otherwise the claim is a problem of this device (inconsistent or not yet
    converged data) and the VRF stays unenforced.
    """
    segments = {str(segment.get("id")): segment for segment in contracts.all_segments}
    vlans: dict[str, int] = {}
    vlan_of_segment: dict[str, int] = {}
    unclassified: dict[str, set[str]] = {}
    for activation in activations:
        segment = activation.get("segment") or {}
        groups = contracts.by_segment.get(str(segment.get("id")))
        if groups and activation.get("vlan_id") and not is_inline_terminated(segment):
            vlans[groups[0].name] = int(activation["vlan_id"])
            vlan_of_segment[str(segment.get("id"))] = int(activation["vlan_id"])
        elif (found := segments.get(str(segment.get("id")))) is not None and is_routed_tenant_segment(found):
            unclassified.setdefault(segment_vrf(found) or "", set()).add(str(found.get("name")))
    own = {str(item["name"]): item for item in interfaces if item.get("name")}
    claimed: dict[str, set[str]] = defaultdict(set)
    problems: dict[str, list[str]] = defaultdict(list)
    vlan_of_group: dict[str, int] = {}
    for claim in claims:
        if claim.vtep != device_name:
            continue
        interface, vlan = own.get(claim.port), vlan_of_segment.get(claim.segment_id)
        if interface is None or claim.segment_id not in _capability_ids(interface):
            problems[claim.group].append(
                f"port {claim.port} is not an interface of {device_name} tagged with the segment"
            )
        elif vlan is None:
            problems[claim.group].append(
                f"the segment has no VLAN on {device_name}, so port {claim.port} cannot be matched"
            )
        else:
            claimed[claim.group].add(claim.port)
            vlan_of_group[claim.group] = vlan
    ports = {
        group: [{"vlan_id": vlan_of_group[group], "name": name} for name in sort_interface_list(list(names))]
        for group, names in claimed.items()
    }
    return _Selectors(vlans=vlans, ports=ports, unclassified=unclassified, problems=dict(problems))


_PORT_LIST_LIMIT = 8


def _claimed_ports_text(claims: Iterable[ComponentClaim], groups: Iterable[str]) -> str | None:
    """``<device> <port>, ...`` of the ports the given component groups are classified on, data-center wide
    (the same on every VTEP), sorted by device and naturally by port, at most ``_PORT_LIST_LIMIT`` listed."""
    wanted = set(groups)
    by_device: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        if claim.group in wanted:
            by_device[claim.vtep].add(claim.port)
    pairs = [
        f"{device} {port}" for device in sorted(by_device) for port in sort_interface_list(list(by_device[device]))
    ]
    if not pairs:
        return None
    more = len(pairs) - _PORT_LIST_LIMIT
    return ", ".join(pairs[:_PORT_LIST_LIMIT]) + (f" and {more} more" if more > 0 else "")


def _vrf_view(
    who: str,
    vrf: str,
    vrf_rows: list[dict[str, Any]],
    *,
    by_name: dict[str, Group],
    group_ids: dict[str, int],
    unclassified_id: int | None,
    no_classifier: set[str],
    component_blockers: dict[str, list[str]],
    claims: Iterable[ComponentClaim],
    staged: str | None,
) -> dict[str, Any]:
    """The contracts and the enforcement of one VRF (``_vxlan_gpo_build``).

    Enforced when the data center enforces (``staged`` is the reason it does not, listed first), no
    component group the VRF's contracts name has a blocker (``component_coverage``, plus the problems of
    this device's claimed ports) and no tenant segment it holds here lacks a classifier.

    The coverage part of that verdict is the data center's, identical on every VTEP of the VRF. The
    per-device part is not: ``component_blockers`` also holds the problems of THIS device's claimed ports
    (missing, untagged, no VLAN here), so one vPC peer can be left unenforced while the other enforces.
    ``classified_ports`` (data center wide, identical on every VTEP) lists the ports the VRF's enforcement
    relies on: component classification by interface + VLAN is documented-not-lab-verified.
    """
    vrf_rows = sorted(vrf_rows, key=lambda row: (row["src_group_id"], row["dst_group_id"]))
    forward = {(row["src"], row["dst"]) for row in vrf_rows}
    contract_views = []
    for row in vrf_rows:
        notes = []
        for end, label in ((row["src"], "source"), (row["dst"], "destination")):
            if end == UNCLASSIFIED:
                notes.append(
                    f"{_UNVERIFIED}: reserved group {group_ids[end]} (UNCLASSIFIED) as the {label} of a contract; "
                    "the guide does not define it"
                )
        if row["src"] != row["dst"] and (row["dst"], row["src"]) in forward:
            notes.append(f"{_UNVERIFIED}: two bidir contracts on one pair")
        contract_views.append(
            {
                "src": row["src_group_id"],
                "dst": row["dst_group_id"],
                "src_name": _label(row["src"]),
                "dst_name": _label(row["dst"]),
                "policy": class_map_name(row["entries"]),
                "notes": notes,
            }
        )
    components = sorted(
        {
            end
            for row in vrf_rows
            for end in (row["src"], row["dst"])
            if end in by_name and by_name[end].kind == "component"
        }
    )
    reasons = [staged] if staged else []
    reasons += [
        f"component group {_label(name)}: {text}" for name in components for text in component_blockers.get(name, [])
    ]
    reasons += [f"segment {name} has no group classifier on this device" for name in sorted(no_classifier)]
    # A staged data center always has its reason: the refusal is for a VRF that would really enforce.
    if not reasons and unclassified_id is None:
        raise _refuse(who, f"VRF {vrf} is enforced but the profile names no unclassified group id")
    return {
        "contracts": contract_views,
        "unenforced": "; ".join(_one_line(reason) for reason in reasons) or None,
        "enforce_tag": None if reasons else unclassified_id,
        "classified_ports": _one_line(ports) if (ports := _claimed_ports_text(claims, components)) else None,
    }


def _vxlan_gpo_build(scope: _DeviceScope) -> dict[str, Any]:
    """The NX-OS group policy one VTEP renders (module docstring).

    A VTEP of a reference fabric always gets it: with no contract row for the VRFs it carries the view is
    enablement only (empty ``class_maps``, ``groups`` and ``vrfs``).

    The scope's ``contracts`` are the cleaned fleet roots' contracts of the device's data center,
    ``activations`` the device's segment activations (``vlan_id`` and ``segment.id``: the VLAN
    ID of a group's segment here, which is per VLAN domain), ``vrfs`` the
    VRFs it carries (``vxlan.l3_vni_mappings``): a segment's VRF is its
    gateway namespace, the ``vrf_name`` of its L3 VNI mapping, and ``interfaces``
    its own cleaned interfaces (the host ports a component selector names must be
    among them, tagged with the segment).

    Each group dict carries ``vlan_id`` (its segment's VLAN here, segment groups)
    and ``interfaces`` (``{vlan_id, name}`` of the host ports of this device that
    classify it, component groups).
    A VRF is enforced (``security enforce``, deny by default) only when every
    component group its contracts name can be classified (no coverage blocker)
    and every routed tenant segment it holds on this device is classified: a
    segment activated here without a group or a VLAN has no classifier. Such a
    VRF gets its groups and contracts but is left unenforced, with the reasons. So does every VRF of a
    data center that does not enforce (``group_policy_enforce`` false, A6): that reason comes first.
    """
    who, dc_id, fleet, contracts = scope.who, scope.dc_id, scope.fleet, scope.contracts
    device_name, activations, interfaces = scope.device_name, scope.activations, scope.interfaces
    reserved = reserved_groups(contracts.profiles, scope.kind)
    view = vxlan_gpo_view(
        contracts.all_groups,
        contracts.rows,
        reserved=reserved,
        reference_sites=contracts.reference_sites,
        local=contracts.local_names,
    )
    on_device = scope.vrfs
    rows_by_vrf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in view["contracts"]:
        if row["vrf"] in on_device:
            rows_by_vrf[row["vrf"]].append(row)
    if not rows_by_vrf:
        # A VTEP with no contracts still enables GPO: a policy-unaware VTEP in a reference
        # site is the mixed state the profile excludes (spec 2.9). Nothing else is rendered,
        # so none of the group data is validated (the refusals below guard what is rendered).
        return {"class_maps": [], "groups": [], "vrfs": {}}
    _refuse_ambiguous_groups(who, contracts.all_groups)
    rows = [row for vrf_rows in rows_by_vrf.values() for row in vrf_rows]
    by_name = contracts.groups_by_name

    # Every group the VRFs hold on this data center must be defined: an enforced
    # VRF denies what no selector classified, so a rule-less segment would lose
    # its own intra-group traffic.
    defined = {row[end] for row in rows for end in ("src", "dst") if row[end] in by_name}
    defined |= {group.name for group in contracts.local if group.vrf in rows_by_vrf}
    group_ids = _validate_group_ids(who, rows, defined, by_name)

    class_maps: dict[str, dict[str, Any]] = {}
    for row in rows:
        class_map = _class_map(who, row["entries"])
        class_maps.setdefault(class_map["name"], class_map)

    coverage = component_coverage(
        contracts,
        vtep_names={str(vtep.get("name")) for vtep in contracts.vteps},
        other_vtep_names={
            str(vtep.get("name")) for other_id, vteps in fleet.items() if other_id != dc_id for vtep in vteps
        },
    )
    selectors = _selectors(
        contracts, activations, claims=coverage.claims, device_name=device_name, interfaces=interfaces
    )
    component_blockers = {
        group: sorted({*coverage.blockers.get(group, []), *selectors.problems.get(group, [])})
        for group in {*coverage.blockers, *selectors.problems}
    }
    group_defs = []
    for name in sorted(defined, key=lambda item: group_ids[item]):
        group = by_name[name]
        notes = []
        if group.intra_group == "deny":
            notes.append(
                f"{_UNVERIFIED}: {_label(name)}: intra-group deny of the microsegmented segment {group.segment} is "
                "not rendered (10.6(3)F intra-group isolation); its hosts talk freely until it is"
            )
        group_defs.append(
            {
                "id": group_ids[name],
                "name": _label(name),
                "vlan_id": selectors.vlans.get(name),
                "interfaces": selectors.ports.get(name, []),
                "notes": notes,
            }
        )

    staged = _staged_reason(contracts, dc_id, "contracts are configured, security enforce is not")
    return {
        "class_maps": [class_maps[name] for name in sorted(class_maps)],
        "groups": group_defs,
        "vrfs": {
            vrf: _vrf_view(
                who,
                vrf,
                vrf_rows,
                by_name=by_name,
                group_ids=group_ids,
                unclassified_id=reserved["unclassified"],
                no_classifier=selectors.unclassified.get(vrf, set()),
                component_blockers=component_blockers,
                claims=coverage.claims,
                staged=staged,
            )
            for vrf, vrf_rows in sorted(rows_by_vrf.items())
        },
    }
