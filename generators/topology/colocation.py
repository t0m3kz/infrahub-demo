"""Colocation metro bootstrap: the identity and the address supernets of the metro's EVPN Multi-Site site.

A colocation metro (the Equinix "FR", the CoreSite "NY") is no facility and owns
no devices. Its children do: a TopologyColocationZone (the cage, our own hardware)
and a TopologyVirtualColocation (a provider-hosted presence) declare and deploy
their own kit, see colocation_fabric.py.

What the metro keeps is what those children share. One metro is one EVPN
Multi-Site site however many zones carry edge routers, so the metro holds the
site's identity:

* the L2 VNI pool of the segments local to the site (``vni_pool``, read by
  generators/topology/segment.py off TopologySegmentHosting, exactly like a DC's);
* the ASN pool (``fabric_asn_pool``) the site's one AS is drawn from. The AS itself
  is not drawn here: it is drawn by whichever zone or virtual colocation first has
  an edge pair to run it on (ColocationFabricGenerator._ensure_evpn), which also
  attaches it as ``evpn_rt_as``, the site-id and admin field of the site's
  route-targets.

When the metro has a ``design`` (TopologyTemplateColocationMetro) it is also the
supernet holder of its zones, the way a DC is of its pods. The design states how many
zones the metro holds (``max_zones``) and which zone designs they may use, and every
pool is sized for that envelope before any zone exists (generators/fabric_sizing.py):

* ``{metro}-loopback-pool``, ``{metro}-management-pool`` and ``{metro}-technical-pool``,
  IPv4 prefix pools sliced from Loopback-IPv4 / Management-IPv4 / Technical-IPv4 and
  attached as ``loopback_pool`` / ``management_pool`` / ``technical_pool``;
* ``{metro}-loopback-ipv6-pool`` and ``{metro}-technical-ipv6-pool``, the IPv6
  supernets of the zones' VTEP loopbacks and edge-pair interlinks, sliced from
  Loopback-IPv6 / Technical-IPv6. Not attached to anything: the zones find them by name;
* the ASN block is sized for ``max_zones`` zones.

Each zone and virtual colocation then slices its own pools out of these
(ColoPoolSlot in colocation_pools.py is the one table of names and sizes, read by
both tiers). A metro without a design creates none of the supernets; its zones
slice the bootstrap pools directly with fixed sizes.

The pools are idempotent upserts by name, so overlapping runs converge; only the
supernets, which allocate a prefix, are serialized by a lock. A pool that already
exists keeps its prefix when the design changes (the slice is keyed by the pool's
name), so a larger design needs the pool and its prefix deleted first.

Registered as add_colocation_metro in .infrahub.yml, targeting the
colocation_metros group and querying colocation_metro.gql.
"""

from __future__ import annotations

from typing import Any, cast

from typing_extensions import TypedDict

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..fabric_sizing import colo_metro_asn_units
from ..helpers.pools import SITE_VNI_END, SITE_VNI_START, name_to_asn_range
from ..pools import PoolMixin
from ..protocols import TopologyColocationMetro
from .colocation_pools import ATTACHED_SLOTS, COLO_POOL_SLOTS

# Metro ASN blocks are drawn from the same deterministic name-hash grid as DC
# fabric ASNs (helpers/pools.py's name_to_asn_range), so a metro can never
# collide with a DC or with another metro. The block is sized in name_to_asn_range's
# max_pods argument, the DC sense of "units of fabric to size for": a metro with a
# design asks for max_zones of them (colo_metro_asn_units); this is the single unit
# of a metro without one, which uses its one site AS and nothing else.
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
        # clean_data() unwraps the raw GraphQL edges/node/value nesting into the
        # flat dicts this generator reads — same first step as every other
        # generator here (see dc.py's generate()).
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

        # Every size is derived before the first save: a run that stopped after
        # saving would end with a partial tracked set, and its cleanup would delete
        # what it owned last run and did not recreate.
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

        # L2 VNI pool for segments local to this metro, exactly like a DC's
        # (generators/topology/segment.py reads vni_pool off
        # TopologySegmentHosting). Stretched segments reaching the metro take
        # their VNI from GLOBAL-L2VNI instead.
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
            # Serialized because each supernet allocates a prefix from a shared
            # bootstrap pool: two overlapping runs of one metro could otherwise
            # both create a pool of the same name (see allocate_resource_pools).
            async with self.resource_lock(f"colocation-metro-pools-{metro}"):
                for slot, prefix_length in supernet_lengths.items():
                    # A prefix pool for every slot: the zones slice their own
                    # address and prefix pools out of it. An existing pool keeps its
                    # prefix when the design changed, see the module docstring.
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
