"""Colocation metro bootstrap: the VNI and ASN pools and, with a ``design``, the address supernets of the metro's
Multi-Site site. A metro owns no devices; zones and virtual colocations slice their pools from its supernets
(ColoPoolSlot) or from bootstrap pools without a design. Pools are upserts by name, only supernets are locked, an
existing pool keeps its prefix. Registered as add_colocation_metro in .infrahub.yml.
"""

from __future__ import annotations

from typing import Any, cast

from typing_extensions import TypedDict

from generators.colocation.colocation_pools import ATTACHED_SLOTS, COLO_POOL_SLOTS
from generators.helpers.pools import name_to_asn_range
from generators.mixins.common import CommonGenerator
from generators.mixins.fabric_sizing import colo_metro_asn_units
from generators.mixins.pools import PoolMixin
from generators.protocols import TopologyColocationMetro
from utils.data_cleaning import clean_data
from utils.limits import SITE_VNI_END, SITE_VNI_START

# Metro ASN blocks come from the same name-hash grid as DC fabric ASNs (name_to_asn_range), so they never collide; a
# metro without a design needs a single unit (its one site AS).
_METRO_ASN_SIZING_UNITS = 1


class TopologyColocationMetroData(TypedDict, total=False):
    """The subset of queries/topology/add/colocation_metro.gql this generator reads."""

    id: str
    name: str
    design: dict[str, Any] | None


class ColocationMetroGenerator(PoolMixin, CommonGenerator):
    """Create a colocation metro's VNI and ASN pools and attach them."""

    data: TopologyColocationMetroData
    graphql_root_key = "TopologyColocationMetro"

    async def generate(self, data: dict[str, Any]) -> None:
        # clean_data() flattens the GraphQL edges/node/value nesting
        metros = clean_data(data).get(self.graphql_root_key, [])
        if not metros:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        for metro in metros:
            self.data = cast(TopologyColocationMetroData, metro)
            await self._generate_metro()

    async def _generate_metro(self) -> None:
        metro_id = self.data["id"]
        metro = self.data["name"].lower()
        design = self.data.get("design")
        self.fabric_name = metro
        self.deployment_id = metro_id
        self.logger.info(f"Generating colocation metro {self.data['name']}")

        # Derive every size before the first save, so a partial run does not leave a tracked set its cleanup would
        # delete
        try:
            supernet_lengths = {slot: slot.metro_length(design) for slot in COLO_POOL_SLOTS} if design else {}
        except ValueError as exc:
            self.logger.error(f"Metro {metro}: cannot size its pools from design '{design and design['name']}': {exc}")
            return

        asn_units = colo_metro_asn_units(design) if design else _METRO_ASN_SIZING_UNITS
        asn_start, asn_end = name_to_asn_range(dc_name=metro, max_pods=asn_units)
        asn_pool = await self.upsert_asn_pool(
            pool_name=f"{metro}-asn-pool",
            description=f"ASN pool for colocation metro {metro.upper()}",
            start_range=asn_start,
            end_range=asn_end,
        )

        # L2 VNI pool for segments local to the metro; stretched segments use GLOBAL-L2VNI
        vni_pool = await self.upsert_number_pool(
            pool_name=f"{metro}-vni-pool",
            description=f"L2 VNI pool for colocation metro {metro.upper()}",
            start_range=SITE_VNI_START,
            end_range=SITE_VNI_END,
            node="ManagedSegmentDeployment",
            node_attribute="vni",
        )

        refs: dict[str, str | None] = {"fabric_asn_pool": asn_pool.id, "vni_pool": vni_pool.id}
        if supernet_lengths:
            # Serialized: each supernet allocates a prefix from a shared bootstrap pool (see allocate_resource_pools)
            async with self.resource_lock(f"colocation-metro-pools-{metro}"):
                for slot, prefix_length in supernet_lengths.items():
                    # A prefix pool per slot, sliced by the zones; an existing pool keeps its prefix when the design
                    # changes
                    pool = await self.ensure_sliced_pool(
                        pool_name=slot.name(metro),
                        parent_pool_name=slot.bootstrap_pool,
                        prefix_length=prefix_length,
                        role=slot.role,
                        kind="prefix",
                    )
                    if slot in ATTACHED_SLOTS:
                        refs[cast(str, slot.attribute)] = pool.id
            self.logger.info(
                f"Metro {metro}: design '{cast(dict, design)['name']}' sized supernets "
                + ", ".join(f"{slot.name(metro)}=/{length}" for slot, length in supernet_lengths.items())
            )

        await self.attach_refs(kind=TopologyColocationMetro, node_id=metro_id, refs=refs)
        self.logger.info(f"Metro {metro}: attached ASN/VNI{'/supernet' if supernet_lengths else ''} pool references")
