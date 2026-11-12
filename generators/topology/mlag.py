"""Generator: MLAG peer-link wiring, reached only off the ManagedMLAG "updated" triggers.
Domain creation is DeviceMixin._ensure_mlag_pairs; both use MLAGWiringMixin."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..mlag import MLAGWiringMixin
from ..pools import PoolMixin
from ..protocols import ManagedMLAG


class MLAGGenerator(MLAGWiringMixin, CablingMixin, PoolMixin, CommonGenerator):
    """Wire capabilities and peer-link interfaces for both devices in an MLAG domain."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        mlag_nodes: list[dict] = cleaned.get("ManagedMLAG", [])

        if not mlag_nodes:
            self.logger.error("No ManagedMLAG found in query response")
            return

        for mlag in mlag_nodes:
            mlag_obj = await self.client.get(kind=ManagedMLAG, id=mlag["id"], include=["capabilities"])
            member_ids = [peer.id for peer in getattr(mlag_obj, "capabilities").peers]
            # track=False: the wiring belongs to the device generator (DeviceMixin._ensure_mlag_pairs).
            await self.ensure_mlag_wiring(mlag_obj, mlag["name"], member_ids=member_ids, track=False)
