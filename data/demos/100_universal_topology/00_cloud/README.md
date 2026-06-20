# Cloud — Where Money Goes to Die Productively

> *"I asked an AI how to reduce our cloud bill. It suggested we move everything back on-prem.
> We fired the AI. The cloud bill remains."*

---

## Overview

**Providers:** AWS, Azure | **Regions:** eu-central-1, westeurope | **Actual Location:** Frankfurt

Welcome to the Cloud layer — where your carefully planned on-premises architecture goes to be reimagined
as a series of managed services, each with their own billing dimension, deprecation schedule, and
mandatory re-certification every 18 months.

---

## What's Here

| Provider | Region | Alias | What's Running |
| --- | --- | --- | --- |
| AWS | eu-central-1 | Frankfurt | EKS, ALB, RDS, NLB, routing, the works |
| Azure | westeurope | Also Frankfurt | AKS, App Gateway, VNet, more routing |

> Both providers call their region "western/central Europe." Both mean Frankfurt.
> The Germans did not ask for this responsibility and yet here we are.

---

## AWS — `aws/eu-central-1/`

A fully modeled EKS cluster with VPC CNI, EBS storage, ALB ingress, ECR registry,
and Prometheus shipping metrics to AMP — because apparently just *running a container*
requires six supporting services and a support contract.

```text
eu-central-1/
├── 00_ipam.yml        # IP ranges. Chosen carefully. Changed immediately.
├── 01_locations.yml   # It's Frankfurt. We know.
├── 02_topology.yml    # CloudRegion topology node. Very official.
├── 03_network.yml     # VPC, subnets, security groups. The holy trinity of AWS confusion.
├── 04_compute.yml     # EKS cluster + 6 capability nodes. Just a few things.
├── 05_loadbalancer.yml # ALB. Not to be confused with NLB. Or CLB. Or the other ALB.
├── 07_instances.yml   # EC2 instances. Named sensibly. This was a mistake.
├── 08_nlb_listeners.yml # NLB listeners. Because one load balancer wasn't enough.
├── 09_routing.yml     # Route tables. Not to be confused with routing protocols.
├── eu-central-1a/     # AZ 1. Alive.
└── eu-central-1b/     # AZ 2. Also alive. The third one wasn't invited.
```

---

## Azure — `azure/westeurope/`

AKS with Azure CNI, Azure Disk CSI, App Gateway ingress, ACR, and Prometheus
remote-writing to Azure Monitor — a URL so long it required a `yamllint` ignore comment.
This is not a joke. Check the file.

```text
westeurope/
├── 00_ipam.yml      # Address space. Microsoft calls it a VNet. We call it "the CIDR situation."
├── 01_topology.yml  # CloudRegion node. Westeurope. Frankfurt.
├── 03_network.yml   # VNet, subnets. Fewer acronyms than AWS. Marginally.
├── 04_aks.yml       # AKS cluster + capabilities + one extremely long URL
└── 05_routing.yml   # UDRs. Not BGP. Azure has opinions about this.
```

---

## A Note on Multi-Cloud

This demo runs both AWS and Azure because the architecture committee had a meeting in 2021
and nobody could agree. Rather than have a follow-up meeting, someone said "let's just do both"
and everyone nodded with the quiet relief of people who will not be in the next meeting.

An AI was later consulted on whether multi-cloud was the right strategy. It said yes.
It also says yes to everything else. This is why we have humans. Allegedly.

---

*No cloud credits were harmed in the modeling of this topology.
Real deployments may vary. Your egress bill will definitely vary.*
