# Zscaler Cloud Infrastructure - Azure Deployment

## Overview

**Customer:** Customer-2 (The one who realized too late that "cloud-first" doesn't mean "security-optional")

**Mission:** Deploy **Zscaler Private Service Edge** across multiple Azure regions because someone finally read the security audit and had an existential crisis about allowing direct internet access.

**Translation:** We're putting a bouncer between your cloud workloads and the internet. Think of Zscaler as the security-paranoid middleware that inspects every packet like TSA inspects your shampoo bottle.

---

## The "Zero Trust" Architecture (Or: Trust No One, Not Even Yourself)

### Deployment Strategy (AKA: The Paranoia Distribution)

Zscaler connectors scattered across Azure like security cameras in a casino:

- **Primary Region**: West Europe (westeurope) - Where production traffic goes to be interrogated
- **Secondary Region**: North Europe (northeurope) - The DR site that management insists we need but hopes we'll never use
- **Tertiary Region**: Germany West Central (germanywestcentral) - Because GDPR compliance officers get nervous sweats

### Components (The Security Theater Ensemble)

1. **Cloud Connectors (ZIA)** - Zscaler Internet Access (The Internet Bouncer)
   - 2 Cloud Connector VMs per region (6 total) - Because single points of failure are so 2010
   - Handles secure tunnels to Zscaler cloud for internet-bound traffic
   - Provides local breakout for cloud workloads (fancy term for "lets traffic out after intense scrutiny")
   - Instance type: Standard_D4s_v3 (4 vCPUs, 16 GB RAM) - Overkill for a security appliance? Never.
   - Image: zscaler-cloud-connector-6.2 (Updated quarterly, because vulnerabilities don't take vacations)
   - **Analogy:** Like having a security guard who reads every letter before letting it leave the building. Yes, even the "Thank You" notes.

2. **App Connectors (ZTNA)** - Zero Trust Network Access (The VPN Killer)
   - 2 App Connector VMs per region (6 total) - Redundancy, because paranoia comes in pairs
   - Enables secure access to private applications without the VPN baggage
   - Provides clientless and client-based ZTNA (Choose your authentication adventure!)
   - Instance type: Standard_D2s_v3 (2 vCPUs, 8 GB RAM) - Smaller than Cloud Connectors because internal apps are supposedly "safer"
   - Image: zscaler-app-connector-1.5
   - **Analogy:** The sophisticated keycard system that replaced the ancient VPN dungeon. Now you can access the castle without memorizing incantations or waiting for the moat to lower.

3. **Network Architecture** (The Segmentation Obsession)
   - Dedicated VNets per region for connector deployment (Isolation: because networking engineers have trust issues)
   - Service subnets for connector VMs (Where the magic happens)
   - Management subnets for administrative access (The back door for when things inevitably break)
   - Separate transit subnets for routing (Because one subnet was too simple)

4. **Load Balancers** (The Traffic Distributors)
   - Internal load balancers for Cloud Connector HA (ZIA traffic)
   - Health checks and automatic failover (Detecting dead VMs faster than your monitoring alerts)
   - Traffic distribution across Cloud Connector instances (Round-robin like it's 1999, but with more TLS)
   - **Fun Fact:** Even your load balancer doesn't trust a single connector. Smart load balancer.

5. **Security** (Because We're Not Done Being Paranoid)
   - Network Security Groups (NSGs) for traffic filtering (The firewall's younger, cloud-native cousin)
   - Private network access only (No public IPs, because we learned our lesson from the 2023 incident)
   - NAT Gateways for outbound internet connectivity (One-way street: exit only, no returns)
   - **Philosophy:** If a packet can't be inspected, logged, and approved by three committees, it's not going anywhere.

---

## Naming Convention (The Taxonomy of Paranoia)

All resources follow the pattern:

```text
zscaler-<region>-<component>-<identifier>
```

Examples:

- `zscaler-westeu-cc-01` - West Europe Cloud Connector (ZIA) VM #1 (The internet traffic inspector)
- `zscaler-westeu-ac-01` - West Europe App Connector (ZTNA) VM #1 (The app access gatekeeper)
- `zscaler-westeu-vnet` - West Europe virtual network (The security perimeter)
- `zscaler-westeu-lb` - West Europe internal load balancer (The traffic cop)

Component abbreviations (Because Security People Love TLAs):

- `cc` = Cloud Connector (ZIA) - "Cloud Cop"
- `ac` = App Connector (ZTNA) - "Access Control"
- `lb` = Load Balancer - "Load... Bearer of packets"
- `nsg` = Network Security Group - "No Sketchy Guests"

---

## The Philosophy (Zero Trust Translated)

**"Never trust, always verify"** - The mantra that launched a thousand security products and justified enterprise budgets.

**Traditional Security:** "Build a wall and hope nobody climbs it" (Spoiler: They climbed it)

**Zero Trust Security:** "Assume everybody is a threat, including your own employees, especially Dave from accounting"

**Zscaler's Approach:** Put every packet through airport-level security screening. Your traffic doesn't get a boarding pass without proving its identity, destination, and purpose.

---

## Load Order (The Dependency Dance)

Files are numbered for sequential loading, because Terraform taught us that parallel deployments lead to tears:

1. `01_zscaler_cloud_account.yml` - Azure subscription (The billing target)
2. `02_zscaler_virtual_networks.yml` - VNets per region (The security bubbles)
3. `03_zscaler_network_segments.yml` - Subnets (service, management, transit) (The segmentation symphony)
4. `04_zscaler_security_groups.yml` - NSGs for traffic control (The packet police)
5. `05_zscaler_connector_instances.yml` - Cloud Connector VMs (The actual workhorses)
6. `06_zscaler_load_balancers.yml` - Internal load balancers (The traffic distributors)
7. `06b_zscaler_lb_vip_services.yml` - LB listeners and backend pools (The connection configuration)
8. `07_zscaler_nat_gateways.yml` - NAT for outbound connectivity (The one-way exit door)

**Pro Tip:** Load them in order. The system doesn't appreciate your creative interpretation of dependencies.

---

## Network Design (The IP Address Monopoly)

Each region follows this pattern (because consistency is the only thing preventing chaos):

```text
VNet: 10.200.x.0/24  (256 addresses, because we're not monsters)
├── Service Subnet: 10.200.x.0/27 (32 IPs for connectors - the workers)
├── Management Subnet: 10.200.x.32/27 (32 IPs for admin access - the emergency exit)
└── Transit Subnet: 10.200.x.64/27 (32 IPs for routing - the highway)
```

- **West Europe:** 10.200.1.0/24 (Production - where real traffic fears to tread)
- **North Europe:** 10.200.2.0/24 (DR - collecting dust until the apocalypse)
- **Germany West Central:** 10.200.3.0/24 (Compliance - because GDPR said so)

**Subnet Philosophy:** Three subnets per region because:

- Service: Where connectors actually work
- Management: Where admins panic at 3 AM
- Transit: Where traffic pretends to be routed efficiently

---

## Integration Points (The Hybrid Cloud Handshake)

### ExpressRoute Integration

**Purpose:** Private connectivity to on-premises (Because the public internet is full of riffraff)

**Reality:** You're paying premium prices to avoid the public internet that you'll still use for half your traffic anyway.

**Benefit:** CIO can tell board about "private, secure connectivity" without mentioning the cost

### Zscaler Internet Access (ZIA) Cloud Service

**Purpose:** Intercept, inspect, and interrogate all internet-bound traffic

**Process:**

1. User requests website
2. Cloud Connector intercepts (You thought you had privacy?)
3. Zscaler cloud inspects (Deep packet inspection is deep)
4. Policy engine decides fate (The digital judge, jury, and executioner)
5. Traffic allowed/blocked/logged (Usually all three)

**Fun Fact:** Even your encrypted traffic gets decrypted, inspected, and re-encrypted. It's like TSA, but for packets.

### Zero Trust Network Access (ZTNA)

**Purpose:** Kill VPNs dead and replace them with identity-based access

**Philosophy:** Your network location means nothing. Your identity means everything. Even then, we'll only give you access to exactly what you need, when you need it, and we'll be watching.

**Translation:** No more "connect to VPN and access everything" like the good old days. Now it's "prove who you are for every single app, every single time."

---

## Use Cases (Why You're Here)

### Internet Breakout in Cloud

**Scenario:** Cloud VMs need internet access but shouldn't just YOLO directly to the web

**Solution:** Force all traffic through Zscaler Cloud Connectors

**Benefit:** Every packet inspected, logged, and judged before leaving your cloud bubble

**Trade-off:** Added latency, but hey, security isn't free (in performance or budget)

### Secure Application Access

**Scenario:** Remote users need access to internal apps without VPN nightmares

**Solution:** Zscaler App Connectors broker access based on identity

**Benefit:** No VPN client, no "full network access," just app-specific connectivity

**Reality Check:** Users will still complain it's "slower than VPN"

### Compliance and Visibility

**Scenario:** Auditors want to know who accessed what, when, and why

**Solution:** Zscaler logs everything. EVERYTHING.

**Benefit:** Pass audits, identify shadow IT, justify security budget increases

**Side Effect:** Storage costs for logs rival the cost of the actual connectors

---

## Technical Details (For Those Who Actually Deploy This)

### Load Balancer Configuration

- **Protocol:** TCP port 9000 (The connector communication port)
- **Algorithm:** Round-robin (Equal opportunity packet distribution)
- **Persistence:** Source IP (Sticky sessions because some protocols are needy)
- **Health Checks:** TCP probes every 10 seconds (Paranoid monitoring interval)

### VM Specifications

**Cloud Connectors (cc):**

- 4 vCPUs, 16 GB RAM (Standard_D4s_v3)
- Overkill? Maybe. But when your entire internet traffic flows through it, you don't cheap out.

**App Connectors (ac):**
- 2 vCPUs, 8 GB RAM (Standard_D2s_v3)
- Internal apps are "lighter" so these get the economy class VMs

### High Availability Strategy
- 2 VMs per region, per connector type
- Load balanced for active-active (Not active-passive, because wasting resources is wasteful)
- Auto-failover in under 30 seconds (Faster than your incident detection)

---

## How to Deploy (The Ritual)

### Prerequisites (The Checklist of Doom)
- Azure subscription with enough quota (Check before Friday afternoon deployments)
- Zscaler cloud tenant configured (Call your TAM if you don't have one)
- ExpressRoute circuit (Optional but recommended by everyone who pays Azure bandwidth bills)
- Strong coffee (Not optional)
- Incident response plan (For when this goes sideways)

### Deployment Steps

```bash
# 1. Load the schema (The blueprint)
uv run infrahubctl schema load schemas --branch zscaler-deployment

# 2. Load the Zscaler infrastructure (The actual build)
uv run infrahubctl object load data/demos/20_cloud/zscaler/ --branch zscaler-deployment

# 3. Verify everything loaded (The nervous checking)
uv run infrahubctl object get CloudInstance --branch zscaler-deployment

# 4. Check load balancer configuration (The connectivity validation)
uv run infrahubctl object get ServiceLoadBalancerVIP --branch zscaler-deployment

# 5. Generate configurations (If you're brave enough to automate)
uv run infrahubctl transform run --branch zscaler-deployment
```

### Post-Deployment (The Real Work Begins)

1. **Configure Zscaler Portal** - Point connectors at your Zscaler cloud tenant (Read the docs this time)
2. **Test Internet Access** - Verify traffic flows through connectors (curl is your friend)
3. **Enable Policies** - Start with "log only" mode (Deploy in production? Never go full enforcement immediately)
4. **Monitor Everything** - Watch dashboards like a hawk for 24 hours (Sleep is for the week after deployment)
5. **Update Documentation** - LOL just kidding, nobody does this

---

## Troubleshooting (When Reality Hits)

### "Connectors Won't Talk to Zscaler Cloud"

- Check NSG rules (Did you allow outbound 443?)
- Verify NAT Gateway (Can they even reach the internet?)
- Check Zscaler portal configuration (Typos happen at 2 AM)

### "Load Balancer Health Checks Failing"

- Verify connectors are actually running (Azure sometimes has opinions)
- Check port 9000 connectivity (NSGs block everything by default)
- Review connector logs (They're actually trying to tell you something)

### "Users Complaining About Slowness"

- Check connector CPU/memory (4 vCPUs might not be enough after all)
- Review Zscaler cloud latency (Plot twist: It's not always your infrastructure)
- Verify user is in correct region (Why is UK traffic going through Germany?)

### "Everything is Down"

- Check Azure status page (Sometimes it's them, not you)
- Verify ExpressRoute circuit (Did the telco "maintenance" you?)
- Restart connectors (The universal fix that actually works 60% of the time)

---

## Cost Analysis (The Budget Reality Check)

### Monthly Costs (Approximate, Azure will surprise you)

**Compute:**

- 12 VMs × ~$150/month = $1,800/month (Could be worse)

**Load Balancers:**

- 3 internal LBs × $20/month = $60/month (Surprisingly reasonable)

**NAT Gateways:**

- 3 NAT Gateways × $45/month = $135/month (Plus data processing fees that will hurt)

**Data Transfer:**

- Outbound internet traffic: $0.087/GB (This is where they get you)
- Inter-region traffic: $0.02/GB (Because "cloud" means "pay for everything")

**Total Base Cost:** ~$2,000/month + traffic costs

**Actual Cost:** Add 40% for "unexpected" Azure charges you'll discover at month end

**Zscaler Licensing:** Not included (Call sales for pricing that makes Azure look cheap)

---

## Success Metrics (How to Prove This Was Worth It)

✅ **Security Posture Improved** - Can now tell board about "Zero Trust Architecture"

✅ **Visibility Increased** - Know exactly what apps people use (Mostly personal Netflix)

✅ **Compliance Achieved** - Auditors stopped asking uncomfortable questions

✅ **VPN Eliminated** - No more "Can you restart the VPN server?" tickets

✅ **Latency Increased** - Wait, that's not a success... (But security isn't free!)

❌ **Budget Depleted** - Azure bill now requires CFO approval

---

## Conclusion (The TL;DR)

You've deployed a multi-region, highly-available Zscaler infrastructure in Azure that:

- Inspects all internet traffic like a paranoid security guard
- Provides Zero Trust access to apps (Because trust got us into this mess)
- Costs more than you budgeted (But less than a breach would)
- Makes compliance auditors smile (Rare sight)
- Adds latency that users will complain about (But IT Security finally wins an argument)

**Is it worth it?** Ask yourself after the next security incident at a competitor who didn't deploy this.

**Would we do it again?** After enough time passes for the deployment trauma to fade, yes.

**Recommended for:** Organizations that learned the hard way that "castle-and-moat" security died in 2015.

---

## Support

For issues, complaints, and existential questions about Zero Trust philosophy:

- **Zscaler Support:** Your TAM's email (Check your spam folder)
- **Azure Support:** Pay for Premium support or enjoy the forum experience
- **Internal IT:** That one person who actually read the Zscaler documentation
- **This README:** You're already here, this is as good as it gets

**Remember:** In Zero Trust, we trust no one. Especially not our deployment scripts on Friday afternoons.

