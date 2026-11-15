"""Phased batch writer for persisting Infrahub protocol nodes in dependency order."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# Explicit persistence order: tags & contexts -> devices -> interfaces -> subinterfaces -> IPs -> routing -> cables
PHASE_BY_KIND: dict[str, int] = {
    "SecurityTag": 10,
    "ManagedFirewallContext": 20,
    "ManagedLoadbalancerContext": 20,
    "LoadbalancerVIP": 22,
    "DcimPhysicalDevice": 30,
    "DcimVirtualDevice": 30,
    "DcimPhysicalInterface": 40,
    "DcimVirtualInterface": 40,
    "DcimLagInterface": 40,
    "DcimSubInterface": 42,
    "IpamIPAddress": 50,
    "RoutingVRF": 60,
    "RoutingBGPPeerGroup": 65,
    "DcimCable": 90,
}
_DEFAULT_PHASE = 50


def _kind_name(kind: Any) -> str:
    return kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))


class BatchWriter:
    """Collects (protocol_kind, payload) items from pure GraphQL builders and flushes them phase-by-phase."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._staged: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []

    def add(self, kind: Any, data: dict[str, Any], **save_kwargs: Any) -> None:
        """Stage a single protocol payload for batched persistence."""
        self._staged.append((kind, data, {"allow_upsert": True, **save_kwargs}))

    def extend(self, items: list[tuple[Any, dict[str, Any]]] | tuple[tuple[Any, dict[str, Any]], ...]) -> None:
        """Stage multiple (protocol_kind, payload) pairs returned by a pure builder."""
        for kind, data in items:
            self.add(kind, data)

    def __len__(self) -> int:
        return len(self._staged)

    async def flush(self) -> list[Any]:
        """Persist all staged items grouped by phase using Infrahub batches, then clear the buffer."""
        if not self._staged:
            return []

        items, self._staged = self._staged, []
        by_phase: dict[int, list[tuple[Any, dict[str, Any], dict[str, Any]]]] = defaultdict(list)
        for kind, data, kwargs in items:
            phase = PHASE_BY_KIND.get(_kind_name(kind), _DEFAULT_PHASE)
            by_phase[phase].append((kind, data, kwargs))

        saved: list[Any] = []
        for phase in sorted(by_phase):
            batch = await self.client.create_batch()
            for kind, data, kwargs in by_phase[phase]:
                node = await self.client.create(kind=kind, data=data)
                batch.add(task=node.save, node=node, **kwargs)
            async for node, error in batch.execute():
                if error:
                    raise error
                saved.append(node)
        return saved
