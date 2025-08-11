# Data Center (DC) Scenario

This scenario contains schemas, templates, and data for data center fabric deployments using spine-leaf architecture.

## Overview

Data center fabrics provide high-performance, scalable network infrastructure using modern protocols like VXLAN/EVPN for overlay networking and BGP/OSPF for underlay routing.

## Architecture

- **Spine Switches**: Aggregation layer providing connectivity between leaf switches
- **Leaf Switches**: Top-of-rack switches connecting servers and providing VTEP functionality
- **Underlay**: BGP or OSPF for IP connectivity between spine and leaf
- **Overlay**: VXLAN/EVPN for tenant isolation and L2/L3 services

## Schemas

### Core Schemas
- [`dc_topology.yml`](schemas/dc_topology.yml) - Data center specific topology definitions
- [`topology.yml`](schemas/topology.yml) - General topology framework

### Routing Schemas
- [`bgp.yml`](schemas/bgp.yml) - BGP protocol configuration
- [`ospf.yml`](schemas/ospf.yml) - OSPF protocol configuration
- [`routing.yml`](schemas/routing.yml) - General routing service definitions
- [`routing_policies*.yml`](schemas/) - Routing policy definitions

## Templates

### Spine Templates
- [`arista_eos.j2`](templates/spines/arista_eos.j2) - Arista EOS spine configuration
- [`cisco_nxos.j2`](templates/spines/cisco_nxos.j2) - Cisco NX-OS spine configuration
- [`edgecore_sonic.j2`](templates/spines/edgecore_sonic.j2) - EdgeCore SONiC spine configuration
- [`eos_vxlan_evpn.j2`](templates/spines/eos_vxlan_evpn.j2) - EOS VXLAN/EVPN specific
- [`nxos_vxlan_evpn.j2`](templates/spines/nxos_vxlan_evpn.j2) - NX-OS VXLAN/EVPN specific

### Leaf Templates
- [`arista_eos.j2`](templates/leafs/arista_eos.j2) - Arista EOS leaf configuration with security
- [`cisco_nxos.j2`](templates/leafs/cisco_nxos.j2) - Cisco NX-OS leaf configuration
- [`edgecore_sonic.j2`](templates/leafs/edgecore_sonic.j2) - EdgeCore SONiC leaf configuration

## Sample Data

- [`DC-1.yml`](data/DC-1.yml) - Katowice Data Center (OSPF-iBGP strategy)
- [`DC-2.yml`](data/DC-2.yml) - Additional data center example
- [`DC-3.yml`](data/DC-3.yml) - Additional data center example
- [`DC-4.yml`](data/DC-4.yml) - Additional data center example

## Key Features

- **Multi-vendor support**: Arista, Cisco, EdgeCore platforms
- **Flexible underlay**: Support for both eBGP and OSPF+iBGP strategies
- **VXLAN/EVPN overlay**: Modern data center overlay protocols
- **Security integration**: ACLs, security zones, and profiles
- **Emulation support**: Container Lab (CLAB) integration

## Usage Examples

1. **Creating a new DC**: Use the [`TopologyDataCenter`](schemas/dc_topology.yml:330) schema
2. **Spine configuration**: Apply spine templates based on device platform
3. **Leaf configuration**: Apply leaf templates with tenant/VRF configurations
4. **Routing strategy**: Choose between `ospf-ibgp` or `ebgp-ibgp` strategies