"""GraphQL queries of the 30_all interconnect and tenant-service steps (test_62, test_62b, test_62c)."""

SCENARIO_NAME = "Scenario 30_all: Interconnects"


COLOCATION_CONTEXTS_QUERY = """
query {
  TopologyCustomerColocation {
    edges {
      node {
        name { value }
        serving_zone {
          node {
            name { value }
            devices(role__value: "firewall") { edges { node { id } } }
          }
        }
        serving_firewall_context {
          node {
            name { value }
            vlan_id { value }
            tenant { node { id } }
            cluster { node { ... on ManagedFirewallHA { capabilities { edges { node { id } } } } } }
            capabilities { edges { node { name { value } } } }
          }
        }
      }
    }
  }
}
"""


# The BGP sessions by name: the <vc>-underlay / <vc>-evpn of an Interconnect and the <vc>-bgp of a Handoff (both
# add_virtual_circuit), and the FABRIC-<a>-<b> of a zone edge pair. A session's interfaces are its `capabilities`: the
# physical ports of the DCI underlay, the VTEP loopbacks of the DCI EVPN session, the two sub-interfaces of a Handoff.
# A process names its device through its own `capabilities`.
SESSIONS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) {
    edges {
      node {
        name { value }
        status { value }
        peering_role { value }
        session_type { value }
        ttl { value }
        bfd_enabled { value }
        send_extended_community { value }
        maximum_routes { value }
        password { node { name { value } } }
        address_families { edges { node { afi { value } safi { value } } } }
        bgp_processes {
          edges {
            node {
              name { value }
              process_role { value }
              local_as { node { asn { value } } }
              capabilities { edges { node { name { value } } } }
            }
          }
        }
        capabilities {
          edges {
            node {
              name { value }
              device { node { name { value } } }
              ... on DcimPhysicalInterface {
                description { value }
                status { value }
                mtu { value }
                ip_address { node { address { value } ip_namespace { node { name { value } } } } }
              }
              ... on DcimVirtualInterface {
                description { value }
                status { value }
                role { value }
                parent_interface { node { name { value } } }
                ip_address { node { address { value } ip_namespace { node { name { value } } } } }
              }
            }
          }
        }
      }
    }
  }
}
"""


# The /31 (/127) add_virtual_circuit allocates per circuit, found by its description ("DCI <name>", "Handoff <name>").
CIRCUIT_PREFIXES_QUERY = """
query ($descriptions: [String]) {
  IpamPrefix(description__values: $descriptions) {
    edges {
      node {
        prefix { value }
        description { value }
        role { value }
        ip_namespace { node { name { value } } }
      }
    }
  }
}
"""


# The members of a group, with their kind.
GROUP_MEMBERS_QUERY = """
query ($name: String!) {
  CoreGroup(name__value: $name) {
    edges { node { members { edges { node { id __typename display_label } } } } }
  }
}
"""


# The interlink ports of the zone edge pairs: role and /127 address.
EDGE_INTERLINK_INTERFACES_QUERY = """
query ($devices: [String], $name: String) {
  DcimPhysicalInterface(device__name__values: $devices, name__value: $name) {
    edges {
      node {
        id
        name { value }
        role { value }
        device { node { name { value } } }
        ip_address { node { address { value } } }
      }
    }
  }
}
"""


# Every cable touching the given interfaces.
EDGE_INTERLINK_CABLES_QUERY = """
query ($ids: [ID]) {
  DcimCable(endpoints__ids: $ids) {
    edges {
      node {
        id
        endpoints { edges { node { id } } }
      }
    }
  }
}
"""


# The office WAN exit sessions add_office_fabric builds toward the SD-WAN edge (the VCO describes the edge's side
# from the same peering): processes with their AS, the key, and the two dot1q sub-interfaces with their addresses.
WAN_EXIT_SESSIONS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) {
    edges {
      node {
        name { value }
        peering_role { value }
        session_type { value }
        ttl { value }
        password { node { name { value } } }
        address_families { edges { node { afi { value } safi { value } } } }
        bgp_processes {
          edges { node { name { value } process_role { value } local_as { node { asn { value } } } } }
        }
        capabilities {
          edges {
            node {
              name { value }
              device { node { name { value } } }
              ... on DcimVirtualInterface {
                parent_interface { node { name { value } } }
                ip_address { node { address { value } ip_namespace { node { name { value } } } } }
              }
            }
          }
        }
      }
    }
  }
}
"""


# The zone-owned slice of Technical-IPv6 the interlink /127 comes from.
EDGE_PAIR_POOL_QUERY = """
query ($name: String) {
  CoreIPPrefixPool(name__value: $name) {
    edges {
      node {
        name { value }
        resources { edges { node { ... on BuiltinIPPrefix { prefix { value } } } } }
      }
    }
  }
}
"""


# The loopback100 Multi-Site VIP of both edges of a zone's pair: one shared address object.
EDGE_PAIR_VIP_QUERY = """
query ($devices: [String]) {
  DcimVirtualInterface(device__name__values: $devices, name__value: "loopback100") {
    edges {
      node {
        role { value }
        device { node { name { value } } }
        ip_address { node { id address { value } } }
      }
    }
  }
}
"""


# The ISP transit sessions: which edge each runs on, and the namespace of its local address.
TRANSIT_SESSIONS_QUERY = """
query ($names: [String]) {
  ManagedBGPPeering(name__values: $names) {
    edges {
      node {
        name { value }
        session_type { value }
        capabilities {
          edges {
            node {
              name { value }
              device { node { name { value } } }
              ... on DcimPhysicalInterface {
                ip_address { node { address { value } ip_namespace { node { name { value } namespace_type { value } } } } }
              }
            }
          }
        }
      }
    }
  }
}
"""

# A rule's match: its policy, segments, the VIP/32s it targets (R_client) and the prefixes it starts
# from (R_server: the load-balancer context's leg).
_RULE_FIELDS = """
        name { value }
        action { value }
        protocol { value }
        port_start { value }
        disabled { value }
        policy { node { name { value } } }
        source_segment { node { name { value } } }
        destination_segment { node { name { value } } }
        destination_ip_addresses { edges { node { address { value } } } }
        source_prefixes { edges { node { prefix { value } } } }
"""

RULES_BY_POLICY_QUERY = (
    "query ($names: [String]) { SecurityPolicyRule(policy__name__values: $names) { edges { node {"
    + _RULE_FIELDS
    + "} } } }"
)

RULES_BY_SOURCE_SEGMENT_QUERY = (
    "query ($names: [String]) { SecurityPolicyRule(source_segment__name__values: $names) { edges { node {"
    + _RULE_FIELDS
    + "} } } }"
)

# The cloud load balancers of the cloud load_balanced components, and their VIPs.
CLOUD_LOAD_BALANCERS_QUERY = """
query ($names: [String]) {
  CloudLoadBalancer(name__values: $names) {
    edges {
      node {
        name { value }
        lb_type { value }
        scheme { value }
        virtual_network { node { name { value } } }
        network_segments { edges { node { name { value } } } }
        member_of_groups { edges { node { name { value } } } }
        vips { edges { node { id } } }
      }
    }
  }
  LoadbalancerVIP(load_balancer__name__values: $names) {
    edges {
      node {
        hostname { value }
        protocol { value }
        port { value }
        load_balancer { node { name { value } } }
        vip_ip { node { address { value } } }
        component { node { fqdn { value } } }
        pool_members { edges { node { address { value } } } }
        health_checks { edges { node { check { value } rise { value } fall { value } timeout { value } } } }
      }
    }
  }
}
"""


# Every location with its reverse `circuits`: the same circuit__location edge as a circuit's `locations`.
CIRCUIT_LOCATIONS_QUERY = """
query {
  TopologyConnectableLocation {
    edges {
      node {
        id
        __typename
        name { value }
        circuits { edges { node { id __typename } } }
      }
    }
  }
}
"""
