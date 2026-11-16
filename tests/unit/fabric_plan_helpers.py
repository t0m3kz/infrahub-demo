"""What the fabric generators read before they plan, answered from the in-memory client (tests/unit/plan_graph_helpers.py):
the routing state and the shared objects of the devices an earlier run wrote, so a re-run plans against what it left.
"""

from __future__ import annotations

from typing import Any

from generators.capabilities.bgp import (
    RoutingReads,
    SharedFacts,
    overlay_as_description,
    overlay_key_name,
    super_spine_as_description,
    underlay_key_name,
)
from generators.capabilities.ospf import ospf_area_name
from generators.dc.routing_inputs import RoutingInputs
from generators.plan import Hfid
from tests.unit.plan_graph_helpers import HfidClient


def _id_of(value: Any) -> str | None:
    """The id a stored relationship value points at (a resolved reference is ``{"id": ...}``, a bare id a string)."""
    if isinstance(value, dict):
        return value.get("id")
    return value if isinstance(value, str) else None


def stored_routing_inputs(
    client: HfidClient, fabric_name: str, names: list[str], *, address_family: bool = True
) -> RoutingInputs:
    """The ``RoutingInputs`` of ``names`` as the store holds them."""
    nodes = list(client.store.values())
    device_name = {node.id: node.data["name"] for node in nodes if node.kind == "DcimPhysicalDevice"}
    by_description = {node.data.get("description"): node.id for node in nodes if node.kind == "RoutingAutonomousSystem"}
    by_name = {node.data["name"]: node.id for node in nodes if "name" in node.data}
    loopbacks: dict[str, tuple[Hfid, str | None]] = {}
    for node in nodes:
        if node.kind == "DcimVirtualInterface" and node.data.get("role") == "loopback":
            device = device_name.get(_id_of(node.data["device"]) or "")
            if device in names:
                loopbacks[device] = (
                    Hfid("DcimVirtualInterface", (device, node.data["name"])),
                    _id_of(node.data.get("ip_address")),
                )
    underlay_as: dict[str, str] = {}
    overlay: set[str] = set()
    for node in nodes:
        if node.kind != "ManagedBGP" or not node.data.get("capabilities"):
            continue
        device = device_name.get(_id_of(node.data["capabilities"][0]) or "", "")
        if device not in names:
            continue
        if node.data["process_role"] == "overlay":
            overlay.add(device)
        elif local_as := _id_of(node.data.get("local_as")):
            underlay_as[device] = local_as
    facts = SharedFacts(
        overlay_as_id=by_description.get(overlay_as_description(fabric_name)),
        super_spine_as_id=by_description.get(super_spine_as_description(fabric_name)),
        ospf_area_id=by_name.get(ospf_area_name(fabric_name)),
        underlay_password_id=by_name.get(underlay_key_name(fabric_name)),
        overlay_password_id=by_name.get(overlay_key_name(fabric_name)),
        evpn_address_family_id="af-evpn" if address_family else None,
    )
    return RoutingInputs(
        reads=RoutingReads(loopbacks=loopbacks, underlay_as=underlay_as, overlay=frozenset(overlay)), facts=facts
    )
