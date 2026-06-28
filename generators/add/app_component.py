"""Generator: wire AppComponent nodes to their load-balancer and compute infrastructure.

Triggered per AppComponent (target group: app_components).

Steps:
  1. If cluster_capabilities are set, assign the component's network_segment to
     the uplink interfaces of every physical host in those clusters' node pools.
     (This is the piece the segment generator can't do because segments don't
     reference clusters — only AppComponent does via cluster_capabilities.)
  2. If vip_service is set:
     a. Add the VIP to interface_capabilities on interface '1.1' of every physical
        LB device that is a member of the ManagedLoadbalancerHA cluster.
     b. Create a LoadbalancerPoolMember per VM in component.capabilities.
     c. Create a LoadbalancerPoolInterface per VM, linked to the PoolMember.
     d. Assign the PoolInterface to the VM's first active interface.

Idempotency: existing PoolMember names are detected and re-registered with
the Infrahub tracker so they are not deleted on re-runs.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import DcimPhysicalInterface, DcimVirtualInterface


class AppComponentGenerator(CommonGenerator):
    """Wire one AppComponent to its network segment (via cluster hosts) and LB backend pool.

    Triggered per AppComponent node — query: app_component_data.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        comp_list = cleaned.get("AppComponent", [])
        if not comp_list:
            self.logger.error("No AppComponent data in GraphQL response")
            return

        comp = comp_list[0]
        comp_slug: str = comp.get("slug") or comp.get("name", "")

        self.logger.info("Processing AppComponent: %s", comp_slug)

        # ── 1. Assign segment to physical host uplinks ────────────────────
        # Two sources of physical hosts:
        #   a) cluster_capabilities → VirtCluster → node_pools → physical_hosts
        #   b) capabilities (explicit VMs) → hosting_device (the hypervisor)
        network_segment = comp.get("network_segment") or {}
        segment_id: str = network_segment.get("id", "")
        segment_name: str = network_segment.get("name", "")
        cluster_capabilities = comp.get("cluster_capabilities") or []
        capabilities = comp.get("capabilities") or []

        # Collect host IDs from both sources
        host_ids: set[str] = set()

        for cluster in cluster_capabilities:
            for pool in cluster.get("node_pools") or []:
                for host in pool.get("physical_hosts") or []:
                    if host.get("id"):
                        host_ids.add(host["id"])

        for vm_stub in capabilities:
            hosting = vm_stub.get("hosting_device") or {}
            if hosting.get("id"):
                host_ids.add(hosting["id"])

        segment_typename: str = network_segment.get("typename", "")
        is_cloud_segment = segment_typename == "CloudNetworkSegment"

        if segment_id and host_ids and not is_cloud_segment:
            await self._assign_segment_to_hosts(
                segment_id=segment_id,
                segment_name=segment_name,
                segment_kind=segment_typename or "ManagedVxlanSegment",
                host_ids=host_ids,
            )
        elif is_cloud_segment:
            self.logger.debug(
                "  %s uses a cloud segment — host uplink assignment not applicable",
                comp_slug,
            )
        elif host_ids and not segment_id:
            self.logger.warning(
                "  %s has cluster/VM capabilities but no network_segment — skipping host uplink assignment",
                comp_slug,
            )

        # ── 2. VIP + pool member wiring ───────────────────────────────────
        vip_service = comp.get("vip_service") or {}
        vip_id: str = vip_service.get("id", "")

        if not vip_id:
            self.logger.info("  %s has no vip_service — skipping LB wiring", comp_slug)
            return

        try:
            vip_obj = await self.client.get(
                kind="LoadbalancerVIP",
                id=vip_id,
                prefetch_relationships=True,
            )
        except Exception as exc:
            self.logger.warning("  Could not fetch VIP %s: %s", vip_id, exc)
            return
        if not vip_obj:
            self.logger.warning("  VIP %s not found — skipping", vip_id)
            return

        await vip_obj.resolve()
        vip_hostname: str = getattr(getattr(vip_obj, "hostname", None), "value", vip_id)
        vip_proto: str = getattr(getattr(vip_obj, "protocol", None), "value", "")
        vip_port: str = str(getattr(getattr(vip_obj, "port", None), "value", ""))

        await self._assign_vip_to_lb(vip_obj, vip_id, vip_hostname)

        backend_port: int | None = comp.get("backend_port")
        for vm_stub in comp.get("capabilities") or []:
            vm_id: str = vm_stub.get("id", "")
            vm_name: str = vm_stub.get("name", vm_id)
            if not vm_id:
                continue
            await self._wire_pool_member(
                member_name=f"{comp_slug}-{vm_name}",
                vm_id=vm_id,
                vm_name=vm_name,
                vip_id=vip_id,
                vip_hostname=vip_hostname,
                vip_proto=vip_proto,
                vip_port=vip_port,
                backend_port=backend_port,
            )

    async def _assign_segment_to_hosts(
        self,
        segment_id: str,
        segment_name: str,
        segment_kind: str,
        host_ids: set[str],
    ) -> None:
        """Assign network_segment to the uplink interfaces of the given physical hosts.

        Called with hosts collected from two sources:
          - cluster_capabilities → node_pools → physical_hosts
          - capabilities (VMs) → hosting_device

        segment_kind is the concrete __typename (ManagedVxlanSegment or ManagedVlanSegment)
        so the SDK can fetch the right node type for interface_capabilities assignment.
        """
        self.logger.info(
            "  Segment '%s' (%s) — assigning to uplink interfaces on %d host(s)",
            segment_name,
            segment_kind,
            len(host_ids),
        )

        try:
            segment_obj = await self.client.get(kind=segment_kind, id=segment_id)
        except Exception as exc:
            self.logger.warning("  Could not fetch segment SDK object for '%s': %s", segment_name, exc)
            return
        if not segment_obj:
            return

        try:
            interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__ids=list(host_ids),
                role__value="uplink",
                status__value="active",
            )
        except Exception as exc:
            self.logger.warning("  Could not query uplink interfaces for hosts: %s", exc)
            return

        assigned = 0
        for iface in interfaces:
            iface_caps = getattr(iface, "interface_capabilities")
            await iface_caps.fetch()
            existing_ids = {peer.id for peer in iface_caps.peers}
            if segment_id not in existing_ids:
                await iface_caps.add(segment_obj)
                assigned += 1
            await iface.save(allow_upsert=True)

        self.logger.info(
            "  Assigned segment '%s' to %d uplink interface(s) (%d already assigned)",
            segment_name,
            assigned,
            len(interfaces) - assigned,
        )

    async def _assign_vip_to_lb(
        self,
        vip_obj: Any,
        vip_id: str,
        vip_hostname: str,
    ) -> None:
        """Add vip_obj to interface_capabilities on the '1.1' interface of every
        physical device that is a member of the VIP's ManagedLoadbalancerHA cluster."""
        lb_rel = getattr(vip_obj, "load_balancer", None)
        lb_peer = getattr(lb_rel, "peer", None) if lb_rel else None
        if not lb_peer or not getattr(lb_peer, "id", None):
            return

        try:
            lb_ha = await self.client.get(
                kind="ManagedLoadbalancerHA",
                id=lb_peer.id,
                prefetch_relationships=True,
            )
        except Exception as exc:
            self.logger.warning("  Could not fetch ManagedLoadbalancerHA %s: %s", lb_peer.id, exc)
            return
        if not lb_ha:
            return

        try:
            await lb_ha.capabilities.fetch()
            lb_devices = list(lb_ha.capabilities.peers)
        except Exception as exc:
            self.logger.warning("  Could not fetch LB HA capabilities: %s", exc)
            return

        for lb_dev in lb_devices:
            lb_dev_name = getattr(getattr(lb_dev, "name", None), "value", lb_dev.id)
            try:
                ingress_ifaces = await self.client.filters(
                    kind=DcimPhysicalInterface,
                    device__ids=[lb_dev.id],
                    name__value="1.1",
                )
            except Exception as exc:
                self.logger.warning("  Could not query ingress interface on %s: %s", lb_dev_name, exc)
                continue

            for iface in ingress_ifaces:
                try:
                    iface_caps = getattr(iface, "interface_capabilities")
                    await iface_caps.fetch()
                    existing_ids = {peer.id for peer in iface_caps.peers}
                    if vip_id not in existing_ids:
                        await iface_caps.add(vip_obj)
                        self.logger.info("  Assigned VIP %s to %s:1.1", vip_hostname, lb_dev_name)
                    await iface.save(allow_upsert=True)
                except Exception as exc:
                    self.logger.warning("  Failed to assign VIP to %s:1.1: %s", lb_dev_name, exc)

    async def _wire_pool_member(
        self,
        member_name: str,
        vm_id: str,
        vm_name: str,
        vip_id: str,
        vip_hostname: str,
        vip_proto: str,
        vip_port: str,
        backend_port: int | None,
    ) -> None:
        """Create (or re-register) one LoadbalancerPoolMember + PoolInterface for a VM."""
        try:
            existing_members = await self.client.filters(
                kind="LoadbalancerPoolMember",
                name__value=member_name,
            )
        except Exception as exc:
            self.logger.warning("  Could not check existing PoolMember '%s': %s", member_name, exc)
            existing_members = []

        if existing_members:
            try:
                await existing_members[0].save(allow_upsert=True)
            except Exception:
                pass
            self.logger.info("  PoolMember %s already exists — re-registered", member_name)
            return

        try:
            vm_full = await self.client.get(
                kind="DcimVirtualDevice",
                id=vm_id,
                prefetch_relationships=True,
            )
        except Exception as exc:
            self.logger.warning("  Could not fetch DcimVirtualDevice %s: %s", vm_id, exc)
            vm_full = None

        ip_id: str | None = None
        if vm_full:
            primary_addr_rel = getattr(vm_full, "primary_address", None)
            primary_addr_peer = getattr(primary_addr_rel, "peer", None) if primary_addr_rel else None
            ip_id = getattr(primary_addr_peer, "id", None) if primary_addr_peer else None

        try:
            pool_member = await self.client.create(
                kind="LoadbalancerPoolMember",
                data={
                    "name": member_name,
                    "status": "active",
                    "vip_service": {"id": vip_id},
                    "weight": 1,
                },
            )
            await pool_member.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error("  Failed to create PoolMember '%s': %s", member_name, exc)
            return

        if vm_full:
            try:
                vm_caps = getattr(vm_full, "capabilities")
                await vm_caps.fetch()
                existing_cap_ids = {peer.id for peer in vm_caps.peers}
                if pool_member.id not in existing_cap_ids:
                    await vm_caps.add(pool_member)
                    await vm_full.save(allow_upsert=True)
            except Exception as exc:
                self.logger.warning("  Could not link PoolMember to VM %s capabilities: %s", vm_name, exc)

        pi_name = f"{member_name}-iface"
        pool_iface_data: dict[str, Any] = {
            "name": pi_name,
            "status": "active",
            "pool_member": {"id": pool_member.id},
        }
        if backend_port is not None:
            pool_iface_data["port"] = backend_port
        if ip_id:
            pool_iface_data["ip_address"] = {"id": ip_id}

        try:
            pool_iface = await self.client.create(
                kind="LoadbalancerPoolInterface",
                data=pool_iface_data,
            )
            await pool_iface.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error("  Failed to create PoolInterface '%s': %s", pi_name, exc)
            return

        if vm_full:
            target_iface = None
            try:
                vm_ifaces = await self.client.filters(
                    kind=DcimVirtualInterface,
                    device__ids=[vm_full.id],
                    status__value="active",
                )
                if not vm_ifaces:
                    vm_ifaces = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__ids=[vm_full.id],
                        status__value="active",
                    )
                if vm_ifaces:
                    target_iface = vm_ifaces[0]
            except Exception as exc:
                self.logger.warning("  Could not query interfaces for VM %s: %s", vm_name, exc)

            if target_iface:
                try:
                    pi_caps = getattr(target_iface, "interface_capabilities")
                    await pi_caps.fetch()
                    existing_pi_ids = {peer.id for peer in pi_caps.peers}
                    if pool_iface.id not in existing_pi_ids:
                        await pi_caps.add(pool_iface)
                        await target_iface.save(allow_upsert=True)
                except Exception as exc:
                    self.logger.warning("  Could not link PoolInterface to %s interface: %s", vm_name, exc)

        self.logger.info(
            "  Wired %s → %s:%s:%s",
            member_name,
            vip_hostname,
            vip_proto,
            vip_port,
        )
