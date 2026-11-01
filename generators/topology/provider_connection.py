"""add_provider_connection: realize a TopologyProviderConnection, a service request, as carrier, interfaces and sessions.

A provider connection says "connect this facility of ours to that provider facility, over a carrier (dedicated) or the
internet (VPN)". It names facilities, never ports or circuits. This generator chooses and creates what the request
leaves open (the CHOICE is generators/helpers/near_port_facts.py over the pure generators/helpers/near_port.py):

- dedicated: the near port (the explicit ``near_port``, else the near end of the connection's own carrier, else a free
  port of the right interface role on the least loaded device of the near facility), the far end (a ``vnic-<name>``
  virtual NIC on the service-edge appliance of a cloud or SaaS region, a free port of a physical provider device) and
  the carrier circuit between them. A carrier that already ends on the near port and reaches the far facility is REUSED
  (found by the end edge, never by a name or a list); otherwise one is created: status ``provisioning``, ``circuit_id``
  and name ``<connection>-XC`` (a placeholder the human replaces with the provider's id), the type by the far kind, the
  connection's provider and bandwidth, in the ``carrier_circuits`` group, ``locations`` left empty (add_circuit derives
  them when the carrier is created). A far facility we order outside the model (a virtual colocation) gets no carrier:
  it must exist on the explicit ``near_port``. The connection's ``carrier_circuits`` is written with the carrier it rides
  (only when it differs): an output, nobody authors it.
- internet_vpn: no carrier. The near device and port are the end of the near facility's internet carrier named by
  ``near_port`` (the one device that ends one when it is unset); ``near_port`` is not a dedicated port choice here and
  the connection's ``carrier_circuits`` is cleared.

Then, per link of the provider's flavour (utils/provider_services.py, keyed by ``OrganizationProvider.connection_profile``
and the path type) ONE TopologySessionCircuit named ``<connection>-L<n>`` with

- its two ends: dedicated, the ``<port>.<vlan>`` sub-interfaces on the near and far carrier ports (circuit type
  ``handoff``); internet_vpn, a ``Tunnel<n>`` interface pair on the near device (riding on the port of its internet carrier)
  and the provider stub (``vpn``, model only: no template renders a tunnel yet);
- ``ip_namespace`` and ``connection`` (the circuit's owner is derived from the connection, never stored),
  ``provider``, the two ``locations``, the connection's ``password`` and, for a provider-assigned flavour, the
  ``link_prefix`` paired from the connection's ``link_prefixes`` (sorted by address);
- membership of the ``virtual_circuits`` group, so ``add_virtual_circuit`` (which fires on the circuit's own created
  rule, it is NOT called from here) allocates the link addresses and builds the eBGP session.

What it does not create: the provider stub devices (authored, shared per provider and facility), the autonomous systems,
the key (authored; the connection's ``password`` is put on every session circuit it creates), and the sub-interface
addresses (add_virtual_circuit). A stub's ``Loopback0`` and ManagedBGP process are built when absent
(generators/provider_stub.py, untracked: a stub is shared and outlives a connection): the AS is the connection's
``provider_as``, the router-id the loopback, drawn from the pool ``Provider-Stub-Loopback-IPv4``. Authored ones are
reused. A missing pool, a missing ``provider_as`` or a process of another AS refuses the run before the first write.

Flow, every failure before the first write:

1. the locks: ``pc-near-<near facility id>`` (dedicated only: the choice of a free port and the creation of the carrier
   that claims it, so two connections cannot take the same port), then ``pc-<connection id>``; then the stubs'
   ``pc-device-<id>`` and the parent port locks (taken in sorted order). Nothing waits for ``pc-near`` while holding a
   port lock, and no ``vc-port-*`` lock of add_virtual_circuit is taken;
2. read the facts and decide the ends (the near-port choice raises ValueError, turned into ``self.fail``), plan the links
   (generators/helpers/provider_connection.py), track the connection (the run's group is never empty, so the SDK cleanup
   runs: a changed path_type, a dropped link or a removed VLAN deletes the circuits and interfaces the run no longer
   reaches);
3. build the stubs that lack a process; create the far NIC and the carrier when they do not exist, write
   ``carrier_circuits``;
4. choose the VLAN under the port locks (the requested ``vlan_id``, else the one its sessions already ride, else the
   lowest free on every parent port; the choice is written back to ``vlan_id``, which reruns this generator once and
   changes nothing), or the tunnel names (lowest free ``Tunnel<n>`` per device);
5. ensure the interfaces and the circuits. Idempotent by edges first, name second: an interface or a circuit that is
   already what the run would write is only tracked (appended to the group, not saved), so a rerun writes nothing and
   cannot reset what add_virtual_circuit set on the interfaces (address, status). A rerun finds the connection's own
   carrier and keeps its near port: it never selects again.

Ownership (tracking group, delete_unused_nodes): the session circuits and the sub-interfaces / tunnels they end on belong
to this run. The connection itself is tracked (never deleted); the carriers, the far NIC (shared underlay, like the
carriers that end on it), the stubs, processes and prefixes are not. Accepted leftovers of a deleted or changed link,
which only a decommission-style cleanup removes: the link addresses and the peering add_virtual_circuit built on the old
circuit, an address left on a deleted sub-interface, the carrier and the NIC of a connection that moved to another port
or to the internet.

Failure convention: ``self.fail`` (logged and raised) for a spec that cannot be built; a run that fails writes
nothing and, because it raises, the SDK does not run its group cleanup.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from utils.circuit_underlay import SESSION_CIRCUIT, session_circuits_on, subinterface_vlan
from utils.data_cleaning import clean_data
from utils.limits import GROUP_CARRIER_CIRCUITS, GROUP_VIRTUAL_CIRCUITS
from utils.provider_services import ADDRESS_SOURCE_OUR_POOL, PATH_DEDICATED

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..helpers.near_port_facts import NearPortFactsMixin, Resolution
from ..helpers.provider_connection import (
    ConnectionSpec,
    ExistingSession,
    LinkPlan,
    bind_far_port,
    choose_tunnel_name,
    choose_tunnel_parent,
    choose_vlan,
    existing_vlan,
    nic_name,
    parse_connection,
    plan_links,
    port_of,
    session_is_current,
    stubs_to_build,
    tunnel_names,
    validate_spec,
)
from ..pools import PoolMixin
from ..protocols import (
    DcimInterface,
    DcimVirtualInterface,
    TopologyCarrierCircuit,
    TopologyProviderConnection,
    TopologySessionCircuit,
)
from ..provider_stub import ProviderStubMixin

_KIND = TopologyProviderConnection.__name__


class ProviderConnectionGenerator(ProviderStubMixin, NearPortFactsMixin, CablingMixin, PoolMixin, CommonGenerator):
    """add_provider_connection — the carrier, the session circuits and their end interfaces of a provider connection."""

    client: Any

    async def generate(self, data: dict[str, Any]) -> None:
        connections = clean_data(data).get(_KIND) or []
        if not connections:
            self.logger.error(f"No {_KIND} data in GraphQL response")
            return
        raw = connections[0]
        name: str = raw.get("name") or ""
        if not name:
            self.logger.error(f"{_KIND} missing name")
            return

        try:
            spec = parse_connection(raw)
            flavour, _ = validate_spec(spec)
        except ValueError as exc:
            self.fail(f"{name}: {exc}")
        dedicated = spec.path_type == PATH_DEDICATED
        if flavour.address_source == ADDRESS_SOURCE_OUR_POOL and spec.link_prefixes:
            self.logger.warning(f"{name}: link_prefixes are ignored, this flavour allocates from our pool")

        async with AsyncExitStack() as locks:
            if dedicated:
                await locks.enter_async_context(self.resource_lock(f"pc-near-{spec.near_id}"))
            await locks.enter_async_context(self.resource_lock(f"pc-{spec.id}"))
            resolution: Resolution | None = None
            try:
                if dedicated:
                    resolution = await self.resolve_dedicated_ends(spec)
                _, links = plan_links(spec, resolution.ends if resolution else None)
            except ValueError as exc:
                self.fail(f"{name}: {exc}")
            self.logger.info(f"Processing provider connection {name} ({spec.path_type}, {len(links)} link(s))")

            # Tracked first and only once the spec is valid: the SDK cleans nothing when a run tracked nothing, and
            # a refused run raises before this line.
            self.client.group_context.related_node_ids.append(spec.id)
            by_name = {session.name: session for session in spec.sessions}
            await self.ensure_provider_stubs(
                spec.name, [(side.device_id, side.device_name) for side in stubs_to_build(links)], spec.provider_as_id
            )
            if resolution is not None:
                links = await self._ensure_carrier(spec, resolution, links)
                await self._build_dedicated(spec, links, by_name)
            else:
                await self._write_carrier_list(spec, [])
                await self._build_vpn(spec, links, by_name)
        self.logger.info(f"Provider connection {name} - completed")

    # -- the carrier of a dedicated connection ----------------------------------------------------------------------

    async def _ensure_carrier(
        self, spec: ConnectionSpec, resolution: Resolution, links: list[LinkPlan]
    ) -> list[LinkPlan]:
        """Create the far NIC and the carrier circuit when they do not exist yet; record the carrier on the connection.

        Both are untracked: shared underlay that outlives the connection's sessions. The carrier is created with its two
        ends in one save and with the connection already listing it (the claim on the near port is atomic: a failure
        between two writes cannot leave a carrier nothing owns).
        """
        near, far = resolution.ends.near, resolution.ends.far
        if far is None:
            stub = resolution.ends.far_device
            node = await self.client.create(
                kind=DcimVirtualInterface,
                data={
                    "name": nic_name(spec.name),
                    "description": f"{spec.name} service-edge NIC",
                    "device": {"id": stub.id},
                    "status": "active",
                    "role": "uplink",
                },
            )
            await node.save(allow_upsert=True, update_group_context=False)
            far = port_of(spec, node.id, nic_name(spec.name), stub.id, stub.name)
            links = bind_far_port(links, far)
            self.logger.info(f"  {stub.name}: NIC {far.name}")
        carrier_id = resolution.carrier_id
        created = None
        if carrier_id is None:
            data: dict[str, Any] = {
                "name": f"{spec.name}-XC",
                "circuit_id": f"{spec.name}-XC",
                "circuit_type": resolution.carrier_type,
                "status": "provisioning",
                "description": f"{spec.name}: {near.label} to {far.label}; replace circuit_id with the provider's",
                "provider": {"id": spec.provider_id},
                # Mandatory in the schema (accepted empty): add_circuit derives the facilities of the ends.
                "locations": [],
                "capabilities": [{"id": near.id}, {"id": far.id}],
                "provider_connections": [{"id": spec.id}],
                "member_of_groups": [{"hfid": [GROUP_CARRIER_CIRCUITS], "kind": "CoreStandardGroup"}],
            }
            if spec.bandwidth:
                data["bandwidth"] = spec.bandwidth
            carrier = await self.client.create(kind=TopologyCarrierCircuit, data=data)
            await carrier.save(allow_upsert=True, update_group_context=False)
            carrier_id = created = carrier.id
            self.logger.info(f"  created carrier circuit {spec.name}-XC ({near.label} - {far.label})")
        await self._write_carrier_list(spec, [carrier_id], created=created)
        return links

    async def _write_carrier_list(self, spec: ConnectionSpec, wanted: list[str], created: str | None = None) -> None:
        """Make ``carrier_circuits`` the wanted list, only when it differs (a new carrier already lists itself)."""
        listed = {carrier.id for carrier in spec.carriers} | ({created} if created else set())
        if listed == set(wanted):
            return
        node = await self.client.create(
            kind=TopologyProviderConnection,
            data={"id": spec.id, "carrier_circuits": [{"id": carrier_id} for carrier_id in wanted]},
        )
        await node.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  {spec.name}: carrier_circuits set ({len(wanted)})")

    # -- dedicated: sub-interfaces on the carrier ports -----------------------------------------------------------

    async def _build_dedicated(
        self, spec: ConnectionSpec, links: list[LinkPlan], by_name: dict[str, ExistingSession]
    ) -> None:
        """One ``<port>.<vlan>`` pair and one handoff circuit per link, under a lock per parent port."""
        ports = [side.port for link in links for side in (link.near, link.far) if side.port is not None]
        port_ids = sorted({port.id for port in ports})
        async with AsyncExitStack() as stack:
            for port_id in port_ids:
                await stack.enter_async_context(self.resource_lock(f"pc-port-{port_id}"))
            nodes = {node.id: node for node in await self.client.filters(kind=DcimInterface, ids=port_ids)}
            missing = [port.label for port in ports if port.id not in nodes]
            if missing:
                self.fail(f"{spec.name}: carrier port(s) {', '.join(missing)} not found")
            subs = await self.client.filters(kind=DcimVirtualInterface, parent_interface__ids=port_ids)
            held = {(getattr(sub, "parent_interface").id, sub.name.value): sub.id for sub in subs}
            used = [vlan for sub in subs if (vlan := subinterface_vlan(sub.name.value)) is not None]
            reuse = next(
                (v for link in links if (v := existing_vlan(by_name.get(link.name), link.near.device_id))), None
            )
            try:
                vlan = choose_vlan(spec.vlan_id, reuse, used)
            except ValueError as exc:
                self.fail(f"{spec.name}: {exc}")

            # Reusing an interface that is not an end of this connection's own circuit would share a port with
            # another session ("one port, one session" of add_virtual_circuit): refused before the first write.
            own_ends = {end.id for session in by_name.values() for end in session.ends}
            foreign = [
                held[(port.id, f"{port.name}.{vlan}")]
                for port in ports
                if (port.id, f"{port.name}.{vlan}") in held and held[(port.id, f"{port.name}.{vlan}")] not in own_ends
            ]
            if foreign:
                others = {c.name.value for c in await session_circuits_on(self.client, foreign)}
                if others - set(by_name):
                    self.fail(
                        f"{spec.name}: VLAN {vlan} is already an end of {', '.join(sorted(others - set(by_name)))} "
                        "on this port; set a free vlan_id or leave it empty"
                    )

            for link in links:
                ends = []
                for side in (link.near, link.far):
                    assert side.port is not None
                    sub_name = f"{side.port.name}.{vlan}"
                    existing_id = held.get((side.port.id, sub_name))
                    if existing_id:
                        self.client.group_context.related_node_ids.append(existing_id)
                        ends.append(existing_id)
                        continue
                    sub = await self.ensure_vlan_subinterface(
                        device_id=side.device_id,
                        device_name=side.device_name,
                        trunk_iface=nodes[side.port.id],
                        vlan_id_value=vlan,
                    )
                    if sub is None:
                        self.fail(f"{spec.name}: could not create {side.device_name} {sub_name}")
                    ends.append(sub.id)
                await self._ensure_circuit(spec, link, by_name.get(link.name), ends)
        if spec.vlan_id != vlan:
            await self._write_back_vlan(spec, vlan)

    async def _write_back_vlan(self, spec: ConnectionSpec, vlan: int) -> None:
        """Record the chosen VLAN on the connection (untracked: the connection is the run's target, not its output).

        The attribute rule of the connection reruns this generator once; the rerun finds the VLAN set and writes
        nothing.
        """
        node = await self.client.create(kind=TopologyProviderConnection, data={"id": spec.id, "vlan_id": vlan})
        await node.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  {spec.name}: vlan_id set to {vlan}")

    # -- internet VPN: a tunnel interface pair per link -----------------------------------------------------------

    async def _build_vpn(
        self, spec: ConnectionSpec, links: list[LinkPlan], by_name: dict[str, ExistingSession]
    ) -> None:
        """One ``Tunnel<n>`` on the near device and on the stub per link, and one vpn circuit, under a device lock."""
        device_ids = sorted({side.device_id for link in links for side in (link.near, link.far)})
        async with AsyncExitStack() as stack:
            for device_id in device_ids:
                await stack.enter_async_context(self.resource_lock(f"pc-device-{device_id}"))
            taken: dict[str, set[str]] = {device_id: set() for device_id in device_ids}
            for device_id in device_ids:
                found = await self.client.filters(
                    kind=DcimVirtualInterface, device__ids=[device_id], name__values=tunnel_names()
                )
                taken[device_id].update(node.name.value for node in found)
            # A provider appliance with a NIC hangs its tunnels on it (the first NIC by name), like the near side
            # rides on the port of its carrier; an appliance without one keeps a free-standing tunnel.
            far_ids = sorted({link.far.device_id for link in links})
            nics: dict[str, dict[str, str]] = {device_id: {} for device_id in far_ids}
            for node in await self.client.filters(kind=DcimVirtualInterface, device__ids=far_ids, include=["device"]):
                nics[getattr(node, "device").id][node.name.value] = node.id
            tunnel_parent: dict[str, str | None] = {}
            for device_id, by_nic_name in nics.items():
                chosen = choose_tunnel_parent(by_nic_name)
                tunnel_parent[device_id] = by_nic_name[chosen] if chosen else None
            for link in links:
                session = by_name.get(link.name)
                ends = []
                for side in (link.near, link.far):
                    kept = next(
                        (
                            end
                            for end in (session.ends if session else ())
                            if end.device_id == side.device_id and end.name in taken[side.device_id]
                        ),
                        None,
                    )
                    if kept is not None:
                        self.client.group_context.related_node_ids.append(kept.id)
                        ends.append(kept.id)
                        continue
                    try:
                        tunnel = choose_tunnel_name(taken[side.device_id])
                    except ValueError as exc:
                        self.fail(f"{spec.name}: {side.device_name}: {exc}")
                    taken[side.device_id].add(tunnel)
                    tunnel_data: dict[str, Any] = {
                        "name": tunnel,
                        "device": {"id": side.device_id},
                        "status": "active",
                        "role": "service",
                        "description": f"{link.name} tunnel",
                    }
                    parent_id = side.port.id if side.port is not None else tunnel_parent.get(side.device_id)
                    if parent_id:
                        tunnel_data["parent_interface"] = {"id": parent_id}
                    node = await self.client.create(kind=DcimVirtualInterface, data=tunnel_data)
                    await node.save(allow_upsert=True)
                    ends.append(node.id)
                await self._ensure_circuit(spec, link, session, ends)

    # -- the session circuit --------------------------------------------------------------------------------------

    async def _ensure_circuit(
        self, spec: ConnectionSpec, link: LinkPlan, existing: ExistingSession | None, end_ids: list[str]
    ) -> None:
        """Save the link's session circuit with both ends in ONE save, or only track it when it is already right."""
        assert spec.provider_id and spec.namespace_id and spec.near_id and spec.far_id
        locations = list(dict.fromkeys([spec.near_id, spec.far_id]))
        desired = {
            "circuit_type": link.circuit_type,
            "status": spec.status,
            "provider_id": spec.provider_id,
            "namespace_id": spec.namespace_id,
            "connection_id": spec.id,
            "link_prefix_id": link.link_prefix_id,
            "password_id": spec.password_id,
            "location_ids": frozenset(locations),
        }
        if session_is_current(existing, desired=desired, end_ids=frozenset(end_ids), group=GROUP_VIRTUAL_CIRCUITS):
            assert existing is not None
            self.client.group_context.related_node_ids.append(existing.id)
            return
        data: dict[str, Any] = {
            "name": link.name,
            "circuit_type": link.circuit_type,
            "status": spec.status,
            "provider": {"id": spec.provider_id},
            "description": f"{spec.name} link {link.index}",
            "ip_namespace": {"id": spec.namespace_id},
            "locations": [{"id": location} for location in locations],
            "capabilities": [{"id": end_id} for end_id in end_ids],
            "connection": {"id": spec.id},
            "member_of_groups": [{"hfid": [GROUP_VIRTUAL_CIRCUITS], "kind": "CoreStandardGroup"}],
        }
        if link.link_prefix_id:
            data["link_prefix"] = {"id": link.link_prefix_id}
        if spec.password_id:
            data["password"] = {"id": spec.password_id}
        if existing is not None:
            data["id"] = existing.id
        circuit = await self.client.create(kind=TopologySessionCircuit, data=data)
        await circuit.save(allow_upsert=True)
        self.logger.info(f"  ensured {SESSION_CIRCUIT} {link.name}")
