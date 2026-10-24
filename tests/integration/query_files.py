"""The GraphQL query files of tests/integration/queries/, loaded once."""

from pathlib import Path

# ======================================================================
# DC topology and routing
# ======================================================================

_QUERIES_DIR = Path(__file__).parent / "queries"


def _load_query(filename: str) -> str:
    """Load a GraphQL query from tests/integration/queries."""
    return (_QUERIES_DIR / filename).read_text(encoding="utf-8")


QUERY_GET_DC_DEVICES = _load_query("get_dc_devices.gql")

QUERY_GET_ENDPOINT_CABLING = _load_query("get_endpoint_cabling.gql")

QUERY_GET_APPLICATION_GRAPH = _load_query("get_application_graph.gql")

QUERY_GET_INTERCONNECT_INVENTORY = _load_query("get_interconnect_inventory.gql")

QUERY_GET_TENANT_SERVICES = _load_query("get_tenant_services.gql")

QUERY_GET_TRANSIT_INVENTORY = _load_query("get_transit_inventory.gql")

QUERY_GET_LOADBALANCER_INVENTORY = _load_query("get_loadbalancer_inventory.gql")

QUERY_GET_FABRIC_POOLS = _load_query("get_fabric_pools.gql")
