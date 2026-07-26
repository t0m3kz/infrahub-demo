"""AppComponent generator that wires segments, VIPs, and pool members."""

from __future__ import annotations

import re
from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.ports import PortsPlanner
from ..protocols import (
    AppComponent,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    LoadbalancerHealthCheck,
    LoadbalancerPoolInterface,
    LoadbalancerPoolMember,
    LoadbalancerVIP,
    ManagedLoadbalancerHA,
)


class AppComponentGenerator(CommonGenerator):
    """Wire one AppComponent to host uplinks and LB backend artifacts."""

    @staticmethod
    def _collect_host_ids(instances: list[dict[str, Any]]) -> set[str]:
        """Return unique physical host IDs behind component instances."""
        host_ids: set[str] = set()
        for inst in instances:
            typename = inst.get("typename", "")
            if typename == "DcimVirtualDevice":
                hosting = inst.get("hosting_device") or {}
                if hosting.get("id"):
                    host_ids.add(hosting["id"])
            elif typename == "DcimPhysicalDevice" and inst.get("id"):
                host_ids.add(inst["id"])
        return host_ids

    @staticmethod
    def _is_cloud_segment(network_segment: dict[str, Any]) -> bool:
        """Return True when component segment is cloud-based."""
        return network_segment.get("typename", "") == "CloudNetworkSegment"

    @staticmethod
    def _backend_port(service_ports: list[dict[str, Any]]) -> int | None:
        """Return first declared backend port if present."""
        if not service_ports:
            return None
        return service_ports[0].get("port")

    @staticmethod
    def _select_primary_service_port(service_ports: list[dict[str, Any]]) -> tuple[int, str] | None:
        """Select a deterministic primary service port for VIP auto-creation."""
        if not service_ports:
            return None

        protocol_rank = {
            "https": 0,
            "http": 1,
            "tls": 2,
            "tcp": 3,
            "udp": 4,
            "tcp_udp": 5,
        }

        candidates: list[tuple[int, int, str]] = []
        for sp in service_ports:
            port_val = sp.get("port")
            if port_val is None:
                continue
            try:
                port_int = int(port_val)
            except (TypeError, ValueError):
                continue

            proto = str(sp.get("protocol") or "tcp").lower()
            rank = protocol_rank.get(proto, 99)
            candidates.append((rank, port_int, proto))

        if not candidates:
            return None

        _rank, selected_port, selected_proto = sorted(candidates, key=lambda x: (x[0], x[1]))[0]
        return (selected_port, selected_proto)

    @staticmethod
    def _sanitize_dns_label(value: str) -> str:
        """Sanitize arbitrary text into a DNS label."""
        normalized = re.sub(r"[^a-z0-9-]", "-", value.strip().lower())
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        return normalized or "component"

    @classmethod
    def _derive_vip_hostname(
        cls,
        app_fqdn: str,
        component_name: str,
        component_type: str,
        component_slug: str,
    ) -> str:
        """Derive deterministic VIP hostname from app/context data."""
        safe_component = cls._sanitize_dns_label(component_name or component_slug)
        safe_slug = cls._sanitize_dns_label(component_slug or component_name)

        if app_fqdn:
            if component_type == "frontend":
                return app_fqdn
            return f"{safe_component}.{app_fqdn}"

        return f"{safe_slug}.internal"

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        comp_list = cleaned.get("AppComponent", [])
        if not comp_list:
            self.logger.error("No AppComponent data in GraphQL response")
            return

        comp = comp_list[0]
        comp_slug: str = comp.get("slug") or comp.get("name", "")

        self.logger.info("Processing AppComponent: %s", comp_slug)

        instances: list[dict[str, Any]] = comp.get("instances") or []

        network_segment = comp.get("network_segment") or {}
        segment_id: str = network_segment.get("id", "")
        segment_name: str = network_segment.get("name", "")
        segment_typename: str = network_segment.get("typename", "")
        is_cloud_segment = self._is_cloud_segment(network_segment)

        host_ids = self._collect_host_ids(instances)

        if segment_id and host_ids and not is_cloud_segment:
            await self._assign_segment_to_hosts(
                segment_id=segment_id,
                segment_name=segment_name,
                segment_kind=segment_typename or "ManagedVxlanSegment",
                host_ids=host_ids,
            )
        elif is_cloud_segment:
            self.logger.debug("  %s uses a cloud segment - host uplink assignment not applicable", comp_slug)
        elif host_ids and not segment_id:
            self.logger.warning(
                "  %s has instances but no network_segment - skipping host uplink assignment",
                comp_slug,
            )

        lb_ha_id = await self._resolve_component_lb_ha_id(comp=comp, comp_slug=comp_slug)
        if not lb_ha_id:
            self.logger.info("  %s has no load_balancer assigned - skipping LB wiring", comp_slug)
            return

        vip_obj = await self._ensure_vip_service_from_ports(comp=comp, comp_slug=comp_slug, lb_ha_id=lb_ha_id)
        if not vip_obj:
            self.logger.info("  %s has no creatable VIP from service_ports - skipping LB wiring", comp_slug)
            return

        vip_id = vip_obj.id

        vip_hostname: str = getattr(getattr(vip_obj, "hostname", None), "value", vip_id)
        vip_proto: str = getattr(getattr(vip_obj, "protocol", None), "value", "")
        vip_port: str = str(getattr(getattr(vip_obj, "port", None), "value", ""))

        await self._assign_vip_to_lb(vip_obj, vip_id, vip_hostname)

        service_ports: list[dict[str, Any]] = comp.get("service_ports") or []
        backend_port = self._backend_port(service_ports)

        vm_instances = [i for i in instances if i.get("typename") == "DcimVirtualDevice"]
        for vm_stub in vm_instances:
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

    async def _ensure_vip_service_from_ports(self, comp: dict[str, Any], comp_slug: str, lb_ha_id: str) -> Any | None:
        service_ports: list[dict[str, Any]] = comp.get("service_ports") or []
        selected = self._select_primary_service_port(service_ports)
        if selected is None:
            self.logger.info("  %s has LB HA assigned but no service_ports - skipping VIP auto-create", comp_slug)
            return None

        port, service_port_protocol = selected

        app_fqdn = ""
        component_name = str(comp.get("name") or comp_slug)
        component_type = str(comp.get("component_type") or "backend")
        vip_hostname = self._derive_vip_hostname(app_fqdn, component_name, component_type, comp_slug)
        vip_protocol = PortsPlanner.to_vip_protocol(service_port_protocol=service_port_protocol, port=port)

        try:
            vip_obj = await self.client.create(
                kind=LoadbalancerVIP,
                data={
                    "hostname": vip_hostname,
                    "protocol": vip_protocol,
                    "port": port,
                    "status": "active",
                    "load_balancing_algorithm": "round_robin",
                    "load_balancer": {"id": lb_ha_id},
                },
            )
            await vip_obj.save(allow_upsert=True)
            self.logger.info("  Upserted VIP %s (%s/%s) on LB HA %s", vip_hostname, vip_protocol, port, lb_ha_id)
        except Exception as exc:
            self.logger.error("  Failed auto VIP create for %s on LB HA %s: %s", comp_slug, lb_ha_id, exc)
            return None

        component_id = str(comp.get("id") or "")
        if component_id:
            await self._attach_component_to_vip(vip_obj=vip_obj, component_id=component_id, comp_slug=comp_slug)

        health_check = await self._attach_default_health_check(
            vip_obj=vip_obj, vip_protocol=vip_protocol, comp_slug=comp_slug
        )

        if health_check and component_id:
            await self._attach_health_check_to_component(
                component_id=component_id, health_check=health_check, comp_slug=comp_slug
            )

        return vip_obj

    async def _resolve_component_lb_ha_id(self, comp: dict[str, Any], comp_slug: str) -> str:
        load_balancer = comp.get("load_balancer") or {}
        lb_ha_id = str(load_balancer.get("id") or "")
        if lb_ha_id:
            return lb_ha_id

        component_id = str(comp.get("id") or "")
        if not component_id:
            return ""

        try:
            component_obj = await self.client.get(kind=AppComponent, id=component_id, prefetch_relationships=True)
            if component_obj is None:
                return ""
            lb_rel = getattr(component_obj, "load_balancer", None)
            lb_peer = getattr(lb_rel, "peer", None) if lb_rel else None
            resolved_id = str(getattr(lb_peer, "id", "") or "")
            if resolved_id:
                return resolved_id
        except Exception as exc:
            self.logger.warning("  Could not resolve load_balancer for %s: %s", comp_slug, exc)

        return ""

    async def _attach_default_health_check(self, vip_obj: Any, vip_protocol: str, comp_slug: str) -> Any | None:
        check_type = self._health_check_type_for_vip(vip_protocol)

        try:
            health_check = await self.client.create(
                kind=LoadbalancerHealthCheck,
                data={"check": check_type, "rise": 3, "fall": 3, "timeout": 1000},
            )
            await health_check.save(allow_upsert=True)
        except Exception as exc:
            self.logger.warning("  Could not upsert default health check (%s) for %s: %s", check_type, comp_slug, exc)
            return None

        try:
            vip_hc_rel = getattr(vip_obj, "health_checks", None)
            if vip_hc_rel is None:
                self.logger.warning("  VIP health_checks relationship missing for %s", comp_slug)
                return health_check

            await vip_hc_rel.fetch()
            existing_ids = {peer.id for peer in vip_hc_rel.peers}
            if health_check.id in existing_ids:
                return health_check

            await self._safe_rel_add(vip_hc_rel, health_check)
            await vip_obj.save(allow_upsert=True)
            self.logger.info("  Attached %s health-check to VIP for %s", check_type, comp_slug)
        except Exception as exc:
            self.logger.warning("  Could not attach health check to VIP for %s: %s", comp_slug, exc)

        return health_check

    async def _attach_component_to_vip(self, vip_obj: Any, component_id: str, comp_slug: str) -> None:
        vip_components_rel = getattr(vip_obj, "app_components", None)
        if vip_components_rel is None:
            self.logger.debug("  VIP relation app_components missing for %s", comp_slug)
            return

        try:
            await vip_components_rel.fetch()
            existing_ids = {peer.id for peer in vip_components_rel.peers}
            if component_id in existing_ids:
                return

            await self._safe_rel_add(vip_components_rel, {"id": component_id})
            await vip_obj.save(allow_upsert=True)
            self.logger.info("  Attached AppComponent %s to VIP", comp_slug)
        except Exception as exc:
            self.logger.warning("  Could not attach AppComponent %s to VIP: %s", comp_slug, exc)

    async def _attach_health_check_to_component(self, component_id: str, health_check: Any, comp_slug: str) -> None:
        try:
            component_obj = await self.client.get(kind=AppComponent, id=component_id, prefetch_relationships=True)
        except Exception as exc:
            self.logger.warning("  Could not fetch AppComponent %s for health-check mapping: %s", component_id, exc)
            return

        if component_obj is None:
            return

        component_hc_rel = getattr(component_obj, "health_checks", None)
        if component_hc_rel is None:
            self.logger.debug("  Component %s has no health_checks relationship", component_id)
            return

        try:
            await component_hc_rel.fetch()
            existing_ids = {peer.id for peer in component_hc_rel.peers}
            if health_check.id in existing_ids:
                return

            await self._safe_rel_add(component_hc_rel, health_check)
            await component_obj.save(allow_upsert=True)
            self.logger.info("  Attached health-check to AppComponent %s", comp_slug)
        except Exception as exc:
            self.logger.warning(
                "  Could not attach health-check to AppComponent %s (%s): %s", comp_slug, component_id, exc
            )

    @staticmethod
    def _health_check_type_for_vip(vip_protocol: str) -> str:
        return PortsPlanner.health_check_type_for_vip(vip_protocol)

    async def _assign_segment_to_hosts(
        self, segment_id: str, segment_name: str, segment_kind: str, host_ids: set[str]
    ) -> None:
        self.logger.info(
            "  Segment '%s' (%s) - assigning to uplink interfaces on %d host(s)",
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
                iface_caps.add(segment_obj)
                assigned += 1
            await iface.save(allow_upsert=True)

        self.logger.info(
            "  Assigned segment '%s' to %d uplink interface(s) (%d already assigned)",
            segment_name,
            assigned,
            len(interfaces) - assigned,
        )

    async def _assign_vip_to_lb(self, vip_obj: Any, vip_id: str, vip_hostname: str) -> None:
        lb_rel = getattr(vip_obj, "load_balancer", None)
        lb_peer = getattr(lb_rel, "peer", None) if lb_rel else None
        if not lb_peer or not getattr(lb_peer, "id", None):
            return

        try:
            lb_ha = await self.client.get(kind=ManagedLoadbalancerHA, id=lb_peer.id, prefetch_relationships=True)
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
                        iface_caps.add(vip_obj)
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
        try:
            existing_members = await self.client.filters(kind=LoadbalancerPoolMember, name__value=member_name)
        except Exception as exc:
            self.logger.warning("  Could not check existing PoolMember '%s': %s", member_name, exc)
            existing_members = []

        if existing_members:
            try:
                await existing_members[0].save(allow_upsert=True)
            except Exception:
                pass
            self.logger.info("  PoolMember %s already exists - re-registered", member_name)
            return

        try:
            vm_full = await self.client.get(kind=DcimVirtualDevice, id=vm_id, prefetch_relationships=True)
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
                kind=LoadbalancerPoolMember,
                data={"name": member_name, "status": "active", "vip_service": {"id": vip_id}, "weight": 1},
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
                    vm_caps.add(pool_member)
                    await vm_full.save(allow_upsert=True)
            except Exception as exc:
                self.logger.warning("  Could not link PoolMember to VM %s capabilities: %s", vm_name, exc)

        pi_name = f"{member_name}-iface"
        pool_iface_data: dict[str, Any] = {"name": pi_name, "status": "active", "pool_member": {"id": pool_member.id}}
        if backend_port is not None:
            pool_iface_data["port"] = backend_port
        if ip_id:
            pool_iface_data["ip_address"] = {"id": ip_id}

        try:
            pool_iface = await self.client.create(kind=LoadbalancerPoolInterface, data=pool_iface_data)
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
                        pi_caps.add(pool_iface)
                        await target_iface.save(allow_upsert=True)
                except Exception as exc:
                    self.logger.warning("  Could not link PoolInterface to %s interface: %s", vm_name, exc)

        self.logger.info("  Wired %s -> %s:%s:%s", member_name, vip_hostname, vip_proto, vip_port)
