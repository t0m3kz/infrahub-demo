# DC8 - Extra-Large Hierarchical Middle-Rack (Tokyo)

## Overview
**DC8** demonstrates an extra-large (XL) hierarchical middle-rack deployment using Cisco Nexus 9000 series equipment in Tokyo.

## Key Details
- **Location**: Tokyo (TYO-1)
- **Design**: XL-Hierarchical-MR
- **Platform**: Cisco Nexus 9000 Series
- **Scale**: 8 pods, 64 racks
- **Deployment**: Middle-Rack only
- **Suites**: 8 (TYO-1 Suite-1 through Suite-8)

## Architecture
- **Super Spines**: 4x N9K-C9336C-FX2
- **Spines per Pod**: 4x N9K-C9364C-GX
- **Racks per Pod**: 8 racks
- **Leaf Template**: 4_CISCO_LEAFS_MR_N9K-C9336C-FX2
- **ToR Template**: 4_CISCO_TORS_N9K-C9336C-FX2

## Use Cases
- Hyperscale cloud provider
- Large enterprise private cloud
- Maximum scale demonstration
- Multi-tenant isolation (8 pods)

## Deployment
```bash
uv run invoke deploy-dc --scenario dc8
```

## Summary
DC8 represents the **maximum scale** demonstration with 8 pods and 64 racks, proving InfraHub can handle hyperscale deployments efficiently.
