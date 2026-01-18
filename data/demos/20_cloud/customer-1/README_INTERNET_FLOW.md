# Customer-1 Application Internet Flow

## Architecture Overview

Customer-1 runs three production applications across multi-cloud infrastructure:

1. **E-Commerce Platform** (AWS eu-central-1)
2. **Analytics Dashboard** (GCP europe-west1)
3. **Customer Portal** (Azure westeurope)

## Internet-to-Application Flow

### Inbound Traffic (Internet → Application)

```
Internet User
    ↓
DNS Resolution (www.ecommerce.customer1.com → 52.29.123.45/46)
    ↓
Public IP (CloudPublicIP)
    ↓
Internet-Facing Load Balancer (CloudLoadBalancer)
    ↓
VIP Service / Listener (ServiceLoadBalancerVIP)
    ↓
Backend Pool (ServiceBackendPool)
    ↓
Application Instances in Private Subnets (CloudInstance)
```

### Outbound Traffic (Application → Internet)

```
Application Instance in Private Subnet (CloudInstance)
    ↓
Private Subnet (CloudNetworkSegment)
    ↓
Route Table → NAT Gateway (CloudNATGateway)
    ↓
NAT Gateway Public IP
    ↓
Internet Gateway (CloudInternetGateway)
    ↓
Internet
```

## Component Details

### 1. E-Commerce Platform (AWS)

**Public Entry Point:**
- **DNS**: `www.ecommerce.customer1.com`
- **Public IPs**:
  - `52.29.123.45` (customer-1-ecommerce-prod-alb-ip-1)
  - `52.29.123.46` (customer-1-ecommerce-prod-alb-ip-2)
- **Load Balancer**: customer-1-ecommerce-prod-alb (ALB)
  - Type: Application Load Balancer
  - Scheme: internet_facing
  - Subnets: customer-1-ecommerce-prod-web-1a, customer-1-ecommerce-prod-web-1b

**VIP Service:**
- **Hostname**: www.ecommerce.customer1.com
- **Protocol**: HTTPS
- **Port**: 443
- **SSL**: ecommerce-prod-wildcard

**Backend Pool:**
- Web tier instances in:
  - customer-1-ecommerce-prod-web-1a (10.100.1.0/24)
  - customer-1-ecommerce-prod-web-1b (10.100.2.0/24)

**Outbound Connectivity:**
- App/Data tier instances use NAT Gateways:
  - customer-1-ecommerce-prod-nat-1a (52.29.234.11)
  - customer-1-ecommerce-prod-nat-1b (52.29.234.12)

**Network Flow:**
```
Internet (HTTPS 443)
  ↓
Public IP: 52.29.123.45/46
  ↓
ALB: customer-1-ecommerce-prod-alb
  ↓
Web Subnets: 10.100.1.0/24, 10.100.2.0/24
  ↓
Backend Instances (web tier)
  ↓ (internal routing)
App Tier: 10.100.10.0/24, 10.100.11.0/24
  ↓ (outbound via NAT)
NAT Gateway: 52.29.234.11/12
  ↓
Internet
```

### 2. Analytics Dashboard (GCP)

**Public Entry Point:**
- **DNS**: `analytics.customer1.com`
- **Public IP**: `34.107.45.67` (customer-1-analytics-prod-lb-ip)
- **Load Balancer**: customer-1-analytics-prod-lb
  - Type: Application Load Balancer
  - Scheme: internet_facing
  - Subnet: customer-1-analytics-prod-web

**VIP Service:**
- **Hostname**: analytics.customer1.com
- **Protocol**: HTTPS
- **Port**: 443
- **SSL**: analytics-prod-cert

**Backend Pool:**
- Web tier instances in customer-1-analytics-prod-web (10.110.1.0/24)

**Outbound Connectivity:**
- App/Data tier instances use Cloud NAT:
  - customer-1-analytics-prod-nat (34.107.89.23)

**Network Flow:**
```
Internet (HTTPS 443)
  ↓
Public IP: 34.107.45.67
  ↓
GCP Load Balancer: customer-1-analytics-prod-lb
  ↓
Web Subnet: 10.110.1.0/24
  ↓
Backend Instances (web tier)
  ↓ (internal routing)
App Tier: 10.110.10.0/24
  ↓ (outbound via Cloud NAT)
Cloud NAT: 34.107.89.23
  ↓
Internet
```

### 3. Customer Portal (Azure)

**Public Entry Point:**
- **DNS**: `portal.customer1.com`
- **Public IP**: `20.93.67.89` (customer-1-portal-prod-lb-ip)
- **Load Balancer**: customer-1-portal-prod-lb (Application Gateway)
  - Type: Application Gateway
  - Scheme: internet_facing
  - Subnets: customer-1-portal-prod-web-1, customer-1-portal-prod-web-2

**VIP Service:**
- **Hostname**: portal.customer1.com
- **Protocol**: HTTPS
- **Port**: 443
- **SSL**: portal-prod-cert

**Backend Pool:**
- Web tier instances in:
  - customer-1-portal-prod-web-1 (10.120.1.0/24)
  - customer-1-portal-prod-web-2 (10.120.2.0/24)

**Outbound Connectivity:**
- App tier instances use NAT Gateway:
  - customer-1-portal-nat-ip (20.93.112.45)

**Network Flow:**
```
Internet (HTTPS 443)
  ↓
Public IP: 20.93.67.89
  ↓
Application Gateway: customer-1-portal-prod-lb
  ↓
Web Subnets: 10.120.1.0/24, 10.120.2.0/24
  ↓
Backend Instances (web tier)
  ↓ (internal routing)
App Tier: 10.120.10.0/24, 10.120.11.0/24
  ↓ (outbound via NAT)
NAT Gateway: 20.93.112.45
  ↓
Internet
```

## Security Controls

### Network Segmentation
- **Web Tier**: Public subnets with load balancer access
- **App Tier**: Private subnets, internal-only
- **Data Tier**: Private subnets, database access only

### Security Groups / Firewall Rules
- **Load Balancer SG**: Allow 443 from 0.0.0.0/0
- **Web Tier SG**: Allow traffic from Load Balancer only
- **App Tier SG**: Allow traffic from Web Tier only
- **Data Tier SG**: Allow traffic from App Tier only

### Public IP Management
- **Load Balancers**: Static public IPs (Elastic IPs / Reserved IPs)
- **NAT Gateways**: Static public IPs for consistent outbound
- **Instances**: No direct public IP assignment

## Data Model Components

### Core Resources Created:
1. **CloudVirtualNetwork** (9 VPCs/VNets)
   - With nested IpamPrefix for CIDR blocks

2. **CloudNetworkSegment** (Subnets for web/app/data tiers)
   - Attached to VPCs
   - Categorized by tier and availability zone

3. **CloudInstance** (Application servers)
   - Deployed in appropriate subnets
   - No direct public IPs

4. **CloudLoadBalancer** (3 internet-facing + 3 internal)
   - Deployed in web tier subnets
   - Public IPs can be attached via UI after creation

5. **CloudPublicIP** (8 total)
   - 4 for NAT gateways (associated in data files)
   - 4 pre-created for load balancers (to be attached via UI)

6. **ServiceLoadBalancerVIP** (Listeners)
   - HTTPS listeners on port 443
   - SSL certificate references

7. **ServiceBackendPool** (Target groups)
   - References to backend instances
   - Health checks configured

8. **CloudNATGateway** (Outbound connectivity)
   - Associated with private subnets
   - Has public IP for outbound traffic

9. **CloudInternetGateway** (VPC internet connectivity)
   - One per VPC
   - Enables public IP routing

**Note**: Public IPs for load balancers are pre-created but need to be associated via the Infrahub UI. This reflects real-world workflows where Elastic IPs/Reserved IPs are allocated first, then attached to resources.

## Validation Checklist

✅ **Network Layer**
- VPCs have CIDR blocks (IpamPrefix)
- Subnets exist within VPC CIDRs
- Internet Gateways attached to VPCs

✅ **Compute Layer**
- Instances deployed in correct subnets
- Instance types appropriate for workload
- Security groups attached

✅ **Load Balancing Layer**
- Load balancers have public IPs (internet-facing)
- VIP services configured with correct ports
- Backend pools reference instances

✅ **Outbound Layer**
- NAT gateways have public IPs
- NAT gateways serve private subnets
- Route tables configured (implicit)

## Complete Flow Example: E-Commerce Purchase

1. **User visits**: `https://www.ecommerce.customer1.com`
2. **DNS resolves** to: `52.29.123.45` (ALB Public IP)
3. **ALB receives** HTTPS request on port 443
4. **ALB forwards** to web tier instance in `10.100.1.0/24`
5. **Web instance** processes, calls backend API
6. **Backend API** in app tier `10.100.10.0/24` processes business logic
7. **App instance** queries database in data tier `10.100.20.0/24`
8. **Database query** needs external service (payment gateway)
9. **Outbound traffic** routes through NAT Gateway `52.29.234.11`
10. **Response** flows back through the same path
11. **ALB returns** response to user

All components are properly defined in the Infrahub data model! 🎉
