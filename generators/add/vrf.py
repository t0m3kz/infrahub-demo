"""Generator for VRF namespace L3 VNI allocation.

When a VRF namespace (IpamNamespace) is added to the vrf_namespaces group,
this generator allocates an L3 VNI from the global GLOBAL-L3VNI pool.

L3 VNI is per-VRF (globally unique), not per-DC. A single global pool
ensures uniqueness across all VRFs regardless of which DCs they span.

Idempotency: if l3_vni is already set, the generator saves the namespace
(for tracker registration) and skips allocation.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator

GLOBAL_L3VNI_POOL_NAME = "GLOBAL-L3VNI"


class VrfNamespaceGenerator(CommonGenerator):
    """Allocate L3 VNI for VRF namespaces from global pool."""

    async def generate(self, data: dict[str, Any]) -> None:
        """Allocate L3 VNI for the namespace if not already set."""
        cleaned = clean_data(data)
        ns_list = cleaned.get("IpamNamespace", [])
        if not ns_list:
            self.logger.error("No IpamNamespace data in GraphQL response")
            return

        ns = ns_list[0]
        ns_id: str = ns.get("id", "")
        ns_name: str = ns.get("name", "")

        if not ns_id or not ns_name:
            self.logger.error("Namespace missing id or name — cannot proceed")
            return

        self.logger.info(f"Processing VRF namespace: {ns_name}")

        # Skip default namespace — it's underlay, not a VRF
        if ns_name == "default":
            self.logger.info(f"Namespace '{ns_name}' is the default namespace — skipping L3 VNI allocation")
            return

        # Check if L3 VNI is already set
        existing_l3_vni = ns.get("l3_vni")
        if existing_l3_vni is not None:
            self.logger.info(f"Namespace '{ns_name}' already has L3 VNI {existing_l3_vni} — re-saving for tracker")
            namespace_obj = await self.client.get(kind="IpamNamespace", id=ns_id)
            await namespace_obj.save(allow_upsert=True)
            return

        # Fetch global L3 VNI pool
        try:
            l3_vni_pool = await self.client.get(
                kind="CoreNumberPool",
                name__value=GLOBAL_L3VNI_POOL_NAME,
            )
        except Exception as exc:
            self.logger.error(
                f"Cannot find global pool '{GLOBAL_L3VNI_POOL_NAME}' — "
                f"cannot allocate L3 VNI for namespace '{ns_name}': {exc}"
            )
            return

        # Allocate L3 VNI from global pool
        vni_identifier = f"{ns_id}-l3vni"
        self.logger.info(f"Allocating L3 VNI for namespace '{ns_name}' from pool {GLOBAL_L3VNI_POOL_NAME}")

        try:
            namespace_obj = await self.client.get(kind="IpamNamespace", id=ns_id)
            namespace_obj.l3_vni = {"from_pool": {"id": l3_vni_pool.id}, "identifier": vni_identifier}
            await namespace_obj.save(allow_upsert=True)
            self.logger.info(f"Namespace '{ns_name}' updated with L3 VNI from pool")
        except Exception as exc:
            self.logger.error(f"Failed to allocate L3 VNI for namespace '{ns_name}': {exc}")
