"""Group contracts of one fabric, deny by default: the segments' rules as rows between group tags.

One builder for every reader (the segment_security consistency check and
the device renderers).

Groups (``segment_groups``):

- a **segment group** per routed tenant segment (VLAN/VXLAN, gateway in a
  PROD/NON-PROD namespace) with its SecurityTag; its hosts are classified by
  the segment (VLAN, VNI or prefix). Intra-group traffic is permitted, except
  in a microsegmented segment, where it is denied and only component
  contracts open it;
- a **component group** per tagged AppComponent of a microsegmented segment
  (its instances talk to each other freely: intra-group permit);
- ``UNCLASSIFIED``, a pseudo-group: everything no selector classifies.

Rows (``get_vrf_contracts``), per VRF and ordered (source, destination) pair,
each a permit of exactly the listed (protocol, ports); the VRF is
``DEFAULT_ACTION`` (deny), so a pair without a row is denied. Every row comes
from an enabled permit SecurityPolicyRule (``segment_flows``, the one rule
view of the leaf ACL, the PBR bypass and the firewall):

- a rule between two tagged segments of one VRF: the source side's groups
  to the destination side's (``rule_groups``), bypassed or
  firewall-inspected alike. A side that names a component tag is that
  component's group only: an intra-segment rule of a microsegmented segment,
  and a component dependency into or out of one (the application generator
  writes the tag of every microsegmented side, RulesPlanner.rule_tags), so a
  dependency on one component opens that component, not its neighbours.
  Any other side is every group of its segment, like the SVI ACL;
- **service legs.** The firewall and load-balancer legs and the transit
  VLANs are never classified (their SVIs are shared by contexts and
  customers), so a flow that may cross a firewall context (PBR redirects
  every inter-segment flow by default, the leaf-local transit leg) gets the
  same entries on both legs: source groups -> UNCLASSIFIED and UNCLASSIFIED ->
  destination groups. Whether the SGACL sees the original destination or the
  firewall's next hop (U) then gives the same verdict;
- a rule with one side outside the VRF's tagged segments (internet,
  MANAGEMENT services, another VRF, a cloud, a prefix selector, an untagged
  segment): the tagged side to or from UNCLASSIFIED, since such traffic
  reaches the fabric through a firewall exchange leg or a border;
- **load-balancer legs.** A VIP and a context's leg are never groups: the
  client's rule to the VIP /32 is client -> UNCLASSIFIED, and the rule of the
  segment-less policy ``lb-<context>`` (leg /29 -> backend segment, read from
  the backend's ``inbound_rules``) is UNCLASSIFIED -> backend;
- nothing else. Two customers' segments with no rule between them have no
  row: denied. A deny rule is no row (the default denies); a deny ordered
  before an overlapping permit in one policy is not modelled (none in data).
  A policy whose default action is permit is not mirrored (none in data):
  group policy stays deny by default.

Platform views map the neutral rows onto one enforcement model:

- ``vxlan_gpo_view`` (NX-OS GPO): group IDs, the profile's reserved groups
  (``unclassified_group_id``, ``unaware_group_id``: data, never a number
  here), and the **policy-unaware rule**: traffic from a site outside the
  reference fabrics (DC12 under DCNM, a colocation BGW) arrives with the
  unaware group, so every row end whose segment is also active on such a site
  gets the same row with the unaware group, and a stretched group with
  intra-group permit gets unaware <-> group permit-all (its other half). The
  unaware group cannot tell customers apart: the union of every such row is
  what any policy-unaware source may send (the reference_fabric check warns
  about each stretched segment; L0 still holds at the source SVI);
- ``segment_security_view`` (EOS MSS-G): segments by prefix, policies under
  the destination segment (``from <source>``), UNCLASSIFIED as the
  destination's fallback policy. MSS-G classifies remote sources by prefix
  too, so it needs no unaware group, but every segment a ``from`` line names
  must be defined, so the view lists the local segments and every segment a
  listed row names. It only enforces traffic to a segment, so a row to
  UNCLASSIFIED is no policy; it is kept per source segment as ``to_unclassified``
  because MSS-G is stateless and the reply of such a flow is traffic from
  UNCLASSIFIED to that segment. A prefix cannot tell a component from its
  segment, so a row that names a component group on either side is
  ``unsupported`` (never widened to the whole segment: that would be fail-open). The reply of a
  permit-all row is ``unsupported`` too: a stateless device matches a reply by source port and
  ``any`` has none, so that direction stays closed (never widened to a forward-all policy); and the
  reply of a flow whose every rule is unsupported is dropped.

Rows are forward only: replies are the renderer's (``REPLIES``). Sorted,
de-duplicated and id-free.

The device renderers read the same contracts through one dispatcher, ``group_policy_device_view``
(the gate, the data center's contracts once, then the builder of the data center's kind, ``GPO_RENDERERS``):
``_vxlan_gpo_build`` for NX-OS and ``_segment_security_build`` for EOS MSS-G (their
docstrings say how). ``_vxlan_gpo_build`` is the NX-OS VTEP configs' reader: the ``vxlan_gpo_view`` of the device's
own data center, cut to the VRFs the device carries (a VTEP with none of them still
enables GPO, with no groups or contracts). Every VTEP of the data
center renders the same class-maps, policy-maps and contracts for a VRF; only the
selectors differ, since they name things of one device: ``match vlan`` for a segment
group (a VLAN ID is per VLAN domain) and ``match vlan <vlan> interface <port>`` for a
component group, rendered on the VTEP that owns the host-facing port only.
It renders nothing (None) unless the data center is a reference fabric of a
kind with a renderer, and refuses a row it cannot number, a tag shared by two
segments and a group named like a pseudo-group (``... refusing unprotected
config``).

A component group is classified by its instances' ports (``component_coverage``): the
host an instance is cabled through (utils/app_instances.physical_host, the one the
generator tags ports for) and the VTEP port at the far end of each uplink or bond
member, that carries the component's segment. No IP is used (an instance has none, an
address is spoofable and IPv4 only). A VRF is left unenforced, with the reasons as a
comment, while a component group it names cannot be placed (coverage blockers: an
instance with no host, not cabled to a VTEP, cabled to a switch that is no VTEP or to a
VTEP port that does not carry the segment, a cable without exactly one far end, a bond
member with no port-channel, a port shared by two components of one segment) or a tenant
segment it holds on the device has no classifier.

The coverage part of the verdict is the data center's, identical on every VTEP of a VRF;
the per-device part is not: a claimed port that THIS device lacks, has untagged or has no
VLAN for (``_selectors`` problems) leaves its VRF unenforced here while the other vPC peer
enforces it. An enforced VRF that relies on component classification lists the claimed
ports (data center wide) in a documented-not-lab-verified comment on its enforce line.
"""

from transforms.helpers.group_policy.dispatch import _BUILDERS, GPO_CONFIG_KEYS, GPO_RENDERERS, group_policy_device_view
from transforms.helpers.group_policy.eos import MSSG_MAX_SEGMENTS, _MssgCatalog, segment_security_view
from transforms.helpers.group_policy.model import (
    DEFAULT_ACTION,
    FALLBACK,
    REPLIES,
    UNAWARE,
    UNCLASSIFIED,
    DataCenterContracts,
    Entry,
    Group,
    data_center_contracts,
    get_vrf_contracts,
    rule_groups,
    segment_groups,
    segment_vrf,
)
from transforms.helpers.group_policy.nxos import (
    ComponentClaim,
    ComponentCoverage,
    class_map_name,
    component_coverage,
    vxlan_gpo_view,
)

__all__ = [
    "DEFAULT_ACTION",
    "FALLBACK",
    "GPO_CONFIG_KEYS",
    "GPO_RENDERERS",
    "MSSG_MAX_SEGMENTS",
    "REPLIES",
    "UNAWARE",
    "UNCLASSIFIED",
    "ComponentClaim",
    "ComponentCoverage",
    "DataCenterContracts",
    "Entry",
    "Group",
    "_BUILDERS",
    "_MssgCatalog",
    "class_map_name",
    "component_coverage",
    "data_center_contracts",
    "get_vrf_contracts",
    "group_policy_device_view",
    "rule_groups",
    "segment_groups",
    "segment_security_view",
    "segment_vrf",
    "vxlan_gpo_view",
]
