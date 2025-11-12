# Equinix POP (Point of Presence) Demo

## 📡 Federation's Interstellar Communication Array

Deploy distributed Points of Presence across multiple locations like the Federation's array of space stations. Each POP is a gateway to the galaxy.

## What Gets Created

**Infrahub Objects Created:**
- TopologyPOP
- LocationBuilding
- EquinixFacility
- DcimPhysicalDevice (peering routers)

## Quick Start

```bash
./deploy.sh --scenario pop
```

## POP Architecture

Each Point of Presence includes:

1. **Location Definition**
   - Geographic location
   - Facility details
   - Contact information

2. **Equinix Facility Integration**
   - Facility mapping
   - Cage details
   - Cross-connect information

3. **Peering Equipment**
   - Peering routers
   - Interface configuration
   - Routing setup

4. **Connectivity**
   - Uplink interfaces
   - Peering connections
   - Redundancy paths

## Multi-Location Deployment

Deploy POPs in multiple locations:
- North America
- Europe
- Asia Pacific
- Other regions

## Containerlab Integration

Simulate entire POP network locally:

```bash
uv run invoke clab up --pop
```

This simulates:
- Multiple POPs
- Network connectivity
- Routing protocols
- Traffic flows

## Features

- **Distributed architecture** - Multi-site deployment
- **Gateway functionality** - International connectivity
- **Redundancy** - Multiple uplinks per location
- **Scalability** - Easy to add new POPs
- **Monitoring** - Health checks per POP

## Data Requirements

Place your POP definitions in:
```
demos/equinix_pop/data/
├── locations/
├── facilities/
└── equipment/
```

## Configuration

Each POP includes:
- BGP peering setup
- Routing policies
- Firewall rules
- Monitoring alerts

## Troubleshooting

Verify POP connectivity:
```bash
uv run infrahubctl query topology --pop
```

## Related Services

This demo integrates with:
- Data Center infrastructure
- Multi-tenant networks
- Security policies
- Load balancing
