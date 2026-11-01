"""add_provider_connection: the per-connection objects of a TopologyProviderConnection.

A provider connection says "this facility of ours is connected to that provider facility, over carriers (dedicated) or
the internet (VPN)". This generator turns it into what routing needs and nothing else: per link of the provider's
flavour (utils/provider_services.py, keyed by ``OrganizationProvider.connection_profile`` and the path type) ONE
TopologySessionCircuit named ``<connection>-L<n>`` with

- its two ends: dedicated, the ``<port>.<vlan>`` sub-interfaces on the near and far carrier ports (circuit type
  ``handoff``); internet_vpn, a ``Tunnel<n>`` interface pair on the near device (riding on the port of its internet carrier)
  and the provider stub (``vpn``, model only: no template renders a tunnel yet);
- ``ip_namespace`` and ``connection`` (the circuit's owner is derived from the connection, never stored),
  ``provider``, the two ``locations``, the connection's ``password`` and, for a provider-assigned flavour, the
  ``link_prefix`` paired from the connection's ``link_prefixes`` (sorted by address);
- membership of the ``virtual_circuits`` group, so ``add_virtual_circuit`` (which fires on the circuit's own created
  rule, it is NOT called from here) allocates the link addresses and builds the eBGP session.

What it does not create: the carrier circuits (authored, shared underlay: they are its INPUT, ``carrier_circuits``),
the provider stub devices (authored, shared per provider and facility; the far port's device of a dedicated link, or
the one stub of the far facility for a VPN), the autonomous systems, the key
(authored; the connection's ``password`` is put on every session circuit it creates), and the sub-interface
addresses (add_virtual_circuit).

A stub's ``Loopback0`` and ManagedBGP process are built when absent (generators/provider_stub.py, untracked: a stub
is shared and outlives a connection): the AS is the connection's ``provider_as``, the router-id the loopback, drawn
from the pool ``Provider-Stub-Loopback-IPv4``. Authored ones are reused. A missing pool, a missing ``provider_as`` or a
process of another AS refuses the run before the first write.

Flow, every failure before the first write:

1. wait for add_circuit on each authored carrier (it derives their locations), plan the links
   (generators/helpers/provider_connection.py: the pure planner raises ValueError, turned into ``self.fail``);
2. track the connection (the run's group is never empty, so the SDK cleanup runs: a changed path_type, a dropped
   link or a removed VLAN deletes the circuits and interfaces the run no longer reaches);
3. hold ``pc-<connection id>``; build the stubs that lack a process (a ``pc-device-<stub>`` lock each, released again
   before the rest); then take a lock per parent port (dedicated) or per device (VPN), taken in sorted order,
   and never a ``vc-port-*`` lock of add_virtual_circuit;
4. choose the VLAN under the port locks (the requested ``vlan_id``, else the one its sessions already ride, else the
   lowest free on every parent port; the choice is written back to ``vlan_id``, which reruns this generator once and
   changes nothing), or the tunnel names (lowest free ``Tunnel<n>`` per device);
5. ensure the interfaces and the circuits. Idempotent by edges first, name second: an interface or a circuit that is
   already what the run would write is only tracked (appended to the group, not saved), so a rerun writes nothing and
   cannot reset what add_virtual_circuit set on the interfaces (address, status).

Ownership (tracking group, delete_unused_nodes): the circuits and the interfaces they end on belong to this run. The
connection itself is tracked (never deleted), the carriers, stubs, processes and prefixes are not. Accepted leftovers
of a deleted or changed link, which only a decommission-style cleanup removes: the link addresses and the peering
add_virtual_circuit built on the old circuit, and an address left on a deleted sub-interface.

Failure convention: ``self.fail`` (logged and raised) for a spec that cannot be built; a run that fails writes
nothing and, because it raises, the SDK does not run its group cleanup.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from utils.circuit_underlay import SESSION_CIRCUIT, session_circuits_on, subinterface_vlan
from utils.data_cleaning import clean_data
from utils.limits import GROUP_VIRTUAL_CIRCUITS
from utils.provider_services import ADDRESS_SOURCE_OUR_POOL, PATH_DEDICATED

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..helpers.provider_connection import (
    ConnectionSpec,
    ExistingSession,
    LinkPlan,
    choose_tunnel_name,
    choose_vlan,
    existing_vlan,
    parse_connection,
    plan_links,
    session_is_current,
    stubs_to_build,
    tunnel_names,
)
from ..pools import PoolMixin
from ..protocols import DcimInterface, DcimVirtualInterface, TopologyProviderConnection, TopologySessionCircuit
from ..provider_stub import ProviderStubMixin
from .circuit import CARRIER_CIRCUIT_GENERATOR

_KIND = TopologyProviderConnection.__name__


class ProviderConnectionGenerator(ProviderStubMixin, CablingMixin, PoolMixin, CommonGenerator):
    """add_provider_connection — the session circuits and their end interfaces of a provider connection."""

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

        # add_circuit derives the carriers' locations; wait for it so the data planned from is what it left.
        refetched = await asyncio.gather(
            *(
                self.wait_for_parent_generator_and_refetch(CARRIER_CIRCUIT_GENERATOR, carrier["id"])
                for carrier in raw.get("carrier_circuits") or []
                if isinstance(carrier, dict) and carrier.get("id")
            )
        )
        refreshed = next((result for result in reversed(refetched) if result), None)
        if refreshed is not None:
            raw = next(iter(clean_data(refreshed).get(_KIND) or []), raw)

        try:
            spec = parse_connection(raw)
            flavour, links = plan_links(spec)
        except ValueError as exc:
            self.fail(f"{name}: {exc}")
        self.logger.info(f"Processing provider connection {name} ({spec.path_type}, {len(links)} link(s))")
        if flavour.address_source == ADDRESS_SOURCE_OUR_POOL and spec.link_prefixes:
            self.logger.warning(f"{name}: link_prefixes are ignored, this flavour allocates from our pool")

        # Tracked first and only once the spec is valid: the SDK cleans nothing when a run tracked nothing, and
        # a refused run raises before this line.
        self.client.group_context.related_node_ids.append(spec.id)
        by_name = {session.name: session for session in spec.sessions}
        async with self.resource_lock(f"pc-{spec.id}"):
            await self.ensure_provider_stubs(
                spec.name, [(side.device_id, side.device_name) for side in stubs_to_build(links)], spec.provider_as_id
            )
            if spec.path_type == PATH_DEDICATED:
                await self._build_dedicated(spec, links, by_name)
            else:
                await self._build_vpn(spec, links, by_name)
        self.logger.info(f"Provider connection {name} - completed")

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
                    if side.port is not None:
                        tunnel_data["parent_interface"] = {"id": side.port.id}
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
