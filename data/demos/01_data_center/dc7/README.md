# DC7 - Medium Standard Mixed (Dublin)

## Overview
**DC7** demonstrates a medium (M) standard mixed deployment using Edgecore open networking equipment in Dublin.

## Key Details
- **Location**: Dublin (DUB-1)
- **Design**: M-Standard-Mixed
- **Platform**: Edgecore 7AS7326-56X-O-48V-F
- **Scale**: 2 pods, 6 racks
- **Deployment**: Mixed (flexible deployment)
- **Suites**: 2 (DUB-1 Suite-1 and Suite-2)

## Architecture
- **Super Spines**: 2x Edgecore 7AS7326-56X-O-48V-F
- **Spines per Pod**: 2x Edgecore 7AS7326-56X-O-48V-F
- **Racks per Pod**: 3 racks
- **Leaf Templates**:
  - 4_EDGECORE_LEAFS_Edgecore-7AS7326-56X-O-48V-F (high capacity)
  - 2_EDGECORE_LEAFS_Edgecore-7AS7326-56X-O-48V-F (standard)

## Use Cases
- Medium-scale mixed deployment
- Open networking demonstration (SONiC)
- Departmental/regional DC
- Cost-effective mixed architecture
- Vendor-neutral infrastructure

## Deployment
```bash
uv run invoke deploy-dc --scenario dc7
```

## Summary
DC7 demonstrates **medium-scale mixed deployment** using Edgecore open networking switches, showcasing SONiC-based infrastructure with flexible pod deployment types.
