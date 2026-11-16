"""The plan of the cables between two device layers: a pure function of the ports of both layers (read from their
templates before the run), with no write and no read of anything this run creates (generators/plan.py).

The pairing is ``CablingPlanner`` (speed groups, offsets, port sorting), the cable name, type and attributes those of
the generators'. A chain (``plan_chain_legs``: border <-> firewall <-> load balancer) is the same links
over the ports of a role, with the ports an earlier leg took left out. A link is: the P2P prefix and its two addresses (``LinkPrefixSpec``), both ports
updated (description, status, address; untracked, they belong to a template), and the cable last (tracked), which
references both ports by HFID so the reconciler writes it after them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, cast

from typing_extensions import TypedDict

from generators.dcim.device_plan import device_hfid, port_hfid
from generators.dcim.far_end import far_end_interfaces
from generators.dcim.template_interfaces import TemplatePort
from generators.helpers.cable_attributes import RackPosition, cable_attributes, endpoint_label
from generators.helpers.cabling import CableTypeDetector, CablingPlanner
from generators.model.errors import GeneratorError
from generators.plan import Hfid, LinkPrefixSpec, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import DcimPhysicalInterface

CABLE_KIND = "DcimCable"
CablingScenario = Literal["pod", "rack", "intra_rack", "intra_rack_middle", "intra_rack_mixed", "chain"]
ELSEWHERE = "?"
"""``ChainPort.cabled_to`` of a port whose cable ends somewhere that is not a physical port: taken, whatever the leg."""


class ChainHop(TypedDict, total=False):
    """One stop in a chain of ``plan_chain_legs``; an endpoint hop needs the role facing its one
    neighbor, an interior hop needs both.
    """

    devices: list[str]
    """Device names for this hop. An empty list makes every leg touching this
    hop a no-op ."""
    up_role: str
    """Interface role facing the PREVIOUS hop in the chain. Omit/empty for the
    first hop (it has no previous neighbor)."""
    down_role: str
    """Interface role facing the NEXT hop in the chain. Omit/empty for the
    last hop (it has no next neighbor)."""


@dataclass(frozen=True)
class PortFacts:
    """A port known before it exists: what ``CablingPlanner`` reads of an interface (``device.display_label``,
    ``name.value``, ``interface_type.value``).
    """

    device_name: str
    port_name: str
    type_name: str | None

    @property
    def hfid(self) -> Hfid:
        return port_hfid(self.device_name, self.port_name)

    @property
    def device(self) -> SimpleNamespace:
        return SimpleNamespace(display_label=self.device_name)

    @property
    def name(self) -> SimpleNamespace:
        return SimpleNamespace(value=self.port_name)

    @property
    def interface_type(self) -> SimpleNamespace:
        return SimpleNamespace(value=self.type_name)


@dataclass(frozen=True)
class Link:
    """One planned cable: its two ends (bottom first) and the keys of its specs."""

    bottom: PortFacts
    top: PortFacts
    cable_name: str
    cable_key: str


@dataclass
class CablingOutcome:
    """The links of one cabling and what could not be planned: the caller raises ``problems`` (``raise_problems``) once the
    whole plan is built, before the first write.
    """

    links: list[Link] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def template_port_facts(
    devices: Sequence[str],
    names: Sequence[str],
    template_ports: Sequence[Any],
    *,
    label: str,
    problems: list[str],
) -> list[PortFacts]:
    """The ports called ``names`` of each device, typed as their template types them (``template_ports``: ``TemplatePort``).
    A name the template lacks is a problem (appended to ``problems``, raised with the others before anything is written)
    and no port.
    """
    by_name = {port.name: port for port in template_ports}
    if missing := [name for name in names if name not in by_name]:
        problems.append(f"{label} template has no port {missing}: those links cannot be planned")
    return [
        PortFacts(device, name, by_name[name].interface_type) for device in devices for name in names if name in by_name
    ]


def raise_problems(problems: Sequence[str]) -> None:
    """Fail a plan that cannot cable what it was asked to, naming every problem (each a missing port or an empty layer).
    Called with the builder's problems once the whole plan is built, so nothing of it is written.
    """
    if problems:
        raise GeneratorError("Cannot plan the cabling, nothing is written:\n- " + "\n- ".join(dict.fromkeys(problems)))


def plan_cabling(
    plan: TopologyPlan,
    *,
    bottom_ports: Sequence[PortFacts],
    top_ports: Sequence[PortFacts],
    strategy: CablingScenario,
    offset: int,
    bottom_sorting: Literal["top_down", "bottom_up"],
    top_sorting: Literal["top_down", "bottom_up"],
    deployment_id: str,
    pool_id: str | NodeRef | None,
    p2p_prefix_length: int,
    positions: Mapping[str, RackPosition | None],
    skip: bool = False,
) -> CablingOutcome:
    """Add the links between ``bottom_ports`` and ``top_ports`` to ``plan``. A layer without ports is a problem the caller
    collects (``CablingOutcome.problems``) and raises with every other one (``raise_problems``) before anything is
    written; an impossible pairing (``CablingPlanError``) raises at once. ``skip``: a port of the layers is missing (the
    caller recorded it as a problem), so the pairing would only fail for that reason: nothing is planned and the caller's
    problems name the ports.
    """
    outcome = CablingOutcome()
    if skip:
        return outcome
    if not bottom_ports or not top_ports:
        outcome.problems.append(
            f"No ports to cable {sorted({port.device_name for port in bottom_ports})} -> "
            f"{sorted({port.device_name for port in top_ports})}: bottom ports={len(bottom_ports)}, "
            f"top ports={len(top_ports)} — skipping cabling"
        )
        return outcome
    pairs = CablingPlanner(
        bottom_interfaces=bottom_ports, top_interfaces=top_ports, bottom_sorting=bottom_sorting, top_sorting=top_sorting
    ).build_cabling_plan(scenario=strategy, cabling_offset=offset)
    if not pairs:
        outcome.problems.append(
            f"No cabling connections planned for {sorted({port.device_name for port in bottom_ports})} -> "
            f"{sorted({port.device_name for port in top_ports})} (strategy={strategy}, offset={offset})"
        )
        return outcome
    for bottom, top in pairs:
        outcome.links.append(
            plan_link(
                plan,
                cast(PortFacts, bottom),  # the planner hands back the objects it was given
                cast(PortFacts, top),
                deployment_id=deployment_id,
                pool_id=pool_id,
                p2p_prefix_length=p2p_prefix_length,
                positions=positions,
            )
        )
    return outcome


def plan_link(
    plan: TopologyPlan,
    bottom: PortFacts,
    top: PortFacts,
    *,
    deployment_id: str,
    pool_id: str | NodeRef | None,
    p2p_prefix_length: int,
    positions: Mapping[str, RackPosition | None],
) -> Link:
    """Add one link to ``plan``: the P2P prefix and addresses when ``pool_id`` is set, both ports (updated, untracked) and
    the cable (owned) that references them. A caller that chose its own port pairs calls it; ``plan_cabling`` calls it
    for the pairs its planner makes.
    """
    endpoint_names = sorted(
        [endpoint_label(bottom.device_name, bottom.port_name), endpoint_label(top.device_name, top.port_name)]
    )
    cable_name = "__".join(endpoint_names)
    cable_type = CableTypeDetector.detect_cable_type(bottom.type_name, top.type_name)
    addresses: tuple[NodeRef, NodeRef] | tuple[None, None] = (None, None)
    if pool_id:
        prefix = LinkPrefixSpec(
            key=f"p2p:{cable_name}",
            ends=(bottom.hfid, top.hfid),
            pool_id=pool_id,
            prefix_length=p2p_prefix_length,
            ownership="owned",
        )
        plan.add(prefix)
        addresses = (NodeRef(prefix.address_keys[0]), NodeRef(prefix.address_keys[1]))
    for port, address in zip((bottom, top), addresses, strict=True):
        plan.add(
            NodeSpec(
                key=f"port:{port.device_name}/{port.port_name}",
                kind="DcimPhysicalInterface",
                data={
                    "device": device_hfid(port.device_name),
                    "name": port.port_name,
                    "description": cable_name,
                    "status": "active",
                    **({"ip_address": address} if address is not None else {}),
                },
                hfid=port.hfid,
                existing=True,
                ownership="template",
            )
        )
    cable_key = f"cable:{cable_name}"
    plan.add(
        NodeSpec(
            key=cable_key,
            kind=CABLE_KIND,
            data={
                "name": cable_name,
                "type": cable_type,
                "endpoints": [bottom.hfid, top.hfid],
                "deployment": {"id": deployment_id} if deployment_id else None,
                **cable_attributes(
                    cable_type, endpoint_names, positions.get(bottom.device_name), positions.get(top.device_name)
                ),
            },
            ownership="owned",
            hfid=Hfid(CABLE_KIND, (cable_name,)),
        )
    )
    return Link(bottom=bottom, top=top, cable_name=cable_name, cable_key=cable_key)


def border_service_chains(
    *,
    border_role_for: Mapping[str, str],
    connectivity_mode: Literal["pbr", "inline"],
    border_names: list[str],
    firewall_names: list[str],
    load_balancer_names: list[str],
) -> list[list[ChainHop]]:
    """The chains (each ``plan_chain_legs`` call, in order) that cable border<->firewall<->load-balancer: pbr is two
    independent legs on the services' "uplink" ports, inline one chain over "uplink" and "downlink" ports.
    """
    chains = [
        [
            ChainHop(devices=border_names, down_role=border_role_for["internal-firewall"]),
            ChainHop(devices=firewall_names, up_role="uplink"),
        ]
    ]
    if connectivity_mode == "inline":
        chains.append(
            [
                ChainHop(devices=firewall_names, down_role="downlink"),
                ChainHop(devices=load_balancer_names, up_role="uplink"),
            ]
        )
        chains.append(
            [
                ChainHop(devices=border_names, down_role=border_role_for["load-balancer"]),
                ChainHop(devices=load_balancer_names, up_role="downlink"),
            ]
        )
    else:
        chains.append(
            [
                ChainHop(devices=border_names, down_role=border_role_for["load-balancer"]),
                ChainHop(devices=load_balancer_names, up_role="uplink"),
            ]
        )
    return chains


@dataclass(frozen=True)
class ChainPort:
    """A physical port of a device a chain cables: its role and type, and the device its cable ends at (None: uncabled)."""

    name: str
    role: str | None
    interface_type: str | None
    cabled_to: str | None = None


class ChainPorts:
    """The ports of the devices of a chain, by device name: the ports of an existing device as they are cabled, a new
    device's as its template gives them (all free). A leg takes ports (``claim``), so the next leg over the same devices
    finds them cabled, as a read of the cables would after the first leg was written.
    """

    def __init__(self, ports: Mapping[str, Sequence[ChainPort]]) -> None:
        self._ports = ports
        self._cabled = {
            (device, port.name): port.cabled_to for device, device_ports in ports.items() for port in device_ports
        }

    def with_role(self, devices: Sequence[str], role: str) -> list[PortFacts]:
        """The ports of ``role`` of ``devices`` (cabled or not)."""
        return [
            PortFacts(device, port.name, port.interface_type)
            for device in devices
            for port in self._ports.get(device, ())
            if port.role == role
        ]

    def free(self, ports: Sequence[PortFacts], peers: Collection[str]) -> list[PortFacts]:
        """The ports that are uncabled or cabled to one of the devices ``peers``: a second chain on the same ports must
        not pick ports the first has cabled.
        """
        return [
            port
            for port in ports
            if (far := self._cabled.get((port.device_name, port.port_name))) is None or far in peers
        ]

    def claim(self, link: Link) -> None:
        self._cabled[(link.bottom.device_name, link.bottom.port_name)] = link.top.device_name
        self._cabled[(link.top.device_name, link.top.port_name)] = link.bottom.device_name


def chain_ports_of(
    live: Mapping[str, Sequence[ChainPort]], templates: Mapping[str, Sequence[TemplatePort]]
) -> ChainPorts:
    """The ports of a chain: the live ports of a device that exists (``read_chain_ports``), else the ports of the template
    ``templates`` names for it (a device this plan creates).
    """
    ports: dict[str, Sequence[ChainPort]] = {
        device: [ChainPort(port.name, port.role, port.interface_type) for port in template_ports]
        for device, template_ports in templates.items()
    }
    ports.update(live)
    return ChainPorts(ports)


async def read_chain_ports(client: Any, device_names: Sequence[str]) -> dict[str, list[ChainPort]]:
    """The physical ports of the devices that exist among ``device_names``, with the device each cable ends at: two reads,
    plus the far ends of the cabled ports.
    """
    if not device_names:
        return {}
    interfaces = await client.filters(
        kind=DcimPhysicalInterface, device__name__values=sorted(set(device_names)), include=["device", "cable"]
    )
    cabled = [iface for iface in interfaces if getattr(getattr(iface, "cable", None), "id", None)]
    far_ends = await far_end_interfaces(client, cabled, include=["device"])
    ports: dict[str, list[ChainPort]] = {}
    for iface in interfaces:
        far = far_ends.get(iface.id)
        cabled_to = None
        if iface in cabled:
            cabled_to = far.device.peer.name.value if far is not None else ELSEWHERE
        ports.setdefault(iface.device.peer.name.value, []).append(
            ChainPort(
                iface.name.value,
                getattr(getattr(iface, "role", None), "value", None),
                getattr(getattr(iface, "interface_type", None), "value", None),
                cabled_to,
            )
        )
    return ports


def plan_chain_legs(
    plan: TopologyPlan,
    *,
    hops: Sequence[ChainHop],
    ports: ChainPorts,
    deployment_id: str,
    positions: Mapping[str, RackPosition | None],
    problems: list[str],
    pool_id: str | NodeRef | None = None,
    p2p_prefix_length: int = 31,
) -> list[Link]:
    """Cable an ordered chain of device groups end to end, index-paired (``ChainCablingStrategy``): each hop's
    ``down_role`` ports cable to the next hop's ``up_role`` ports, over the ports an earlier leg did not take. A leg with
    no device on a side is nothing to cable; one without a port of the role on a side is a problem the caller raises with
    the others before anything is written. The links carry no P2P addresses unless ``pool_id`` (a prefix pool) is given:
    each then gets a ``p2p_prefix_length`` prefix from it.
    """
    links: list[Link] = []
    for top_hop, bottom_hop in zip(hops, hops[1:]):
        top_devices, bottom_devices = top_hop.get("devices") or [], bottom_hop.get("devices") or []
        if not top_devices or not bottom_devices:
            continue
        top_role, bottom_role = top_hop.get("down_role", ""), bottom_hop.get("up_role", "")
        top_all, bottom_all = ports.with_role(top_devices, top_role), ports.with_role(bottom_devices, bottom_role)
        top = ports.free(top_all, {port.device_name for port in bottom_all})
        bottom = ports.free(bottom_all, {port.device_name for port in top_all})
        if not top or not bottom:
            problems.append(
                f"cannot cable {sorted(top_devices)}<->{sorted(bottom_devices)} — "
                f"{top_role}_ports={len(top)}, {bottom_role}_ports={len(bottom)}."
            )
            continue
        outcome = plan_cabling(
            plan,
            bottom_ports=bottom,
            top_ports=top,
            strategy="chain",
            offset=0,
            bottom_sorting="bottom_up",
            top_sorting="bottom_up",
            deployment_id=deployment_id,
            pool_id=pool_id,
            p2p_prefix_length=p2p_prefix_length,
            positions=positions,
        )
        problems.extend(outcome.problems)
        for link in outcome.links:
            ports.claim(link)
        links.extend(outcome.links)
    return links
