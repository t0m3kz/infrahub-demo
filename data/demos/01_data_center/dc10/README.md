# DC10 - Small Hierarchical ToR (Sydney)

## Overview
**DC10** demonstrates a small (S) hierarchical ToR deployment using Dell PowerSwitch S5000 series equipment in Sydney.

## Key Details
- **Location**: Sydney (SYD-1)
- **Design**: S-Hierarchical-ToR
- **Platform**: Dell PowerSwitch S5000 Series
- **Scale**: 2 pods, 8 racks
- **Deployment**: ToR (direct to spine, no leaf layer)
- **Rooms**: 2 (SYD-1 Room-1 and Room-2)

## Architecture
- **Super Spines**: 4x PowerSwitch S5232F-ON
- **Spines per Pod**: 4x PowerSwitch S5232F-ON
- **Racks per Pod**: 4 racks
- **Leaf Templates** (ToR mode):
  - 2_DELL_LEAFS_PowerSwitch-S5224F-ON (24 ports)
  - 4_DELL_LEAFS_PowerSwitch-S5248F-ON (48 ports)

## Use Cases
- Regional office edge DC
- Small enterprise private cloud (SMB)
- Lab/test environment
- Branch office infrastructure

## Deployment
```bash
uv run invoke deploy-dc --scenario dc10
```

## Summary
DC10 demonstrates **small-scale hierarchical ToR** deployment with direct spine connectivity, ideal for edge/branch offices with simplified architecture and lower latency.
