"""Design ranges and limits, setup-data names, vendor limits and the retry policy, defined once.

The schema (``min_value``/``max_value``) and the bootstrap data (``data/bootstrap``) stay the source of truth for what
Infrahub stores; a constant here is a MIRROR that code needs at design time (sizing, validation, rendering), and
``tests/unit/test_limits_consistency.py`` checks that every mirror equals what the schema and the bootstrap data say.
Change a value in the data or the schema and that test names the constant to change here.

Sections (generators, transforms and checks import from here; a value used by one module only stays next to its code):

1. Design ranges and limits (VLAN, VNI, ASN, routing tag, MTU, prefix lengths)
2. Vendor and platform limits (name lengths, segment counts)
3. Setup data: the pool and group names the bootstrap data must define
4. Retry and polling policy
"""

from __future__ import annotations

from dataclasses import dataclass

# --- 1. Design ranges and limits -------------------------------------------------------------------------------

# VLAN bands of a border leaf's VLAN table (get_vlans de-duplicates by VLAN ID, so no two bands may overlap):
#
#   100-2999    customer VLANs of every local VLAN domain (generators/vlan_domain.py). 3968-4094 is NX-OS's default
#               internally reserved band (4094 the conventional vPC peer-keepalive VLAN), so the space above 2999
#               is not customer space.
#   3000-3099   FirewallContext VLANs (one context = one VLAN on the firewall uplink and the border-leaf service port)
#   3100-3199   LoadbalancerContext VLANs; the other half of the context band, so the two kinds' legs never share a tag
#   3000-3799   the per-VRF transit VLANs: a context VLAN + TRANSIT_VLAN_BAND per namespace slot
#   3800-3803   the WAN exit tags of an office (utils/exchange_transit.py WAN_EXIT_VLAN_BASE)
#   3900-3967   the local VLAN of the per-VRF L3 VNI SVI NX-OS needs for symmetric IRB (transforms/helpers/overlay.py);
#               above all of the above and below NX-OS's reserved 3968-4094
CUSTOMER_VLAN_ID_MIN = 100
CUSTOMER_VLAN_ID_MAX = 2999
FW_CONTEXT_VLAN_START = 3000
FW_CONTEXT_VLAN_END = 3099
LB_CONTEXT_VLAN_START = 3100
LB_CONTEXT_VLAN_END = 3199
TRANSIT_VLAN_BAND = 200
L3VNI_SVI_VLAN_BASE = 3900
L3VNI_SVI_VLAN_MAX = 3967

# VNI bands (a VNI is one flat 24-bit field on the wire with no L2/L3 discriminator, so bands must not overlap):
#
#   10001-39999   L2 VNI of every site's own ``{site}-vni-pool`` (a DC's, a colocation metro's, an office's)
#   40000-49999   L2 VNI GLOBAL-L2VNI, stretched segments (data/bootstrap/18_vni_pools.yml, not mirrored: used by name)
#   60000-60003   the fixed L3 VNIs of the four namespaces (data/bootstrap/22_namespaces.yml)
#   64000-64799   the exchange transit VNIs, TRANSIT_VNI_BASE + the transit VLAN (utils/exchange_transit.py)
#
# A route-distinguisher or route-target built from a 4-byte private ASN leaves 16 bits for the VNI.
SITE_VNI_START = 10001
SITE_VNI_END = 39999
TRANSIT_VNI_BASE = 61000
MAX_ENCODABLE_VNI = 65535

# The private 32-bit ASN space (RFC 6996) of the PRIVATE-ASN32 pool (data/bootstrap/16_asn_pools.yml); 4294967295 is
# reserved (RFC 7300). Fabric ASN ranges are placed in it by generators/helpers/pools.py.
PRIVATE_ASN32_START = 4200000000
PRIVATE_ASN32_END = 4294967294

# The routing tag is the 16-bit value behind a standard community; 65535 is reserved, so a tag is 1..65534. Schema
# parameters of OrganizationCustomer.routing_tag and ManagedNetworkSegment.routing_tag and the Customer-Routing-Tag /
# Segment-Routing-Tag pools (data/bootstrap/19c_routing_tag_pools.yml) say the same.
ROUTING_TAG_MIN = 1
ROUTING_TAG_MAX = 65534

# One MTU every platform accepts on a routed underlay port: Arista EOS caps a routed port at 9214, NX-OS allows 9216,
# so the vendor-neutral model uses 9214. The DCI ports and sessions (generators/topology/virtual_circuit.py) and the
# NX-OS L3 VNI SVI (rendered from the transform context ``underlay_mtu``) use it.
UNDERLAY_MTU = 9214

# Prefix lengths of the pools the bootstrap data defines (``default_prefix_length`` of the pool): a firewall transit
# or load-balancer leg /29 (23_transit_pools.yml), a load-balancer context's routed VIP range /26 (the same file),
# a context's SNAT address /32 (FW-SNAT-INTERNET-IPv4).
TRANSIT_PREFIXLEN = 29
LB_VIP_PREFIXLEN = 26
SNAT_PREFIX_LENGTH = 32

# The host offsets inside a transit /29 (utils/exchange_transit.py, generators/firewall_transit.py,
# transforms/helpers/ha.py, checks/common.py) and the tenant segment's anycast gateway: .1 the border-leaf / edge
# anycast gateway, .2/.3 reserved, .4 the firewall or load-balancer VIP, .5/.6 the HA members. The largest offset
# must be a host of TRANSIT_PREFIXLEN.
OFFSET_ANYCAST = 1
OFFSET_VIP = 4
OFFSET_MEMBER_A = 5
OFFSET_MEMBER_B = 6

# Sequence numbers of a segment's leaf ACL (transforms/helpers/acl.py, transforms/helpers/firewall.py): its own rules
# are numbered by rule index (generators/segment_firewall.py: 100, 110, ... below 4000), the return legs of the
# permits into the segment start at ACL_RETURN_LEG_SEQ_START, the "outside" band of a permit to a prefix-less
# external segment (the internet) follows at ACL_INTERNET_BAND_SEQ_START, and the closing rule never sits below
# ACL_CLOSING_SEQ.
ACL_RETURN_LEG_SEQ_START = 5000
ACL_INTERNET_BAND_SEQ_START = 6000
ACL_CLOSING_SEQ = 9990

# A circuit has two ends (two ports, at most two facilities): checks/circuit_placement.py and
# checks/physical_circuit_ports.py.
CIRCUIT_ENDS = 2

# --- 2. Vendor and platform limits -----------------------------------------------------------------------------

# The longest VLAN name NX-OS (and IOS XE, EOS) accepts; a longer one is cut with a digest (transforms/helpers/firewall.py).
VLAN_NAME_MAX = 32
# The shortest provider limit on a load balancer name (AWS ALB/NLB: 32); a longer one is cut with a digest
# (generators/helpers/lb_vip.py).
CLOUD_LB_NAME_MAX = 32
# An IOS XE ACL `remark` holds at most 100 characters (transforms/helpers/cisco_xe.py).
XE_REMARK_MAX = 100
# The segments one Arista EOS device can define across all VRFs (spec 1.4, D: the manual's limit;
# transforms/helpers/group_policy/eos.py).
EOS_MSSG_MAX_SEGMENTS = 60

# --- 3. Setup data: pool and group names -----------------------------------------------------------------------

# The pools and groups below are loaded by data/bootstrap and found by name; ``tests/unit/test_limits_consistency.py``
# checks that every name exists there. A pool name that carries a VRF namespace is a TEMPLATE (``{namespace}`` is the
# namespace name: PROD, NON-PROD, INTERNET, MANAGEMENT); the builders (``utils/exchange_transit.py``
# ``transit_pool_name``/``lb_leg_pool_name``/``lb_vip_pool_name``, ``generators/topology/segment.py``
# ``tenant_pool_name``, ``generators/topology/virtual_circuit.py`` ``handoff_pool_name``) fill it in.

# Prefix pools (17_ip_prefix_pools.yml): the supernets every DC, colocation and office slices its own pools from.
TECHNICAL_IPV4_POOL = "Technical-IPv4"
LOOPBACK_IPV4_POOL = "Loopback-IPv4"
MANAGEMENT_IPV4_POOL = "Management-IPv4"
LOOPBACK_IPV6_POOL = "Loopback-IPv6"
TECHNICAL_IPV6_POOL = "Technical-IPv6"
# The MLAG control session of a domain (17_ip_prefix_pools.yml), by address family in generators/mlag.py.
MLAG_CONTROL_IPV4_POOL = "MLAG-Control-IPv4"
MLAG_CONTROL_IPV6_POOL = "MLAG-Control-IPv6"
# The DCI point-to-point links (20_dci_pools.yml), by address family in generators/topology/virtual_circuit.py.
DCI_TECHNICAL_IPV4_POOL = "DCI-Technical-IPv4"
DCI_TECHNICAL_IPV6_POOL = "DCI-Technical-IPv6"
# Per VRF namespace: the firewall transit /29 legs, the load-balancer /29 legs and /26 VIP ranges (23_transit_pools.yml),
# the tenant segment /24s (23b_tenant_pools.yml) and the handoff /31 links (26_handoff_pools.yml). Transit and handoff
# exist for all four namespaces, the load-balancer and tenant pools for the two tenant ones (PROD, NON-PROD).
FW_TRANSIT_POOL_TEMPLATE = "FW-Transit-{namespace}-IPv4"
LB_LEG_POOL_TEMPLATE = "LB-Leg-{namespace}-IPv4"
LB_VIP_POOL_TEMPLATE = "LB-VIP-{namespace}-IPv4"
TENANT_POOL_TEMPLATE = "Tenant-{namespace}-IPv4"
HANDOFF_POOL_TEMPLATE = "Handoff-{namespace}-IPv4"
# The public SNAT address of a firewall context (23_transit_pools.yml, an address pool in the INTERNET namespace).
FW_SNAT_POOL = "FW-SNAT-INTERNET-IPv4"

# Number pools: the VNI every stretched segment shares (18_vni_pools.yml), the private 32-bit ASNs (16_asn_pools.yml)
# and the customer and segment routing tags (19c_routing_tag_pools.yml).
STRETCHED_VNI_POOL = "GLOBAL-L2VNI"
PRIVATE_ASN32_POOL = "PRIVATE-ASN32"
CUSTOMER_ROUTING_TAG_POOL = "Customer-Routing-Tag"
SEGMENT_ROUTING_TAG_POOL = "Segment-Routing-Tag"

# Standard groups (00_groups.yml) the generators put objects into or read membership of.
GROUP_HA_DOMAINS = "ha_domains"
GROUP_MLAG_DOMAINS = "mlag_domains"
GROUP_APP_COMPONENTS = "app_components"
GROUP_PHYSICAL_CIRCUITS = "physical_circuits"
GROUP_CLOUD_LOADBALANCERS = "cloud_loadbalancers"
# Devices that are never a border gateway of a VLAN domain: the SD-WAN hubs and the provider stub routers.
GROUP_SDWAN_GATEWAYS = "sdwan_gateways"
GROUP_PROVIDER_STUBS = "provider_stubs"

# device_role -> the group its devices join, for roles whose group does not follow the f"{device_role}s" convention.
# One map for every create_devices() caller: a group created on a branch because a caller missed it collides with
# every other branch that creates it when both merge.
ROLE_GROUP_NAMES: dict[str, str] = {
    "load-balancer": "loadbalancers",
    "access-switch": "access-switches",
    "distribution-switch": "distribution-switches",
}

# infrastructure_orchestrator / application_orchestrator -> the group the artifact definitions of that orchestrator
# target (generators/topology/orchestrator_routing.py). A customer footprint, an application and a provider-hosted
# colocation each have their own set.
TOPOLOGY_ORCHESTRATOR_GROUPS: dict[str, str] = {
    "ansible_automation_platform": "topology_ansible",
    "github_actions": "topology_github_actions",
    "terraform": "topology_terraform",
    "argocd": "topology_argocd",
}
APPLICATION_ORCHESTRATOR_GROUPS: dict[str, str] = {
    "ansible_automation_platform": "application_ansible",
    "github_actions": "application_github_actions",
    "argocd": "application_argocd",
}
VIRTUAL_COLOCATION_ORCHESTRATOR_GROUPS: dict[str, str] = {"terraform": "virtual_colocation_terraform"}

# --- 4. Retry and polling policy -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """``attempts`` tries in total; ``delay`` is the base of the wait between two of them, in seconds.

    The generators wait ``generators.helpers.common.retry_delay(delay, attempt)`` (jittered exponential backoff
    from ``delay``, capped); ``LB_VIP_REREAD`` is the one fixed-interval poll.
    """

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


# Wait for data that another generator of the same load is still writing (an interface, a pool, a device, a
# shared routing object): ten tries from 3 s.
WAIT_FOR_PEER_GENERATOR = RetryPolicy(attempts=10, delay=3.0)

# Wait for a generator run in flight to finish (Infrahub task poll): every 3 s, for at most 30 min.
PARENT_GENERATOR_WAIT = PollPolicy(interval=3, timeout=1800)

# The resource lock node of the cabling and IP allocation; stale after longer than any single realistic cabling run.
# A waiter backs off to 10 s (generators/pools.py), so it must keep trying for longer than stale_after: a lock whose
# release failed is only reclaimed once it is that old, and waiters that gave up sooner would all fail on it.
RESOURCE_LOCK = ResourceLockPolicy(attempts=40, delay=2.0, stale_after=300)

# A save that races a write onto the same shared node (NODE_NOT_FOUND) is retried.
NODE_SAVE_RETRY = RetryPolicy(attempts=5, delay=2.0)

# Re-read of load balancer inputs another generator may still be writing: fixed interval, not backoff.
LB_VIP_REREAD = RetryPolicy(attempts=12, delay=5.0)

# add_sdwan_edge runs beside the office fabric generator: wait for the edge it creates (an office with an
# sdwan_gateway only).
OFFICE_EDGE_WAIT = RetryPolicy(attempts=8, delay=3.0)

# Short on purpose: most pods host no border-leaf, so this fires on every pod's bootstrap.
BORDER_LEAF_WAIT = RetryPolicy(attempts=3, delay=2.0)

# Tries to save a security policy rule whose generated index collided; each retry re-reads the index at once.
RULE_SAVE_ATTEMPTS = 5
