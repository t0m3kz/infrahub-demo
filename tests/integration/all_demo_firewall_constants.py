"""30_all firewall expectations (test_62, test_64): firewall contexts, internet egress rules and the inter-VRF exchange
legs through the contexts."""

from typing import TypedDict

from utils.exchange_transit import EXCHANGE_PEERS, PROD_TYPE, namespace_type_for_environment

from .all_demo_circuit_constants import ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS, _site
from .all_demo_constants import _ALL_DEMO_ROOT, ALL_DEMO_DC_NAMES, ALL_DEMO_EXPECTED_OBJECTS, _all_demo
from .demo_data import ROOT, declared, name

# One shared (tenant-less) firewall context per firewall cluster: one per DC
# and one per serving colocation zone, whose pair every footprint naming that
# serving_zone shares (a colocation has no design, so never a dedicated
# context). The colocation context carries transit legs like a DC one (its
# legs face the zone's two edges, whose firewall ports are tagged with the
# context) ...
ALL_DEMO_SHARED_FIREWALL_CONTEXTS = len(ALL_DEMO_DC_NAMES) + len(ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS)


_BOOTSTRAP_ROOT = ROOT / "data" / "bootstrap"


def _dedicated_tenants(flag: str) -> dict[str, str]:
    """Tenant deployment -> design, for every DC footprint whose design blueprint sets ``flag``."""
    designs = {name(design) for design in declared("TopologyCustomerTemplateDC", _BOOTSTRAP_ROOT) if design.value(flag)}
    return {
        name(deployment): str(deployment.item["design"])
        for deployment in _all_demo("TopologyCustomerDC")
        if deployment.item.get("design") in designs
    }


# ... plus one dedicated context per customer whose design blueprint sets
# dedicated_firewall (data/bootstrap/21_customer_templates.yml: L_DC and
# XL_DC do, S_DC and M_DC do not). Maps tenant deployment -> design.
ALL_DEMO_DEDICATED_FIREWALL_TENANTS: dict[str, str] = _dedicated_tenants("dedicated_firewall")


# ---------------------------------------------------------------------------
# 30_all — internet egress rules (07_zone_policies/05_security_policy.yml)
#
# A hand-written permit from a tenant segment to the Internet segment
# (data/bootstrap/27_external_segments.yml: no prefix, INTERNET-ZONE). The
# firewall renders it to INTERNET-ZONE with destination any; the source leaf's
# ACL denies every NON_INTERNET_PREFIXES range before the permit to any.
# ---------------------------------------------------------------------------


class InternetEgressRule(TypedDict):
    """One permit to the Internet segment."""

    #: DC whose shared firewall context serves `source_segment`.
    dc: str
    source_segment: str
    destination_segment: str
    destination_zone: str
    protocol: str
    port: int


ALL_DEMO_INTERNET_EGRESS_RULES: dict[str, InternetEgressRule] = {
    "nordix-prod-to-internet-https": {
        "dc": "DC11",
        "source_segment": "c006-nordix-prod-p",
        "destination_segment": "internet",
        "destination_zone": "INTERNET-ZONE",
        "protocol": "tcp",
        "port": 443,
    },
}


# ---------------------------------------------------------------------------
# 30_all — inter-VRF exchange legs through the firewall contexts (PR 3)
#
# Everything below is DERIVED, not hand-counted. A context's legs are its
# tenant VRFs plus INTERNET and MANAGEMENT (EXCHANGE_PEERS): a dedicated context serves its one
# tenant, a shared context serves every customer deployment of its DC that has
# no dedicated context. A serving colocation zone
# (ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS, FR) has one shared context serving
# all its deployments, on the same rules. A deployment's VRF follows its
# environment (`p` is PROD, every other value NON-PROD); an office has no
# environment and is always PROD (OFFICE_NAMESPACE). The deployments are
# the TopologyCustomerDC footprints 30_all declares (06_customer_boarding,
# <customer>/dc/01_deployments.yml).
# ---------------------------------------------------------------------------

ALL_DEMO_TRANSIT_NAMESPACES = ("PROD", "NON-PROD", "INTERNET", "MANAGEMENT")

_TRANSIT_NAMESPACE_BY_TYPE: dict[str, str] = dict(
    zip(("prod", "non_prod", "internet", "management"), ALL_DEMO_TRANSIT_NAMESPACES)
)


# Deployment name ({org}-{environment}-{DC}) of every TopologyCustomerDC, per DC.
ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    dc: tuple(sorted(name(d) for d in _all_demo("TopologyCustomerDC") if _site(d) == dc)) for dc in ALL_DEMO_DC_NAMES
}


# The bootstrap exchanges of the removed data/bootstrap/23_exchanges.yml. The
# gateway is mandatory now, so every exchange is created per context.
ALL_DEMO_LEGACY_EXCHANGES = ("PROD-INTERNET", "NON-PROD-INTERNET", "PROD-MANAGEMENT", "NON-PROD-MANAGEMENT")


# Resource prefixes of the FW-Transit-<VRF>-IPv4 pools (data/bootstrap/20_dci_pools.yml).
ALL_DEMO_TRANSIT_RESOURCES: dict[str, str] = {
    "PROD": "100.66.0.0/20",
    "NON-PROD": "100.66.16.0/20",
    "INTERNET": "100.66.32.0/20",
    "MANAGEMENT": "100.66.48.0/20",
}


MANAGEMENT_NAMESPACE = "MANAGEMENT"

# DC11's core-services segment (07_applications/infra/00_segment.yml): INFRA-owned, in VRF MANAGEMENT,
# pinned to al-dc1111111501 Ethernet1/2, reached by the c006 udp/53 and udp/123 rules (05_security_policy.yml).
MANAGEMENT_SERVICES_PREFIX = "10.250.0.0/24"

MANAGEMENT_SERVICES_GATEWAY = "10.250.0.1/24"

MANAGEMENT_SERVICES_VLAN = 250

MANAGEMENT_SERVICES_LEAF = "al-dc1111111501"

MANAGEMENT_SERVICES_DC = "DC11"

# rule name -> (protocol, port), the DC11 permits PROD-ZONE -> MANAGEMENT-ZONE.
ALL_DEMO_MANAGEMENT_RULES: dict[str, tuple[str, int]] = {
    "nordix-prod-to-core-dns": ("udp", 53),
    "nordix-prod-to-core-ntp": ("udp", 123),
}

# The tenant segment those rules start from, read from 05_security_policy.yml.
# Its prefix and its local VLAN on each switch are derived (add_vxlan_segment:
# a /24 from its environment's tenant pool, one VLAN per VLAN domain), so
# test_64 reads both from the graph.
# Exactly one (the unpacking raises otherwise).
(MANAGEMENT_CLIENT_SEGMENT,) = sorted(
    {
        str(rule.item["source_segment"])
        for rule in declared("SecurityPolicyRule", _ALL_DEMO_ROOT)
        if rule.item.get("name") in ALL_DEMO_MANAGEMENT_RULES
    }
)


# A firewall cluster is an HA pair: one sub-interface per member per leg.
ALL_DEMO_FIREWALL_MEMBERS = 2


class ExpectedTransitContext(TypedDict):
    """What one firewall context of a 30_all site (a DC, or a serving colocation zone) must carry."""

    #: The DC (`DC10`) or the serving colocation zone (`FR2`) the context's firewalls sit in.
    site: str
    #: Tenant deployment of a dedicated context, None for the DC's shared one.
    tenant: str | None
    #: Deployments the context serves.
    deployments: tuple[str, ...]
    #: Namespace names with a leg, in ALL_DEMO_TRANSIT_NAMESPACES order.
    namespaces: tuple[str, ...]
    #: Exchange name suffixes `{A}-{Z}` (the exchange is `{context}-{A}-{Z}`).
    exchanges: tuple[str, ...]


OFFICE_NAMESPACE = _TRANSIT_NAMESPACE_BY_TYPE[PROD_TYPE]

"""The tenant VRF of every office: it has no environment, an office is always PROD."""


def deployment_environment(deployment: str) -> str:
    """`C008-D-DC12` -> `d`: the environment segment of a DC or colocation deployment name (not an office's)."""
    return deployment.split("-")[1].lower()


def deployment_namespace(deployment: str) -> str:
    """The tenant VRF namespace name of a deployment (`p` -> PROD, anything else NON-PROD)."""
    return _TRANSIT_NAMESPACE_BY_TYPE[namespace_type_for_environment(deployment_environment(deployment))]


def transit_namespace_type(namespace_name: str) -> str:
    """`NON-PROD` -> `non_prod`."""
    return next(ns_type for ns_type, name in _TRANSIT_NAMESPACE_BY_TYPE.items() if name == namespace_name)


def _expected_transit_context(site: str, tenant: str | None, deployments: tuple[str, ...]) -> ExpectedTransitContext:
    tenant_types = {namespace_type_for_environment(deployment_environment(d)) for d in deployments}
    exchanges = [
        f"{_TRANSIT_NAMESPACE_BY_TYPE[tenant_type]}-{_TRANSIT_NAMESPACE_BY_TYPE[peer]}"
        for tenant_type in _TRANSIT_NAMESPACE_BY_TYPE
        if tenant_type in tenant_types
        for peer in EXCHANGE_PEERS[tenant_type]
    ]
    peer_types = {peer for tenant_type in tenant_types for peer in EXCHANGE_PEERS[tenant_type]}
    namespaces = tuple(
        name for ns_type, name in _TRANSIT_NAMESPACE_BY_TYPE.items() if ns_type in tenant_types | peer_types
    )
    return {
        "site": site,
        "tenant": tenant,
        "deployments": deployments,
        "namespaces": namespaces,
        "exchanges": tuple(exchanges),
    }


def expected_transit_contexts() -> list[ExpectedTransitContext]:
    """The shared context of every 30_all DC, then its dedicated ones (ALL_DEMO_DEDICATED_FIREWALL_TENANTS),
    then the shared context of every serving colocation zone (never a dedicated one: no design)."""
    contexts: list[ExpectedTransitContext] = []
    for dc, deployments in ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS.items():
        dedicated = tuple(d for d in deployments if d in ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
        shared = tuple(d for d in deployments if d not in ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
        contexts.append(_expected_transit_context(dc, None, shared))
        contexts.extend(_expected_transit_context(dc, tenant, (tenant,)) for tenant in dedicated)
    for site, deployments in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS.items():
        contexts.append(_expected_transit_context(site, None, deployments))
    return contexts


ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS = expected_transit_contexts()

# PR 4d: every context with an INTERNET leg owns one SNAT /32 (FW-SNAT-INTERNET-IPv4,
# data/bootstrap/23_transit_pools.yml). EXCHANGE_PEERS gives every tenant VRF an INTERNET
# leg, so that is every context, the serving colocation zone's included.
ALL_DEMO_SNAT_POOL_PREFIX = "192.0.2.0/24"

ALL_DEMO_SNAT_ADDRESSES = sum("INTERNET" in c["namespaces"] for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)

# DC10 3+3, DC11 3+3, DC12 4+3, FR2 3 legs.
ALL_DEMO_TRANSIT_LEGS = sum(len(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)

ALL_DEMO_TRANSIT_EXCHANGES = sum(len(c["exchanges"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)

# One /29 per leg, so the /29 count per VRF is the leg count per VRF.
ALL_DEMO_TRANSIT_PREFIXES: dict[str, int] = {
    name: sum(name in c["namespaces"] for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
    for name in ALL_DEMO_TRANSIT_NAMESPACES
}

ALL_DEMO_TRANSIT_SUBINTERFACES = ALL_DEMO_TRANSIT_LEGS * ALL_DEMO_FIREWALL_MEMBERS

# Each site's member uplinks reach 2 service ports (dedicated virtual members follow
# their hosting firewall): a DC's border leafs, a serving colocation zone's two edges
# (firewall-role ports Ethernet1/15-16). Every context of the site tags both.
ALL_DEMO_BORDER_FIREWALL_PORTS = (
    len(ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS) + len(ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS)
) * ALL_DEMO_FIREWALL_MEMBERS


# Declared nowhere: the customer boarding generators create them, so the floors
# join ALL_DEMO_EXPECTED_OBJECTS here. Contexts = the shared ones + one per
# dedicated tenant; `>=` because the DC6 chain's own boarding merges into main.
ALL_DEMO_EXPECTED_OBJECTS["ManagedFirewallContext"] = ALL_DEMO_SHARED_FIREWALL_CONTEXTS + len(
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS
)

ALL_DEMO_EXPECTED_OBJECTS["TopologyRoutedExchange"] = ALL_DEMO_TRANSIT_EXCHANGES
