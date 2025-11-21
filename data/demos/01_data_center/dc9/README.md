# DC9 - Large Hierarchical Mixed (Singapore)

## Overview
**DC9** demonstrates a large (L) hierarchical mixed deployment using Arista DCS-7000 series equipment in Singapore.

## Key Details
- **Location**: Singapore (SIN-1)
- **Design**: L-Hierarchical-Mixed
- **Platform**: Arista DCS-7000 Series (mixed models)
- **Scale**: 4 pods, 24 racks
- **Deployment**: Mixed (flexible leaf templates)
- **Suites**: 4 (SIN-1 Suite-1 through Suite-4)

## Architecture
- **Super Spines**: 4x DCS-7050CX3-32C-R
- **Spines per Pod**: 4x DCS-7050CX3-32C-R
- **Racks per Pod**: 6 racks with mixed templates
- **Leaf Templates**:
  - 4_ARISTA_LEAFS_DCS-7050CX3-32C-R (high capacity)
  - 2_ARISTA_LEAFS_DCS-7050CX3-32C-R (standard)
  - 4_ARISTA_LEAFS_DCS-7050CX4M-48D8-F (high density)
  - 4_ARISTA_LEAFS_DCS-7050SX3-24YC4C-S-R (compact)

## Use Cases
- Multi-workload cloud
- Enterprise hybrid cloud
- Managed service provider
- Workload-optimized deployments

## Deployment
```bash
uv run invoke deploy-dc --scenario dc9
```

## Summary
DC9 demonstrates **large-scale mixed deployment** with 4 different Arista leaf models, showcasing flexible template usage for diverse workload requirements.
