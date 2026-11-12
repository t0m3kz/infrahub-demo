"""Design ranges, limits, setup-data names, vendor limits and the retry policy, defined once.

A constant here mirrors the schema or ``data/bootstrap`` (``tests/unit/test_limits_consistency.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

# PAN-OS HA pairs sharing a broadcast domain require distinct group IDs (1..63).
FIREWALL_HA_GROUP_POOL = "Firewall-HA-Group-ID"
FIREWALL_HA_GROUP_MIN = 1
FIREWALL_HA_GROUP_MAX = 63

# --- 1. Design ranges and limits -------------------------------------------------------------------------------

# VLAN bands (must not overlap): 100-2999 customer (100-2499 local, 2500-2999 shared), 3000-3099 firewall contexts,
# 3100-3199 LB contexts, 3000-3799 per-VRF transit, 3800-3803 office WAN exit, 3900-3967 L3 VNI SVI (below NX-OS
# reserved 3968-4094).
CUSTOMER_VLAN_ID_MIN = 100
CUSTOMER_VLAN_ID_MAX = 2999
# Local ids (vlan_scope local) are drawn from 100-2499; a shared segment's id is SHARED_VLAN_ID_MIN + (VNI -
# GLOBAL_L2_VNI_START), in 2500-2999, so the first SHARED_VLAN_COUNT stretched segments can be shared.
LOCAL_VLAN_ID_MAX = 2499
SHARED_VLAN_ID_MIN = LOCAL_VLAN_ID_MAX + 1
SHARED_VLAN_ID_MAX = CUSTOMER_VLAN_ID_MAX
SHARED_VLAN_COUNT = SHARED_VLAN_ID_MAX - SHARED_VLAN_ID_MIN + 1
GLOBAL_L2_VNI_START = 40000
FW_CONTEXT_VLAN_START = 3000
FW_CONTEXT_VLAN_END = 3099
LB_CONTEXT_VLAN_START = 3100
LB_CONTEXT_VLAN_END = 3199
TRANSIT_VLAN_BAND = 200
L3VNI_SVI_VLAN_BASE = 3900
L3VNI_SVI_VLAN_MAX = 3967

# VNI bands (must not overlap): 10001-39999 site pools, 40000-49999 GLOBAL-L2VNI, 60000-60003 L3 VNIs, 64000-64799
# transit VNIs. An RD or RT built from a 4-byte private ASN leaves 16 bits for the VNI.
SITE_VNI_START = 10001
SITE_VNI_END = 39999
TRANSIT_VNI_BASE = 61000
MAX_ENCODABLE_VNI = 65535

# The private 32-bit ASN space (RFC 6996) of the PRIVATE-ASN32 pool; 4294967295 is reserved (RFC 7300).
PRIVATE_ASN32_START = 4200000000
PRIVATE_ASN32_END = 4294967294

# The routing tag is a 16-bit community value; 65535 is reserved, so a tag is 1..65534 (as in the schema and pools).
ROUTING_TAG_MIN = 1
ROUTING_TAG_MAX = 65534

# One MTU every platform accepts on a routed underlay port (EOS caps at 9214, NX-OS allows 9216).
UNDERLAY_MTU = 9214

# Default prefix lengths of the bootstrap pools: transit and LB leg /29, LB VIP range /26, context SNAT /32.
TRANSIT_PREFIXLEN = 29
LB_VIP_PREFIXLEN = 26
SNAT_PREFIX_LENGTH = 32

# Tenant segment prefix (utils/segment_addressing.py): with no host_count the design's length, else the smallest prefix
# that holds the hosts plus network, broadcast and gateway, between /29 and /22. SEGMENT_HOST_COUNT_MAX mirrors the
# schema max of ManagedNetworkSegment.host_count.
SEGMENT_PREFIX_LENGTH_MAX = 29
SEGMENT_PREFIX_LENGTH_MIN = 22
SEGMENT_RESERVED_ADDRESSES = 3
SEGMENT_HOST_COUNT_MAX = 2 ** (32 - SEGMENT_PREFIX_LENGTH_MIN) - SEGMENT_RESERVED_ADDRESSES

# Routed range of one cloud virtual network (generators/topology/cloud_range.py): a slice of the footprint pool.
CLOUD_RANGE_PREFIX_LENGTH = 29

# Host offsets inside a transit /29 and the segment anycast gateway: .1 anycast gateway, .2/.3 reserved, .4 VIP, .5/.6
# HA members. The largest offset must be a host of TRANSIT_PREFIXLEN.
OFFSET_ANYCAST = 1
OFFSET_VIP = 4
OFFSET_MEMBER_A = 5
OFFSET_MEMBER_B = 6

# Leaf ACL sequence numbers: own rules by index (100, 110, ... below 4000), return legs from ACL_RETURN_LEG_SEQ_START,
# the internet-zone "outside" band from ACL_INTERNET_BAND_SEQ_START, the closing rule at ACL_CLOSING_SEQ or above.
ACL_RETURN_LEG_SEQ_START = 5000
ACL_INTERNET_BAND_SEQ_START = 6000
ACL_CLOSING_SEQ = 9990

# A circuit has two ends (two ports, at most two facilities).
CIRCUIT_ENDS = 2

# --- 2. Vendor and platform limits -----------------------------------------------------------------------------

# The longest VLAN name NX-OS (and IOS XE, EOS) accepts; a longer one is cut with a digest.
VLAN_NAME_MAX = 32
# The shortest provider limit on a load balancer name (AWS ALB/NLB); a longer one is cut with a digest.
CLOUD_LB_NAME_MAX = 32
# An IOS XE ACL `remark` holds at most 100 characters (transforms/helpers/cisco_xe.py).
XE_REMARK_MAX = 100
# The segments one Arista EOS device can define across all VRFs.
EOS_MSSG_MAX_SEGMENTS = 60

# BGP maximum-prefix (utils/bgp_expectation.py): limits are derived from the model; the slack is added to a set-sized
# limit; the ceiling is the 32-bit counter (U: no vendor guide), a derived limit above it is not rendered.
BGP_PREFIX_SLACK = 2
BGP_MAX_PREFIX_CEILING = 2**32 - 1

# --- 3. Setup data: pool and group names -----------------------------------------------------------------------

# Pools and groups loaded by data/bootstrap and found by name. A name with ``{namespace}`` is a template (PROD,
# NON-PROD, INTERNET, MANAGEMENT) filled in by the builders.

# The underlay namespace (Infrahub's built-in): the global routing table, never a VRF instance or transit leg.
DEFAULT_NAMESPACE = "default"

# Prefix pools (17_ip_prefix_pools.yml) every DC, colocation and office slices its own pools from.
TECHNICAL_IPV4_POOL = "Technical-IPv4"
LOOPBACK_IPV4_POOL = "Loopback-IPv4"
MANAGEMENT_IPV4_POOL = "Management-IPv4"
LOOPBACK_IPV6_POOL = "Loopback-IPv6"
TECHNICAL_IPV6_POOL = "Technical-IPv6"
# The MLAG control session of a domain (17_ip_prefix_pools.yml).
MLAG_CONTROL_IPV4_POOL = "MLAG-Control-IPv4"
MLAG_CONTROL_IPV6_POOL = "MLAG-Control-IPv6"
# The DCI point-to-point links (20_dci_pools.yml).
DCI_TECHNICAL_IPV4_POOL = "DCI-Technical-IPv4"
DCI_TECHNICAL_IPV6_POOL = "DCI-Technical-IPv6"
# Per VRF namespace: firewall transit and LB leg /29s, LB VIP /26s, tenant pools (IPv4, IPv6) and handoff /31s. Transit
# and handoff exist for all four namespaces, LB and tenant pools for PROD and NON-PROD.
FW_TRANSIT_POOL_TEMPLATE = "FW-Transit-{namespace}-IPv4"
LB_LEG_POOL_TEMPLATE = "LB-Leg-{namespace}-IPv4"
LB_VIP_POOL_TEMPLATE = "LB-VIP-{namespace}-IPv4"
TENANT_POOL_TEMPLATE = "Tenant-{namespace}-IPv4"
TENANT_POOL_TEMPLATE_IPV6 = "Tenant-{namespace}-IPv6"
# A customer's own pool, created when its footprint is boarded (not bootstrap data): lowercase owner org_id,
# environment, facility, family.
CUSTOMER_POOL_NAME_FORMAT = "{owner}-{environment}-{facility}-{family}"
HANDOFF_POOL_TEMPLATE = "Handoff-{namespace}-IPv4"
# The public SNAT address of a firewall context (an address pool in the INTERNET namespace).
FW_SNAT_POOL = "FW-SNAT-INTERNET-IPv4"

# Number pools: the stretched-segment VNI, the private 32-bit ASNs and the routing tags.
STRETCHED_VNI_POOL = "GLOBAL-L2VNI"
PRIVATE_ASN32_POOL = "PRIVATE-ASN32"
CUSTOMER_ROUTING_TAG_POOL = "Customer-Routing-Tag"
SEGMENT_ROUTING_TAG_POOL = "Segment-Routing-Tag"

# Standard groups (00_groups.yml) generators put objects into or read.
GROUP_HA_DOMAINS = "ha_domains"
GROUP_MLAG_DOMAINS = "mlag_domains"
GROUP_APP_COMPONENTS = "app_components"
GROUP_CARRIER_CIRCUITS = "carrier_circuits"
# Every session circuit: the target group of add_virtual_circuit.
GROUP_VIRTUAL_CIRCUITS = "virtual_circuits"
# Devices that are never a border gateway of a VLAN domain.
GROUP_SDWAN_GATEWAYS = "sdwan_gateways"
GROUP_PROVIDER_STUBS = "provider_stubs"
# Every DcimPatchPanel: the target group of add_patch_panel.
GROUP_PATCH_PANELS = "patch_panels"
# Every firewall and load-balancer context: the target group of add_context_routes.
GROUP_SERVICE_CONTEXTS = "service_contexts"
# Every segment deployment add_vxlan_segment builds: the target group of add_segment_deployment_ports.
GROUP_SEGMENT_DEPLOYMENTS = "segment_deployments"

# device_role -> group, for roles whose group is not f"{device_role}s" (one map for every create_devices() caller).
ROLE_GROUP_NAMES: dict[str, str] = {
    "internal-firewall": "firewalls",
    "external-firewall": "firewalls",
    "load-balancer": "loadbalancers",
    "access-switch": "access-switches",
    "distribution-switch": "distribution-switches",
}

# orchestrator -> the group its artifact definitions target (generators/topology/orchestrator_routing.py).
TOPOLOGY_ORCHESTRATOR_GROUPS: dict[str, str] = {
    "ansible_automation_platform": "topology_ansible",
    "github_actions": "topology_github_actions",
    "argocd": "topology_argocd",
}
APPLICATION_ORCHESTRATOR_GROUPS: dict[str, str] = {
    "ansible_automation_platform": "application_ansible",
    "github_actions": "application_github_actions",
    "argocd": "application_argocd",
}

# --- 4. Retry and polling policy -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """``attempts`` tries; ``delay`` is the base wait between two of them, in seconds (see ``retry_delay``)."""

    attempts: int
    delay: float


@dataclass(frozen=True)
class PollPolicy:
    """A poll of a remote task: check every ``interval`` seconds, give up after ``timeout`` seconds."""

    interval: int
    timeout: int


@dataclass(frozen=True)
class ResourceLockPolicy(RetryPolicy):
    """A lock node held by a run: ``stale_after`` seconds old and it is treated as abandoned."""

    stale_after: int


# Wait for data another generator of the same load is still writing: ten tries from 3 s.
WAIT_FOR_PEER_GENERATOR = RetryPolicy(attempts=10, delay=3.0)

# Wait for a generator run in flight (Infrahub task poll): every 3 s, at most 30 min.
PARENT_GENERATOR_WAIT = PollPolicy(interval=3, timeout=1800)

# Resource lock of cabling and IP allocation. Waiters must keep trying longer than stale_after, or a failed release
# blocks them.
RESOURCE_LOCK = ResourceLockPolicy(attempts=40, delay=2.0, stale_after=300)

# A save that races onto the same shared node (NODE_NOT_FOUND) is retried.
NODE_SAVE_RETRY = RetryPolicy(attempts=5, delay=2.0)

# Re-read of load balancer inputs another generator may still write: fixed interval.
LB_VIP_REREAD = RetryPolicy(attempts=12, delay=5.0)

# add_sdwan_edge runs beside the office fabric generator: wait for the edge it creates.
OFFICE_EDGE_WAIT = RetryPolicy(attempts=8, delay=3.0)

# Short on purpose: most pods host no border-leaf, so this fires on every pod bootstrap.
BORDER_LEAF_WAIT = RetryPolicy(attempts=3, delay=2.0)

# Retries of a security policy rule save whose generated index collided.
RULE_SAVE_ATTEMPTS = 5
