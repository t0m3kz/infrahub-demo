# Service Schemas - Circuit Architecture

## Overview

Simple two-object architecture:

1. **TopologyCircuit** - Provider's external circuit infrastructure
2. **ManagedCircuit** - Our operational management (consumed by both sides)

## Core Concept

**TopologyCircuit** = Provider's external circuit (e.g., Equinix XC-12345)
**ManagedCircuit** = Our operational wrapper (ONE object used by both interfaces)

Both side A and side B interfaces consume/reference the same managed circuit.

## Schemas

### managed_circuit.yml - `ManagedCircuit`

**Purpose**: Operational management for provider circuit

**Created by**: WAN connectivity generator (`generators/add/wan.py`)

**Usage**: Active - Created ONCE per circuit, consumed by both sides

**Relationships**:
- `circuit` → `TopologyCircuit` (the provider's external circuit)
- `deployment` → `TopologyDeployment` (managing deployment)
- `owner` → `OrganizationProvider` (circuit owner)

## Architecture

```
TopologyCircuit (Provider's External Circuit)
├── circuit_id: "XC-12345"
├── provider: Equinix
├── a_side_location: Building A
├── b_side_location: Building B
└── bandwidth, circuit_type

ManagedCircuit (ONE operational object)
├── circuit → TopologyCircuit
├── deployment → Source Deployment
└── Consumed by both side A and B

Side A Interface → uses ManagedCircuit
Side B Interface → uses ManagedCircuit

DcimCable
└── endpoints: [side_a_interface, side_b_interface]
```

**Simple and Clean**:
- Provider circuit = external (ONE object)
- Managed circuit = our tracking (ONE object)
- Both interfaces consume the same managed circuit
- Cable connects the interfaces physically

## References

- [WAN Generator](../../generators/add/wan.py) - Creates circuits and managed entities
- [TopologyCircuit Schema](../topology/topology_connectivity.yml) - Provider circuit definition

## Design Rationale

### Why Separate Provider Circuit from Our Interfaces?

**The circuit is EXTERNAL** - we don't own it:
- Provider provisions circuit XC-12345 between their cages
- Circuit exists whether or not we connect to it
- ManagedCircuitService binds OUR interface to that EXTERNAL circuit

### Why Not Put Interfaces on TopologyCircuit?

**Mixing external and internal concerns**:
- TopologyCircuit.a_side_interface would mean "circuit knows about our interface"
- But the circuit is external - it shouldn't know about our equipment
- Instead: ManagedCircuitService says "our interface connects to circuit"

**Example**:
- ❌ Bad: `circuit.a_side_interface = our_eth1`  (circuit references our stuff)
- ✅ Good: `service.circuit = external_circuit, service.interface = our_eth1` (we reference external circuit)

### Why Interface Services Relationship Matters

The `interface.interface_services` relationship is polymorphic and includes:
- ManagedCircuitService (connections to provider circuits)
- ManagedVirtualLinkService (overlay connections)
- ManagedNetworkSegment (VLANs/VXLANs)
- ManagedOSPF (routing protocol instances)

This allows bidirectional traversal:
- From interface → "what external circuits am I connected to?"
- From circuit → "what interfaces are bound to me?" (via service objects)

### Creating a Circuit (Generator Pattern)

```python
# 1. Create provider's circuit (external infrastructure)
circuit = await client.create(
    kind="TopologyCircuit",
    data={
        "circuit_id": "XC-dc1-border-01-fr5-edge-01",
        "circuit_type": "cross_connect",
        "bandwidth": 10000,
        "provider": {"hfid": ["Equinix"]},
        # Locations show where PROVIDER circuit exists
        "a_side_location": building_a,
        "z_side_location": building_b,
        # NOTE: No interface references - it's external!
    },
)

# 2. Create operational management entity
managed_circuit = await client.create(
    kind="ManagedCircuit",
    data={
        "name": "XC-dc1-border-01-fr5-edge-01",
        "circuit": {"id": circuit.id},
        "deployment": {"id": source_deployment["id"]},
    },
)

# 3. Bind OUR source interface to the external circuit
service_a = await client.create(
    kind="ManagedCircuitService",
    data={
        "name": "XC-dc1-border-01-fr5-edge-01-A",
        "circuit": {"id": circuit.id},  # External circuit
        "side": "a_side",
        "interface": {"id": our_source_interface.id},  # OUR interface
        "device": {"id": our_source_device.id},  # OUR device
        "deployment": {"id": source_deployment["id"]},
    },
)

# 4. Bind OUR destination interface to the external circuit
service_z = await client.create(
    kind="ManagedCircuitService",
    data={
        "name": "XC-dc1-border-01-fr5-edge-01-Z",
        "circuit": {"id": circuit.id},  # Same external circuit
        "side": "z_side",
        "interface": {"id": our_dest_interface.id},  # OUR other interface
        "device": {"id": our_dest_device.id},  # OUR other device
        "deployment": {"id": dest_deployment["id"]},
    },
)

# 5. Create physical cable between OUR interfaces
cable = await client.create(
    kind="DcimCable",
    data={
        "name": "XC-dc1-border-01-fr5-edge-01-Cable",
        "endpoints": [our_source_interface.id, our_dest_interface.id],
    },
)
```

### Querying Circuit Information

**From Circuit to Interfaces**:
```graphql
query GetCircuit {
  TopologyCircuit(circuit_id__value: "XC-dc1-border-01-fr5-edge-01") {
    edges {
      node {
        circuit_id { value }
        circuit_type { value }
        bandwidth { value }

        # A-side details via interface
        a_side_interface {
          node {
            name { value }
            device {
              node {
                name { value }
              }
            }
            ip_address {
              node {
                address { value }
              }
            }
          }
        }

        # Z-side details via interface
        z_side_interface {
          node {
            name { value }
            device {
              node {
                name { value }
              }
            }
          }
        }
      }
    }
  }
}
```

**From Interface to Circuits**:
```graphql
query GetInterfaceCircuits {
  DcimPhysicalInterface(name__value: "Ethernet1/1") {
    edges {
      node {
        name { value }

        # All circuit services on this interface
        interface_services {
          edges {
            node {
              ... on ManagedCircuitService {
                name { value }
                side { value }
                circuit {
                  node {
                    circuit_id { value }
                    circuit_type { value }
                    bandwidth { value }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

## Design Rationale

### Why Three Objects Instead of One?

**Separation of Concerns**: Each object serves a distinct purpose that cannot be merged without losing functionality.

**Interface Services Relationship**: The `interface.interface_services` relationship is polymorphic and includes:
- ManagedCircuitService (physical circuits)
- ManagedVirtualLinkService (overlay connections)
- ManagedNetworkSegment (VLANs/VXLANs)
- ManagedOSPF (routing protocol instances)

Without ManagedCircuitService, interfaces would lose the ability to enumerate their circuit connections.

### Why Not Just Use TopologyCircuit.a_side_interface?

**Directionality**: TopologyCircuit.a_side_interface shows WHERE the circuit connects, but ManagedCircuitService shows:
- Which SIDE (A or Z) from the circuit's perspective
- Per-interface service metadata
- Bidirectional traversal (interface → circuits)

**Example Use Case**:
- Interface has 3 circuits terminating on it
- Query `interface.interface_services` → get all 3 circuit services
- Without service objects, you'd need complex queries to find all circuits that reference this interface

## Migration Notes

For the complete three-layer architecture, create:

1. **TopologyCircuit** - Always set `a_side_interface` and `z_side_interface`
2. **ManagedCircuit** - Create once per circuit for operational management
3. **ManagedCircuitService** - Create twice per circuit (A-side and Z-side) for interface tracking

## References

- [WAN Generator](../../generators/add/wan.py) - Creates all three circuit objects
- [Common Transform](../../transforms/common.py) - Extracts circuit services from interfaces
- [TopologyCircuit Schema](../topology/topology_connectivity.yml) - Base circuit definition

