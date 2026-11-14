"""add_connectivity_request: reconcile inter-topology circuits for a TopologyConnectivityRequest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from generators.mixins.common import CommonGenerator
from generators.protocols import TopologyConnectivityRequest
from utils.data_cleaning import clean_data

CONNECTIVITY_REQUEST_GENERATOR = "add_connectivity_request"
VALID_CONNECTIVITY_TYPES = frozenset({"dci", "cloud_onramp", "saas_peering", "wan_access"})


@dataclass(frozen=True)
class ConnectableEndpoint:
    """One TopologyConnectableLocation end of a connectivity request."""

    id: str
    kind: str
    label: str
    circuit_ids: frozenset[str]

    @classmethod
    def parse(cls, node: Any) -> ConnectableEndpoint | None:
        if not isinstance(node, dict) or not node.get("id"):
            return None
        circuits = {c["id"] for c in (node.get("circuits") or []) if isinstance(c, dict) and c.get("id")}
        return cls(
            id=node["id"],
            kind=node.get("typename") or "",
            label=node.get("display_label") or node["id"],
            circuit_ids=frozenset(circuits),
        )


def matching_circuit_ids(
    source: ConnectableEndpoint,
    destination: ConnectableEndpoint,
    existing_realized: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return sorted circuit IDs spanning both source and destination (plus any already attached)."""
    shared = set(source.circuit_ids & destination.circuit_ids)
    for item in existing_realized or []:
        if isinstance(item, dict) and item.get("id"):
            shared.add(item["id"])
    return sorted(shared)


class ConnectivityRequestGenerator(CommonGenerator):
    """Reconciles realized circuits and lifecycle status for a TopologyConnectivityRequest."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        requests = cleaned.get("TopologyConnectivityRequest") or []
        if not requests:
            self.logger.warning("ConnectivityRequestGenerator: no TopologyConnectivityRequest in query payload")
            return
        for req in requests:
            await self._reconcile_request(req)

    async def _reconcile_request(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        name = req.get("name") or req_id or "unknown"
        if not req_id:
            self.fail(f"ConnectivityRequest '{name}': missing id")

        source = ConnectableEndpoint.parse(req.get("source"))
        destination = ConnectableEndpoint.parse(req.get("destination"))
        if source is None or destination is None:
            self.fail(f"ConnectivityRequest '{name}': both source and destination locations are required")
        if source.id == destination.id:
            self.fail(f"ConnectivityRequest '{name}': source and destination cannot be the same ({source.label})")

        conn_type = req.get("connectivity_type") or "dci"
        if conn_type not in VALID_CONNECTIVITY_TYPES:
            self.fail(f"ConnectivityRequest '{name}': unsupported connectivity_type '{conn_type}'")

        circuit_ids = matching_circuit_ids(source, destination, req.get("realized_circuits"))
        target_status = "active" if circuit_ids else "provisioning"

        node = await self.client.get(kind=TopologyConnectivityRequest, id=req_id)
        if circuit_ids:
            existing = {i["id"] for i in (req.get("realized_circuits") or []) if isinstance(i, dict) and i.get("id")}
            missing = [cid for cid in circuit_ids if cid not in existing]
            if missing:
                await node.add_relationships(relation_to_update="realized_circuits", related_nodes=missing)

        if getattr(getattr(node, "status", None), "value", None) != target_status:
            node.status.value = target_status
            await node.save()

        self.logger.info(
            f"  {name} ({conn_type}: {source.label} -> {destination.label}): {len(circuit_ids)} circuit(s)"
        )
