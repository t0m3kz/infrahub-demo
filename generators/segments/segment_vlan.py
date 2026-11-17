"""Realizes a segment's local VLAN ID per device (a ManagedSegmentVlan row is on one device); the rows are owned
by the generator that writes them (tracked). See domain-model.md VLAN rows. MLAG peers share ONE id: an existing id wins, else the lowest id free on every device (WARNING
on fallback; none free fails); a row's id is never re-chosen. Ids are read and written under a per-device lock (sorted,
inside the segment lock).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from generators.plan import Hfid, NodeSpec, RetireSpec, TopologyPlan
from generators.protocols import DcimPhysicalDevice, ManagedMLAG, ManagedSegmentVlan
from utils.data_cleaning import clean_data
from utils.fabric_roles import VTEP_ROLES
from utils.limits import (
    CUSTOMER_VLAN_ID_MIN,
    GROUP_PROVIDER_STUBS,
    GROUP_SDWAN_GATEWAYS,
    LOCAL_VLAN_ID_MAX,
)
from utils.segment_vlan import ROW_KIND, mlag_id, shared_vlan_id, shared_vni_band

if TYPE_CHECKING:
    from generators.base import BaseGenerator

# Devices a stretched segment is realized on: every VTEP of the hosting parents (VTEP_ROLES). An SD-WAN gateway or
# provider stub in a zone has the `edge` role too but terminates no VXLAN, so it is no border gateway.
NON_BORDER_GATEWAY_GROUPS = frozenset({GROUP_SDWAN_GATEWAYS, GROUP_PROVIDER_STUBS})

_SEGMENT_VLAN_DOMAINS_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/segment_vlans.gql"

SWITCHED_PORT_KINDS = ("DcimPhysicalInterface", "DcimLAGInterface")
"""The kinds of DcimSwitchedInterface: the ports that can carry a segment (a member of its switch's VLAN row)."""

# Ports of a segment, read from the port side: a port is a member of its switch's VLAN row (written only by the
# reconcile).
SEGMENT_PORTS_QUERY = """
query segment_ports($row_ids: [ID]) {
  DcimPhysicalInterface(capabilities__ids: $row_ids) {
    edges {
      node {
        id
        device { node { id } }
        capabilities(ids: $row_ids) { edges { node { id __typename } } }
      }
    }
  }
  DcimLAGInterface(capabilities__ids: $row_ids) {
    edges {
      node {
        id
        device { node { id } }
        capabilities(ids: $row_ids) { edges { node { id __typename } } }
      }
    }
  }
}
"""


def vlan_device_lock_key(device_id: str) -> str:
    """The resource_lock key serializing VLAN id choice on one device."""
    return f"vlan-device-ids-{device_id}"


def segment_lock_key(segment_id: str) -> str:
    """The resource_lock key serializing the per-segment reconciliation across both generators."""
    return f"segment-vlan-domains-{segment_id}"


@dataclass(frozen=True)
class SegmentRows:
    """What a reconcile decided about a segment's VLAN rows, by device id: ``new`` {device: segment deployment the row
    goes under}, ``kept`` {device: (segment deployment, vlan_id)} of the rows that stay, ``stale`` {device: (row id,
    vlan_id)} of the rows that go, ``names`` {device: name} of every device named.
    """

    names: Mapping[str, str]
    new: Mapping[str, str]
    kept: Mapping[str, tuple[str, int]]
    stale: Mapping[str, tuple[str, int]]

    @property
    def stale_row_ids(self) -> set[str]:
        return {row_id for row_id, _ in self.stale.values()}


def plan_segment_rows(rows: SegmentRows, chosen: Mapping[str, int]) -> TopologyPlan:
    """The plan that writes a segment's rows: each row is owned by the generator that writes it (tracked: the run's
    group deletes it when it is no longer planned), named by its device and VLAN id. A new row takes the place of the
    stale row of its device (``replaces``: deleted before the new one is written, so its VLAN id is free; the same id
    keeps the row, moved under the new deployment); the stale rows of devices that get no new row are retired.
    """
    plan = TopologyPlan(root_id=None)

    def device(device_id: str) -> Hfid:
        return Hfid("DcimPhysicalDevice", (rows.names[device_id],))

    def row_hfid(device_id: str, vlan_id: int) -> Hfid:
        return Hfid(ROW_KIND, (rows.names[device_id], str(vlan_id)))

    wanted = {device_id: (deployment_id, chosen[device_id]) for device_id, deployment_id in rows.new.items()}
    wanted.update(rows.kept)
    for device_id, (deployment_id, vlan_id) in sorted(wanted.items()):
        hfid = row_hfid(device_id, vlan_id)
        replaced = row_hfid(device_id, rows.stale[device_id][1]) if device_id in rows.stale else None
        plan.add(
            NodeSpec(
                f"row-{device_id}",
                ManagedSegmentVlan,
                {"segment_deployment": {"id": deployment_id}, "device": device(device_id), "vlan_id": vlan_id},
                hfid=hfid,
                ownership="owned",
                replaces=(replaced,) if replaced is not None and replaced != hfid else (),
            )
        )
    gone = tuple(
        row_hfid(device_id, vlan_id)
        for device_id, (_, vlan_id) in sorted(rows.stale.items())
        if device_id not in wanted
    )
    if gone:
        plan.add(RetireSpec(gone))
    return plan


def _choose_vlan_ids(
    gen: BaseGenerator,
    segment_name: str,
    groups: list[tuple[str, ...]],
    new: set[str],
    taken: dict[str, set[int]],
    held: dict[str, int],
    preferred: int | None,
    fixed: int | None = None,
) -> dict[str, int]:
    """The vlan_id for each device in ``new``; each group (MLAG pair or single) shares one id. A group takes
    ``held``, else ``preferred``, else the lowest id free on all members (WARNING on fallback; none free fails).
    ``fixed`` is a shared segment's id: no fallback.
    """
    if fixed is not None:
        for group in groups:
            clash = [device_id for device_id in group if fixed in taken[device_id]]
            if clash:
                gen.fail(
                    f"Segment {segment_name} is shared: VLAN ID {fixed} (from its VNI) is already used by another "
                    f"segment on {', '.join(clash)}"
                )
        return {device_id: fixed for group in groups for device_id in group if device_id in new}
    band = range(CUSTOMER_VLAN_ID_MIN, LOCAL_VLAN_ID_MAX + 1)
    default = preferred
    if default is None:
        busy = set().union(*(taken[device_id] for group in groups for device_id in group))
        default = next((vlan_id for vlan_id in band if vlan_id not in busy), None)
    chosen: dict[str, int] = {}
    for group in groups:
        wanted = next((held[device_id] for device_id in group if device_id in held), default)
        busy = set().union(*(taken[device_id] for device_id in group))
        if wanted is not None and wanted not in busy:
            common: int | None = wanted
        else:
            common = next((vlan_id for vlan_id in band if vlan_id not in busy), None)
            reason = (
                f"VLAN ID {wanted} is already used by another segment on {', '.join(group)}"
                if wanted is not None
                else f"no VLAN ID is free on every device of the segment ({', '.join(group)} included)"
            )
            gen.logger.warning(
                f"Segment {segment_name}: {reason} — using {common if common is not None else 'its own lowest'} "
                "there, so the segment has different VLAN IDs across devices"
            )
        for device_id in group:
            if device_id not in new:
                continue
            if common is not None:
                chosen[device_id] = common
                continue
            own = next((vlan_id for vlan_id in band if vlan_id not in taken[device_id]), None)
            if own is None:
                gen.fail(
                    f"Device {device_id} has no free VLAN ID in {CUSTOMER_VLAN_ID_MIN}-{LOCAL_VLAN_ID_MAX} "
                    f"for segment {segment_name}"
                )
            gen.logger.warning(
                f"Segment {segment_name}: no VLAN ID is free on both members of {', '.join(group)} — "
                f"using {own} on {device_id}, so the pair's VLAN IDs differ"
            )
            chosen[device_id] = own
    return chosen


async def _read_vlan_ids(
    gen: BaseGenerator,
    segment_deployment_ids: list[str],
    members: list[str],
    kept_devices: list[str],
    stale_row_ids: frozenset[str],
) -> tuple[dict[str, set[int]], dict[str, int], int | None]:
    """What the devices' rows say, for the choice of an id: ({device id: ids other segments hold}, {device id: the
    id this segment holds}, the lowest id the segment holds anywhere). A row about to go (``stale_row_ids``) says
    nothing: its id is free.
    """
    rows = await gen.client.filters(
        kind=ManagedSegmentVlan,
        device__ids=sorted({*members, *kept_devices}),
        include=["segment_deployment", "device"],
    )
    taken: dict[str, set[int]] = {device_id: set() for device_id in members}
    held: dict[str, int] = {}
    own_ids: list[int] = []
    for row in rows:
        if row.id in stale_row_ids:
            continue
        device_id = row.device.id
        if device_id is None:
            continue
        if row.segment_deployment.id in segment_deployment_ids:
            own_ids.append(row.vlan_id.value)
            held[device_id] = row.vlan_id.value
        elif device_id in taken:
            taken[device_id].add(row.vlan_id.value)
    return taken, held, min(own_ids, default=None)


async def _write_segment_vlans(
    gen: BaseGenerator,
    segment_name: str,
    segment_deployment_ids: list[str],
    rows: SegmentRows,
    groups: list[tuple[str, ...]],
    kept_devices: list[str],
    fixed: int | None = None,
) -> None:
    """Choose the vlan_id of each new row (``fixed``: a shared segment's id) and write the segment's rows as one
    plan: the new ones, the ones that stay (re-saved: the run tracks only what it saves) and the stale ones removed.
    The ids are read and the plan applied under the lock of every device a new row lands on (sorted, inside the
    segment lock): the rows are written before another run reads the ids as taken.
    """
    members = sorted({device_id for group in groups for device_id in group})
    async with gen.plan_locks(*(vlan_device_lock_key(device_id) for device_id in members)):
        chosen: dict[str, int] = {}
        if rows.new:
            taken, held, preferred = await _read_vlan_ids(
                gen, segment_deployment_ids, members, kept_devices, frozenset(rows.stale_row_ids)
            )
            chosen = _choose_vlan_ids(gen, segment_name, groups, set(rows.new), taken, held, preferred, fixed)
        await gen.apply_plan(plan_segment_rows(rows, chosen))
    for device_id in sorted(chosen):
        gen.logger.info(f"  Allocated VLAN ID {chosen[device_id]} on device {device_id} for segment {segment_name}")


def _device_deployment_ids(hosting_parent: dict[str, Any]) -> list[str]:
    """Deployment ids whose devices a segment can land on: the hosting parent plus its pods, zones and virtual
    colocations.
    """
    pod_ids = [child["id"] for child in hosting_parent.get("children") or [] if child.get("id")]
    return [hosting_parent["id"], *pod_ids]


async def fetch_segment_vlan_state(gen: BaseGenerator, segment_id: str) -> dict[str, Any] | None:
    """The reconciliation inputs (segment_vlans.gql plus the row ports) per ManagedSegmentDeployment; None for a
    non-VXLAN segment.
    """
    result = await gen.client.execute_graphql(
        query=_SEGMENT_VLAN_DOMAINS_QUERY_PATH.read_text(), variables={"segment_id": segment_id}
    )
    cleaned = clean_data(result)
    segments = cleaned.get("ManagedVxlanSegment") or []
    if not segments:
        return None
    deployments: list[dict[str, Any]] = []
    for segment_deployment in cleaned.get("ManagedSegmentDeployment") or []:
        rows = {
            device_id: {"id": row["id"], "vlan_id": row.get("vlan_id")}
            for row in segment_deployment.get("vlans") or []
            if (device_id := (row.get("device") or {}).get("id"))
        }
        deployments.append(
            {
                "id": segment_deployment["id"],
                "deployment_id": (segment_deployment.get("deployment") or {}).get("id"),
                "vni": segment_deployment.get("vni"),
                "rows": rows,
                # The ports assigned to the deployment (its interface `capabilities`): a person's request.
                "requested": [
                    port["id"]
                    for port in segment_deployment.get("capabilities") or []
                    if port.get("id") and port.get("typename") in SWITCHED_PORT_KINDS
                ],
            }
        )
    segments[0]["ports"] = await _fetch_segment_ports(
        gen, [row["id"] for item in deployments for row in item["rows"].values()]
    )
    return {"segment": segments[0], "deployments": deployments}


async def _fetch_segment_ports(gen: BaseGenerator, row_ids: list[str]) -> list[dict[str, Any]]:
    """The switch ports that are members of the segment's VLAN rows (one read)."""
    if not row_ids:
        return []
    result = await gen.client.execute_graphql(query=SEGMENT_PORTS_QUERY, variables={"row_ids": sorted(row_ids)})
    return _segment_ports(result, set(row_ids))


def _deployment_by_device_deployment(state: dict[str, Any]) -> dict[str, str]:
    """{deployment id a device sits in: id of the segment's deployment there}."""
    by_hosting = {item["deployment_id"]: item["id"] for item in state["deployments"] if item["deployment_id"]}
    mapping = dict(by_hosting)
    for customer_deployment in state["segment"].get("customer_deployments") or []:
        parent = customer_deployment.get("parent") or {}
        if segment_deployment_id := by_hosting.get(parent.get("id")):
            for deployment_id in _device_deployment_ids(parent):
                mapping.setdefault(deployment_id, segment_deployment_id)
    return mapping


def _segment_ports(result: dict[str, Any], row_ids: set[str]) -> list[dict[str, Any]]:
    """The switch ports carrying the segment as ``{"id", "device_id", "row_id"}``, from SEGMENT_PORTS_QUERY."""
    ports: list[dict[str, Any]] = []
    for kind in SWITCHED_PORT_KINDS:
        for port_edge in (result.get(kind) or {}).get("edges") or []:
            node = port_edge["node"]
            for edge in (node.get("capabilities") or {}).get("edges") or []:
                peer = edge.get("node") or {}
                if peer.get("__typename") != ROW_KIND or peer.get("id") not in row_ids:
                    continue
                ports.append(
                    {
                        "id": node["id"],
                        "device_id": ((node.get("device") or {}).get("node") or {}).get("id"),
                        "row_id": peer["id"],
                    }
                )
    return ports


async def _segment_vlan_devices(gen: BaseGenerator, segment: dict[str, Any], port_device_ids: set[str]) -> list[Any]:
    """Every device that needs a row: port devices, plus every VTEP of the hosting parents for a stretched segment,
    plus MLAG peers.
    """
    tagged_device_ids = sorted(port_device_ids)
    deployment_ids: list[str] = []
    if (segment.get("stretch_scope") or "local") != "local":
        for customer_deployment in segment.get("customer_deployments") or []:
            parent = customer_deployment.get("parent") or {}
            if parent.get("id"):
                deployment_ids.extend(_device_deployment_ids(parent))

    # The two reads do not depend on each other: together, then merged in this order (a VTEP read replaces the same
    # device read first)
    tagged, vteps = await asyncio.gather(
        gen.client.filters(kind=DcimPhysicalDevice, ids=tagged_device_ids, include=["capabilities", "deployment"])
        if tagged_device_ids
        else asyncio.sleep(0, result=[]),
        gen.client.filters(
            kind=DcimPhysicalDevice,
            deployment__ids=list(dict.fromkeys(deployment_ids)),
            role__values=sorted(VTEP_ROLES),
            include=["capabilities", "member_of_groups", "deployment"],
        )
        if deployment_ids
        else asyncio.sleep(0, result=[]),
    )
    devices: dict[str, Any] = {device.id: device for device in tagged}
    for device in vteps:
        if not NON_BORDER_GATEWAY_GROUPS.isdisjoint(_group_names(device)):
            continue
        devices[device.id] = device

    mlag_ids = sorted({mlag for device in devices.values() if (mlag := _mlag_of(device))})
    if mlag_ids:
        mlags = await gen.client.filters(kind=ManagedMLAG, ids=mlag_ids, include=["capabilities"])
        peer_ids = sorted(
            {peer.id for mlag in mlags for peer in mlag.capabilities.peers if peer.id and peer.id not in devices}
        )
        if peer_ids:
            for device in await gen.client.filters(
                kind=DcimPhysicalDevice, ids=peer_ids, include=["capabilities", "deployment"]
            ):
                devices[device.id] = device
    return list(devices.values())


async def _read_devices(gen: BaseGenerator, device_ids: list[str]) -> list[Any]:
    """The devices ``device_ids``, one read (their names)."""
    return await gen.client.filters(kind=DcimPhysicalDevice, ids=device_ids)


def _mlag_of(device: Any) -> str | None:
    """The id of the MLAG the device (fetched with its capabilities) is a member of."""
    capabilities = getattr(device, "capabilities", None)
    return mlag_id((peer.typename, peer.id) for peer in capabilities.peers) if capabilities else None


def _group_names(device: Any) -> set[str]:
    """Names of the groups a device was fetched with (a group's HFID is its name)."""
    return {peer.hfid[0] for peer in device.member_of_groups.peers if peer.hfid}


def _shared_vlan_id(gen: BaseGenerator, segment_name: str, state: dict[str, Any]) -> int | None:
    """The VLAN id of a shared-scope segment (from its VNI), None for a local one; fails without a single in-band
    VNI.
    """
    if (state["segment"].get("vlan_scope") or "local") != "shared":
        return None
    vnis = {item["vni"] for item in state["deployments"] if item.get("vni") is not None}
    vlan_ids = {shared_vlan_id(vni) for vni in vnis}
    if len(vnis) != 1 or None in vlan_ids:
        gen.fail(
            f"Segment {segment_name} is shared: it needs ONE VNI in its deployments within the shared band "
            f"(VNI {shared_vni_band()}, the first numbers of the global pool), found {sorted(vnis) or 'none'}"
        )
    return next(iter(vlan_ids))


async def reconcile_segment_vlans(
    gen: BaseGenerator,
    segment_id: str,
    segment_name: str,
    state: dict[str, Any] | None,
    port_device_ids: set[str],
) -> bool:
    """Reconcile the segment's rows: create missing ones, delete those on devices that no longer need it; True if
    one changed. Desired rows are the port devices, the VTEPs of a stretched segment and MLAG peers. The caller
    holds the segment lock; ``state`` is a fresh state or None.
    """
    if state is None:
        state = await fetch_segment_vlan_state(gen, segment_id)
    if state is None:
        gen.logger.info(f"Segment {segment_name}: not a VXLAN segment — no VLAN rows")
        return False

    devices = await _segment_vlan_devices(gen, state["segment"], port_device_ids)
    deployment_by_device_deployment = _deployment_by_device_deployment(state)

    by_group: dict[str, list[Any]] = {}
    for device in devices:
        by_group.setdefault(_mlag_of(device) or device.id, []).append(device)

    desired: dict[str, str] = {}
    undecided: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for members in by_group.values():
        segment_deployments = {
            device.id: deployment_by_device_deployment.get(getattr(getattr(device, "deployment", None), "id", None))
            for device in members
        }
        for device in members:
            if segment_deployments[device.id] is None:
                gen.logger.warning(
                    f"Segment {segment_name}: device {device.name.value} sits in no deployment the segment is "
                    "activated in — no VLAN ID for it"
                )
        if len(set(segment_deployments.values())) > 1:
            undecided.update(segment_deployments)
            if None not in segment_deployments.values():
                gen.logger.warning(
                    f"Segment {segment_name}: the MLAG pair {', '.join(sorted(segment_deployments))} sits in two "
                    "deployments — left as it is"
                )
        elif None in segment_deployments.values():
            undecided.update(segment_deployments)
        else:
            desired.update(segment_deployments)
            groups.append(tuple(sorted(segment_deployments)))

    existing: dict[str, tuple[str, str, int]] = {
        device_id: (row["id"], item["id"], row["vlan_id"])
        for item in state["deployments"]
        for device_id, row in item["rows"].items()
    }
    for group in groups:
        ids = {existing[device_id][2] for device_id in group if device_id in existing}
        if len(group) > 1 and len(ids) > 1:
            gen.logger.warning(
                f"Segment {segment_name}: the MLAG pair {' and '.join(group)} has different VLAN IDs "
                f"({', '.join(f'{device_id}: {existing[device_id][2]}' for device_id in group if device_id in existing)}) "
                "— not renumbered, fix one row by hand"
            )

    stale = {
        device_id: (row_id, vlan_id)
        for device_id, (row_id, deployment_id, vlan_id) in sorted(existing.items())
        if device_id not in undecided and desired.get(device_id) != deployment_id
    }
    new = {
        device_id: deployment_id
        for device_id, deployment_id in desired.items()
        if existing.get(device_id, (None, None, None))[1] != deployment_id
    }
    # The rows that stay are written again: the run tracks only what it saves, and a row it no longer saves goes
    kept = {
        device_id: (deployment_id, vlan_id)
        for device_id, (_, deployment_id, vlan_id) in existing.items()
        if device_id not in stale and device_id not in new
    }
    names = {device.id: device.name.value for device in devices}
    if missing := sorted(device_id for device_id in stale if device_id not in names):
        names.update({device.id: device.name.value for device in await _read_devices(gen, missing)})
    rows = SegmentRows(names=names, new=new, kept=kept, stale=stale)
    if rows.new or rows.kept or rows.stale:
        await _write_segment_vlans(
            gen,
            segment_name,
            [item["id"] for item in state["deployments"]],
            rows,
            [group for group in groups if not new.keys().isdisjoint(group)],
            sorted(set(desired) & existing.keys() - new.keys()),
            _shared_vlan_id(gen, segment_name, state) if new else None,
        )

    gen.logger.info(
        f"Segment {segment_name}: {len(desired)} device(s) need it (peers included) — "
        f"created {len(new)}, removed {len(stale)} row(s)"
    )
    return bool(new or stale)
