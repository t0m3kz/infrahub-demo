"""Generator for customer boarding — turns an OrganizationProfile into infrastructure.

Triggered when an OrganizationCustomer with a profile is created (see
data/events/99_actions.yml: trigger-customer-boarding-on-created). Reads the
customer's OrganizationProfile and, for every allowed environment:

  1. Get-or-creates an IpamNamespace named "{org_id}-{ENV_SUFFIX}", tagged
     into the vrf_namespaces group so VrfNamespaceGenerator (add_vrf_namespace)
     allocates its L3 VNI from the shared GLOBAL-L3VNI pool on its own trigger.
  2. Allocates a customer prefix (sized by profile.global_subnet_size) from
     the shared Customer-IPv4 pool into that namespace.

Idempotent: every step is query-then-create, so re-running (e.g. after the
profile's allowed_environments grows) only creates the delta.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator

CUSTOMER_PREFIX_POOL_NAME = "Customer-IPv4"

# Suffix map shared with data/demos/06_customers/*/00_environments.yml's convention.
ENV_SUFFIX = {
    "p": "PROD",
    "n": "NONPROD",
    "s": "STAGING",
    "d": "DEV",
    "t": "TEST",
}


class CustomerBoardingGenerator(CommonGenerator):
    """Board a customer onto per-environment namespaces and IP allocation from its profile."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        customers = cleaned.get("OrganizationCustomer", [])
        if not customers:
            self.logger.error("No OrganizationCustomer data in GraphQL response")
            return

        customer = customers[0]
        org_id: str = customer.get("org_id", "")
        customer_name: str = customer.get("name", org_id)

        if not org_id:
            self.logger.error("Customer missing org_id — cannot proceed")
            return

        profile = customer.get("profile")
        if not profile:
            self.logger.info(f"Customer '{customer_name}' ({org_id}) has no profile — nothing to board")
            return

        profile_name: str = profile.get("name", "")
        allowed_environments: list[str] = profile.get("allowed_environments") or ["p"]
        subnet_size: int = profile.get("global_subnet_size") or 24

        self.logger.info(
            f"Boarding customer '{customer_name}' ({org_id}) with profile '{profile_name}' "
            f"— environments={allowed_environments}, subnet=/{subnet_size}"
        )

        for env in allowed_environments:
            suffix = ENV_SUFFIX.get(env)
            if not suffix:
                self.logger.warning(f"Unknown environment code '{env}' on profile '{profile_name}' — skipping")
                continue
            await self._board_environment(
                org_id=org_id, customer_name=customer_name, env_suffix=suffix, subnet_size=subnet_size
            )

    async def _board_environment(self, org_id: str, customer_name: str, env_suffix: str, subnet_size: int) -> None:
        """Get-or-create the VRF namespace for one environment and allocate its customer prefix."""
        ns_name = f"{org_id}-{env_suffix}"

        namespace = await self.client.get(kind="IpamNamespace", name__value=ns_name, raise_when_missing=False)
        if namespace is None:
            try:
                namespace = await self.client.create(
                    kind="IpamNamespace",
                    data={
                        "name": ns_name,
                        "description": f"{customer_name} {env_suffix.lower()} VRF",
                        "member_of_groups": ["vrf_namespaces"],
                    },
                )
                await namespace.save(allow_upsert=True)
                self.logger.info(
                    f"Created namespace '{ns_name}' (L3 VNI allocated by add_vrf_namespace on its own trigger)"
                )
            except Exception as exc:
                self.logger.error(f"Failed to create namespace '{ns_name}': {exc}")
                return
        else:
            self.logger.info(f"Namespace '{ns_name}' already exists — re-saving for tracker")
            await namespace.save(allow_upsert=True)

        await self._allocate_customer_prefix(namespace=namespace, ns_name=ns_name, subnet_size=subnet_size)

    async def _allocate_customer_prefix(self, namespace: Any, ns_name: str, subnet_size: int) -> None:
        """Idempotently allocate one customer prefix into the namespace from the shared pool.

        CoreIPPrefixPool.ip_namespace is fixed per-pool (Customer-IPv4 lives in the
        "default" namespace) — allocate_next_ip_prefix cannot override it per-call.
        So allocation happens in two steps: (1) reserve a block from the shared pool
        (lands in "default", collision-free across all customers via the pool's own
        bookkeeping), then (2) create the tenant-visible IpamPrefix with that same
        CIDR scoped to the customer's namespace — matching the pattern every
        hand-authored customer segment file already uses (ip_namespace: "C001-PROD").
        """
        existing = await self.client.filters(kind="IpamPrefix", ip_namespace__ids=[namespace.id])
        if existing:
            self.logger.info(
                f"Namespace '{ns_name}' already has a prefix ({existing[0].prefix.value}) — skipping allocation"
            )
            return

        try:
            pool = await self.client.get(kind="CoreIPPrefixPool", name__value=CUSTOMER_PREFIX_POOL_NAME)
        except Exception as exc:
            self.logger.error(
                f"Cannot find shared pool '{CUSTOMER_PREFIX_POOL_NAME}' — cannot allocate prefix for '{ns_name}': {exc}"
            )
            return

        try:
            reservation = await self.client.allocate_next_ip_prefix(
                resource_pool=pool,
                identifier=f"{ns_name}-customer-prefix",
                prefix_length=subnet_size,
                data={"description": f"Reservation for {ns_name}", "role": "customer"},
            )
        except Exception as exc:
            self.logger.error(f"Failed to reserve customer prefix for '{ns_name}': {exc}")
            return

        cidr = reservation.prefix.value
        try:
            tenant_prefix = await self.client.create(
                kind="IpamPrefix",
                data={
                    "prefix": cidr,
                    "description": f"Customer prefix for {ns_name}",
                    "role": "customer",
                    "status": "active",
                    "ip_namespace": {"id": namespace.id},
                },
            )
            await tenant_prefix.save(allow_upsert=True)
            self.logger.info(f"Allocated prefix {cidr} for namespace '{ns_name}'")
        except Exception as exc:
            self.logger.error(f"Failed to create tenant prefix {cidr} in namespace '{ns_name}': {exc}")
