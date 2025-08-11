# VXLAN/EVPN Overlay Data Structure

This folder contains the necessary data objects to configure a complete VXLAN/EVPN fabric for DC-1.

## File Organization

1. **01_ospf_areas.yml** - OSPF areas for underlay routing
2. **02_ospf_interface_profiles.yml** - OSPF interface profiles (P2P, loopback)
3. **03_ospf_services.yml** - ServiceOSPF instances for each device (underlay)
4. **04_bgp_services.yml** - ServiceBGP instances for EVPN control plane
5. **05_evpn_services.yml** - ServiceEVPN instances for overlay control
6. **06_vxlan_services.yml** - ServiceVXLAN instances for data plane
7. **07_network_segments.yml** - ServiceNetworkSegment for L2/L3 segments
8. **08_vni_pools.yml** - CoreNumberPool for VNI allocation

## Fabric Architecture

### Underlay (IP Fabric)
- **Protocol**: OSPF Area 0
- **Devices**: 2 Spines + 4 Leafs
- **Router IDs**:
  - Spines: 10.1.1.1-2
  - Leafs: 10.1.1.11-14

### Overlay (VXLAN/EVPN)
- **Control Plane**: BGP EVPN (ASN 65001)
- **Data Plane**: VXLAN
- **Route Reflection**: Spines as RR servers
- **VNIs**:
  - Web Tier: 100 (LEAF1, LEAF2)
  - App Tier: 200 (LEAF1, LEAF2)
  - DB Tier: 300 (LEAF3, LEAF4)

### Network Segments
- **WEB-TIER-SEGMENT**: VNI 100 (L2 only)
- **APP-TIER-SEGMENT**: VNI 200 (L2 only)
- **DB-TIER-SEGMENT**: VNI 300 (L2 only, jumbo frames)
- **TENANT-A-L3-SEGMENT**: VNI 1000 (L3 gateway)

## Service Relationships

```
ServiceEVPN → underlay_routing → ServiceOSPF
ServiceVXLAN → underlay_routing → ServiceOSPF
ServiceVXLAN → evpn_service → ServiceEVPN
ServiceNetworkSegment → vxlan_services → ServiceVXLAN[]
```

## Load Order

Load files in numerical order to respect dependencies:
```bash
infrahubctl load data/overlay/01_ospf_areas.yml
infrahubctl load data/overlay/02_ospf_interface_profiles.yml
infrahubctl load data/overlay/03_ospf_services.yml
infrahubctl load data/overlay/04_bgp_services.yml
infrahubctl load data/overlay/05_evpn_services.yml
infrahubctl load data/overlay/06_vxlan_services.yml
infrahubctl load data/overlay/07_network_segments.yml
infrahubctl load data/overlay/08_vni_pools.yml
```

Or load entire folder:
```bash
infrahubctl load data/overlay/
```
