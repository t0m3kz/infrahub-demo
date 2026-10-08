"""Generator: LB backend no-SNAT return-path wiring.

Mirrors generators/topology/customer_dc.py's FirewallContext sub-interface
provisioning, but for LoadbalancerVIP: when a VIP has snat_enabled=false,
backend pool members see the real client IP, so their response traffic
would otherwise return via the fabric's anycast gateway, bypassing the
load balancer entirely. Attaching the LB directly to the VIP's
backend_segment as a routed peer (a VLAN sub-interface + IP on that
segment) gives PBR rendering (transforms/helpers/loadbalancer_pbr.py) a
real next-hop to redirect backend response traffic through.

No-op when snat_enabled is true (default) or backend_segment is unset —
SNAT is the common case and needs nothing extra, the LB's own vip_ip
already sees all return traffic.

Triggered on LoadbalancerVIP create/update (see data/events/99_actions.yml's
trigger-lb-backend-nexthop-* rules). Registered as add_lb_backend_nexthop in
.infrahub.yml, targeting the loadbalancer_vips group and querying
loadbalancer_vip_data.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.protocols import CoreIPAddressPool

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..protocols import LoadbalancerVIP


class LoadbalancerBackendNexthopGenerator(CablingMixin, CommonGenerator):
    """add_lb_backend_nexthop — VLAN sub-interface + IP for a no-SNAT VIP's
    return path, on every device of the VIP's own load-balancer HA domain.

    Composes CommonGenerator like every other CablingMixin consumer. The mixin
    documents ``deployment_id``/``_resolve_pool``/``_retry_delay`` as host-class
    requirements; only ``find_role_interface`` and ``ensure_vlan_subinterface``
    are used here, so inheriting InfrahubGenerator directly happened to work, but
    any future call into ``create_cabling`` would have raised AttributeError.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        entries = cleaned.get("LoadbalancerVIP", [])
        if not entries:
            self.logger.info("No LoadbalancerVIP data in GraphQL response — not this generator's kind")
            return
        vip = entries[0]

        vip_id: str = vip.get("id", "")
        vip_hostname: str = vip.get("hostname", vip_id)
        if not vip_id:
            self.logger.error("VIP missing id — cannot proceed")
            return

        if vip.get("snat_enabled", True):
            self.logger.info(f"VIP {vip_hostname}: snat_enabled — no return-path wiring needed")
            return

        backend_segment = vip.get("backend_segment")
        if not backend_segment or not backend_segment.get("id"):
            self.logger.info(f"VIP {vip_hostname}: no-SNAT but no backend_segment set — skipping")
            return

        load_balancer = vip.get("load_balancer") or {}
        devices: list[dict[str, Any]] = load_balancer.get("capabilities") or []
        if not devices:
            self.logger.error(f"VIP {vip_hostname}: load balancer has no member devices — cannot proceed")
            return

        # ManagedVlanSegment.vlan_id is a manual attribute, always present
        # directly. ManagedVxlanSegment has no plain vlan_id — its LOCAL
        # VLAN ID is per VLAN domain (ManagedVlanDomainSegment), not
        # per-deployment, and this generator has no device to resolve a
        # domain against (a VIP's backend_segment isn't itself a leaf/MLAG
        # pair) — VXLAN backend segments are a known gap here, not yet
        # supported for no-SNAT return-path wiring.
        vlan_id_value = backend_segment.get("vlan_id")
        if vlan_id_value is None:
            self.logger.error(
                f"VIP {vip_hostname}: backend_segment '{backend_segment.get('name')}' has no VLAN ID "
                "(VXLAN backend segments are not yet supported for no-SNAT return-path wiring)"
            )
            return

        # ip_prefix is queried as a bare `{ id }` selection — clean_data()
        # collapses that single-key dict straight to the id string.
        segment_prefix_id: str = (backend_segment.get("gateway") or {}).get("ip_prefix") or ""
        pool = None
        if segment_prefix_id:
            pool = await self._ensure_backend_pool(
                vip_id=vip_id, vip_hostname=vip_hostname, segment_prefix_id=segment_prefix_id
            )

        vip_obj = await self.client.get(kind=LoadbalancerVIP, id=vip_id)

        for device in devices:
            device_id = device["id"]
            device_name = device["name"]
            ip_id: str | None = None
            if pool is not None:
                ip_id = await self._allocate_backend_ip(
                    pool=pool, vip_id=vip_id, segment_prefix_id=segment_prefix_id, device_name=device_name
                )

            try:
                trunk_iface = await self.find_role_interface(
                    device_id=device_id, role="downlink", fallback_any_physical=True
                )
            except Exception as exc:
                self.logger.error(f"Error resolving downlink interface on {device_name}: {exc}")
                continue
            if trunk_iface is None:
                self.logger.error(f"{device_name}: no downlink interface found — cannot wire VIP {vip_hostname}")
                continue

            await self.ensure_vlan_subinterface(
                device_id=device_id,
                device_name=device_name,
                trunk_iface=trunk_iface,
                vlan_id_value=vlan_id_value,
                capability_obj=vip_obj,
                ip_address_id=ip_id,
                # Shared: <downlink>.<vlan> is the same sub-interface for every
                # no-SNAT VIP on this backend segment, so no VIP's run owns it.
                # The /32 on it stays owned (and cleaned up) by this VIP's run.
                track=False,
            )

    async def _ensure_backend_pool(
        self, *, vip_id: str, vip_hostname: str, segment_prefix_id: str
    ) -> CoreIPAddressPool | None:
        """The backend_segment's EXISTING prefix wrapped in this VIP's own
        CoreIPAddressPool (see CablingMixin.ensure_prefix_address_pool).

        One pool per VIP, named from the VIP's own id: it belongs to this
        VIP's run alone, so tracking it is safe — a pool named after the
        segment's prefix was shared by every no-SNAT VIP on that segment, and
        the first of them to change its backend deleted it for the others.
        Two VIPs' pools over the same prefix still never hand out the same
        address: allocation skips addresses that already exist in the prefix.
        """
        pool = await self.ensure_prefix_address_pool(
            pool_name=f"lb-backend-{vip_id}-pool", prefix_id=segment_prefix_id, prefix_length=32
        )
        if pool is None:
            self.logger.error(f"VIP {vip_hostname}: no backend IP pool — sub-interfaces get no address")
        return pool

    async def _allocate_backend_ip(
        self, *, pool: CoreIPAddressPool, vip_id: str, segment_prefix_id: str, device_name: str
    ) -> str | None:
        """This VIP's /32 for ``device_name`` — owned by this VIP's run. The
        identifier carries the prefix, so a moved backend_segment draws a
        fresh address from the new prefix instead of the old reservation;
        the old address is no longer saved and is cleaned up with the run."""
        return await self.allocate_prefix_address(
            pool=pool,
            identifier=f"{vip_id}-{segment_prefix_id}-{device_name}-lb-backend",
            prefix_length=32,
            description=f"LB backend return-path — {device_name}",
        )
