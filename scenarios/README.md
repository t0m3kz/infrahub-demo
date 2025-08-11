# Network Infrastructure Scenarios

This directory contains organized schemas, templates, and data files for 5 different network infrastructure scenarios:

## Scenarios Overview

### 1. DC (Data Center)
- **Location**: [`scenarios/dc/`](dc/)
- **Description**: Data center fabric with spine-leaf architecture
- **Key Components**: VXLAN/EVPN, BGP, OSPF routing protocols
- **Device Types**: Spine switches, Leaf switches
- **Schemas**: DC topology, routing protocols (BGP, OSPF, PIM)
- **Templates**: Arista EOS, Cisco NX-OS, EdgeCore SONiC configurations

### 2. POP (Point of Presence)
- **Location**: [`scenarios/pop/`](pop/)
- **Description**: Colocation center network infrastructure
- **Key Components**: Edge routing, provider connectivity
- **Device Types**: Edge routers, core equipment
- **Schemas**: Topology definitions for colocation centers
- **Templates**: Edge device configurations

### 3. Load Balancer
- **Location**: [`scenarios/loadbalancer/`](loadbalancer/)
- **Description**: Application load balancing infrastructure
- **Key Components**: VIPs, backend servers, health checks
- **Device Types**: F5 BIG-IP, HAProxy, NGINX
- **Schemas**: Load balancer services, server definitions
- **Templates**: F5 networks, HAProxy configurations

### 4. Security Gateway (Cloud Security)
- **Location**: [`scenarios/security_gateway/`](security_gateway/)
- **Description**: Cloud-based security services and gateways
- **Key Components**: SASE, SWG, ZTNA, Cloud firewalls
- **Providers**: Zscaler, Prisma Access, Cisco Umbrella, Cloudflare
- **Schemas**: Cloud security services, gateways, policy groups
- **Templates**: Zscaler cloud configurations

### 5. Security (Network Security)
- **Location**: [`scenarios/security/`](security/)
- **Description**: Traditional network security with firewalls and policies
- **Key Components**: Security zones, policies, ACLs, firewall rules
- **Device Types**: Network firewalls, security appliances
- **Schemas**: Security policies, zones, addresses, services
- **Templates**: Firewall configurations

## Directory Structure

Each scenario follows a consistent structure:

```
scenarios/{scenario}/
├── schemas/          # Schema definitions (.yml files)
├── templates/        # Jinja2 configuration templates (.j2 files)
├── data/            # Sample data files (.yml files)
└── README.md        # Scenario-specific documentation
```

## Usage

1. **Schemas**: Define the data models and object types for each scenario
2. **Templates**: Generate device configurations using Jinja2 templating
3. **Data**: Sample data files showing real-world examples

## Integration

These scenarios can be used individually or combined to create comprehensive network infrastructures that span multiple deployment types and security models.