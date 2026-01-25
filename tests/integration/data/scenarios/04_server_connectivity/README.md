# Server Connectivity Cabling Demo

## 🔌 Scotty's Engineering Miracles

*"I'm giving her all she's got, Captain!"* – Transport layer engineers, beam down those server connections! This demo ensures every physical server gets connected with dual-uplink redundancy.

## ⚠️ Important: Uses DC-1

This demo **deploys servers to the existing DC-1 topology**. Make sure DC-1 is deployed before running this demo.

## What Gets Created

**Infrahub Objects Created:**

- DcimPhysicalDevice (servers added to DC-1)
- DcimInterface (server connections)
- DcimCable (cabling connections)

## Prerequisites

Deploy DC-1 first:

```bash
cd ../data_center_topology
./deploy.sh --scenario dc1
```

## Quick Start

After DC-1 is deployed, add servers:

```bash
./deploy.sh
```

## Features

- **Dual-uplink redundancy** - Like having both warp engines firing
- **Round-robin distribution** - Fair load sharing across leaf switches
- **No orphaned servers** - Every server gets connected
- **Intelligent allocation** - Smart interface distribution

## Generated Connectivity

Each server connection includes:

- Dual connections to leaf switches
- Round-robin interface allocation
- Proper cable documentation
- Full connectivity mapping

## Architecture

The cabling strategy ensures:

1. Every server is connected to 2 leaf switches (redundancy)
2. Interfaces are distributed evenly using round-robin
3. No interface reuse or conflicts
4. Automatic failover capability

## Troubleshooting

If servers aren't connecting:

1. Verify DC-1 topology is deployed
2. Check leaf switch availability in DC-1
3. Review interface allocation in logs
