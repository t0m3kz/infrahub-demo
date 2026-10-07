"""Generator: MLAG peer-link wiring.

Only reached now off the two remaining ManagedMLAG "updated" triggers:
capabilities changed, or virtual_peer_link changed, by something outside
the topology-generator flow (direct API/UI edit, branch merge). Domain
*creation* wiring is inlined into DeviceMixin._ensure_mlag_pairs
(generators/devices.py) — that trigger was removed from
data/events/99_actions.yml. Both call the same MLAGWiringMixin.
"""

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
            mlag_obj = await self.client.get(kind=ManagedMLAG, id=mlag["id"])
            # track=False: the wiring belongs to the device generator that
            # created this domain (DeviceMixin._ensure_mlag_pairs), which
            # re-tracks it on every run. This trigger only re-wires it.
            await self.ensure_mlag_wiring(mlag_obj, mlag["name"], track=False)
