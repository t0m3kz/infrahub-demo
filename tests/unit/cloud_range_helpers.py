"""Builders the cloud routed-range tests share: a recording client that also answers for networks and attachments."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from tests.unit.segment_customer_pool_helpers import Client

ACCOUNT = {"id": "acc-1", "name": "vaultex-aws"}
REGION_ID = "region-1"


def network(network_id: str, name: str) -> MagicMock:
    """A CloudVirtualNetwork node."""
    node = MagicMock(id=network_id)
    node.name.value = name
    return node


def attachment(attachment_id: str, network_id: str, prefixes: tuple[str, ...] = ()) -> MagicMock:
    """A CloudGatewayAttachment node to a network, with ``prefixes`` ids already on its allowed_prefixes."""
    node = MagicMock(id=attachment_id)
    node.virtual_network.id = network_id
    node.allowed_prefixes.peer_ids = list(prefixes)
    return node


class CloudClient(Client):
    """The pool client, plus the networks and attachments the range function reads (recorded queries)."""

    def __init__(self, networks: list[MagicMock], attachments: list[MagicMock]) -> None:
        super().__init__()
        self.networks = networks
        self.attachments = attachments
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        if isinstance(kind, str):
            self.queries.append((kind, kwargs))
            return self.networks if kind == "CloudVirtualNetwork" else self.attachments
        return await super().filters(kind=kind, **kwargs)


def footprint(**overrides: Any) -> dict[str, Any]:
    """A cleaned cloud footprint (customer C003, PROD, tier S: a /28)."""
    data: dict[str, Any] = {
        "id": "cloud-1",
        "name": "C003-P-VAULTEX-AWS-EU-CENTRAL-1",
        "environment": "p",
        "owner": {"org_id": "C003"},
        "account": ACCOUNT,
        "parent": {"id": REGION_ID},
        "design": {"name": "S", "address_prefix_length": 28},
    }
    return {**data, **overrides}
