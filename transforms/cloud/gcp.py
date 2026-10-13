"""GCP HCL rendering for the cloud VPC Terraform transform."""

from transforms.cloud.terraform_common import tf_id


def _provider_block_gcp(project: str, region: str) -> list[str]:
    return [
        "terraform {",
        "  required_providers {",
        "    google = {",
        '      source  = "hashicorp/google"',
        '      version = "~> 5.0"',
        "    }",
        "  }",
        "}",
        "",
        'provider "google" {',
        f'  project = "{project}"',
        f'  region  = "{region}"',
        "}",
        "",
    ]


def _vpc_blocks_gcp(vpc: dict, ctx: dict, region: str) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_id = tf_id(vpc_name)

    lines += _network_blocks_gcp(vpc, vpc_name, vpc_id, region)
    lines += _security_group_blocks_gcp(vpc_name, vpc_id, ctx)
    lines += _instance_blocks_gcp(vpc_name, ctx)
    lines += _public_ip_blocks_gcp(ctx, region)
    lines += _nat_blocks_gcp(vpc_name, vpc_id, ctx, region)
    lines += _route_blocks_gcp(vpc_name, vpc_id, ctx)
    lines += _vpn_gateway_blocks_gcp(vpc_name, vpc_id, ctx, region)
    lines += _instance_group_blocks_gcp(vpc_name, vpc_id, ctx, region)

    return lines


def _network_blocks_gcp(vpc: dict, vpc_name: str, vpc_id: str, region: str) -> list[str]:
    lines: list[str] = []
    lines += [
        f'resource "google_compute_network" "{vpc_id}" {{',
        f'  name                    = "{vpc_name}"',
        "  auto_create_subnetworks = false",
        "}",
        "",
    ]

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_id = tf_id(seg_name)
        cidr = seg.get("cidr_block") or ""
        az_name = (seg.get("availability_zone") or {}).get("name", "")
        seg_region = "-".join(az_name.split("-")[:-1]) if az_name else region
        lines += [
            f'resource "google_compute_subnetwork" "{seg_id}" {{',
            f'  name          = "{seg_name}"',
            f'  ip_cidr_range = "{cidr}"',
            f'  region        = "{seg_region}"',
            f"  network       = google_compute_network.{vpc_id}.id",
            "}",
            "",
        ]

    return lines


def _security_group_blocks_gcp(vpc_name: str, vpc_id: str, ctx: dict) -> list[str]:
    lines: list[str] = []
    for sg in ctx["sgs_by_vpc"].get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_id = tf_id(sg_name)
        rules_for_sg = sg.get("rules") or []
        active_rules = [r for r in rules_for_sg if not r.get("disabled")]

        if not active_rules:
            # Emit a stub firewall with default allow-tcp
            lines += [
                f'resource "google_compute_firewall" "{sg_id}" {{',
                f'  name    = "{sg_name}"',
                f"  network = google_compute_network.{vpc_id}.name",
                "  allow {",
                '    protocol = "tcp"',
                "  }",
                "}",
                "",
            ]
            continue

        # GCP: one google_compute_firewall per direction (ingress/egress rules grouped)
        ingress_rules = [r for r in active_rules if r.get("direction", "ingress") == "ingress"]
        egress_rules = [r for r in active_rules if r.get("direction", "ingress") == "egress"]

        # A policy-derived rule has its own peer range, so it gets its own firewall,
        # targeted at the instances of its segment by tag.
        policy_derived = bool(sg.get("policy_derived"))
        firewalls: list[tuple[str, list[dict]]] = []
        for direction, dir_rules in [("ingress", ingress_rules), ("egress", egress_rules)]:
            if policy_derived:
                firewalls += [(direction, [rule]) for rule in dir_rules]
            elif dir_rules:
                firewalls.append((direction, dir_rules))

        for n, (direction, dir_rules) in enumerate(firewalls):
            fw_id = f"{sg_id}_{direction}" if not policy_derived else f"{sg_id}_{n}_{direction}"
            fw_name = fw_id.replace("_", "-") if policy_derived else f"{sg_name}-{direction}"
            lines += [f'resource "google_compute_firewall" "{fw_id}" {{']
            lines += [
                f'  name      = "{fw_name}"',
                f"  network   = google_compute_network.{vpc_id}.name",
                f'  direction = "{direction.upper()}"',
            ]
            if policy_derived:
                peer_key, ranges_key = (
                    ("source_cidr", "source_ranges") if direction == "ingress" else ("dest_cidr", "destination_ranges")
                )
                lines += [
                    f"  priority  = {dir_rules[0].get('priority') or 1000}",
                    f'  {ranges_key} = ["{dir_rules[0].get(peer_key)}"]',
                    f'  target_tags = ["{sg_id}"]',
                ]
            for rule in dir_rules:
                proto = rule.get("protocol", "tcp")
                gcp_proto = "all" if proto == "any" else proto
                p_start = rule.get("port_start")
                p_end = rule.get("port_end")
                if p_start and proto not in ("icmp", "any"):
                    port_str = f"{p_start}-{p_end}" if p_end and p_end != p_start else str(p_start)
                    lines += [
                        "  allow {",
                        f'    protocol = "{gcp_proto}"',
                        f'    ports    = ["{port_str}"]',
                        "  }",
                    ]
                else:
                    lines += [
                        "  allow {",
                        f'    protocol = "{gcp_proto}"',
                        "  }",
                    ]
            lines += ["}", ""]

    return lines


def _instance_blocks_gcp(vpc_name: str, ctx: dict) -> list[str]:
    lines: list[str] = []
    for inst in ctx["inst_by_vpc"].get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_id = tf_id(inst_name)
        az_name = (inst.get("availability_zone") or {}).get("name", "")
        seg_ref = inst.get("network_segment") or {}
        subnetwork_ref = (
            f"google_compute_subnetwork.{tf_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        )
        lines += [
            f'resource "google_compute_instance" "{inst_id}" {{',
            f'  name         = "{inst_name}"',
            f'  machine_type = "{inst.get("instance_type", "")}"',
            f'  zone         = "{az_name}"',
            "  boot_disk {",
            "    initialize_params {",
            f'      image = "{inst.get("image", "")}"',
            "    }",
            "  }",
            "  network_interface {",
            f"    subnetwork = {subnetwork_ref}",
            "  }",
        ]
        policy_sgs = {sg["name"] for sg in ctx["sgs_by_vpc"].get(vpc_name) or [] if sg.get("policy_derived")}
        tags = [tf_id(sg["name"]) for sg in inst.get("security_groups") or [] if sg.get("name") in policy_sgs]
        if tags:
            quoted = ", ".join(f'"{tag}"' for tag in tags)
            lines.append(f"  tags = [{quoted}]")
        lines += [
            f'  labels = {{ name = "{tf_id(inst_name)}", managed_by = "infrahub" }}',
            "}",
            "",
        ]

    return lines


def _public_ip_blocks_gcp(ctx: dict, region: str) -> list[str]:
    lines: list[str] = []
    # Public IPs (GCP compute addresses)
    for pip in ctx["public_ips"]:
        pip_name = pip.get("name", "")
        pip_id = tf_id(pip_name)
        lines += [
            f'resource "google_compute_address" "{pip_id}" {{',
            f'  name   = "{pip_name}"',
            f'  region = "{region}"',
            "}",
            "",
        ]

    return lines


def _nat_blocks_gcp(vpc_name: str, vpc_id: str, ctx: dict, region: str) -> list[str]:
    lines: list[str] = []
    # Cloud Router + NAT
    for nat in ctx["nats_by_vpc"].get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_id = tf_id(nat_name)
        router_id = f"router_{nat_id}"
        lines += [
            f'resource "google_compute_router" "{router_id}" {{',
            f'  name    = "{nat_name}-router"',
            f'  region  = "{region}"',
            f"  network = google_compute_network.{vpc_id}.id",
            "}",
            "",
            f'resource "google_compute_router_nat" "{nat_id}" {{',
            f'  name                               = "{nat_name}"',
            f"  router                             = google_compute_router.{router_id}.name",
            f'  region                             = "{region}"',
            '  nat_ip_allocate_option             = "AUTO_ONLY"',
            '  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"',
            "}",
            "",
        ]

    return lines


def _route_blocks_gcp(vpc_name: str, vpc_id: str, ctx: dict) -> list[str]:
    lines: list[str] = []
    # Routes
    for route in ctx["routes"]:
        route_rt = (route.get("route_table") or {}).get("name", "")
        # For GCP, filter routes belonging to this VPC via route table grouping
        rt_obj = next((rt for rt in (ctx["rts_by_vpc"].get(vpc_name) or []) if rt.get("name") == route_rt), None)
        if rt_obj is None:
            continue
        route_name = route.get("name", "")
        dest = (route.get("destination") or {}).get("prefix", "")
        igw_ref = (route.get("internet_gateway") or {}).get("name", "")
        route_id = tf_id(route_name) if route_name else f"route_{vpc_id}_{tf_id(dest)}"
        lines += [
            f'resource "google_compute_route" "{route_id}" {{',
            f'  name             = "{route_name or dest}"',
            f"  network          = google_compute_network.{vpc_id}.id",
            f'  dest_range       = "{dest}"',
            "  priority         = 1000",
        ]
        if igw_ref:
            lines.append('  next_hop_gateway = "default-internet-gateway"')
        lines += ["}", ""]

    return lines


def _vpn_gateway_blocks_gcp(vpc_name: str, vpc_id: str, ctx: dict, region: str) -> list[str]:
    lines: list[str] = []
    # VPN Gateways
    for vpngw in ctx["vpngws_by_vpc"].get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_id = tf_id(vpngw_name)
        lines += [
            f'resource "google_compute_ha_vpn_gateway" "{vpngw_id}" {{',
            f'  name    = "{vpngw_name}"',
            f"  network = google_compute_network.{vpc_id}.id",
            f'  region  = "{region}"',
            "}",
            "",
        ]

    return lines


def _instance_group_blocks_gcp(vpc_name: str, vpc_id: str, ctx: dict, region: str) -> list[str]:
    lines: list[str] = []
    # Auto Scaling Groups → GCP instance group manager + autoscaler
    for asg in ctx["asgs_by_vpc"].get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_id = tf_id(asg_name)
        it_id = f"{asg_id}_template"
        igm_id = f"{asg_id}_igm"
        lines += [
            f'resource "google_compute_instance_template" "{it_id}" {{',
            f'  name         = "{asg_name}-template"',
            f'  machine_type = "{asg.get("instance_type", "")}"',
            f'  region       = "{region}"',
            "  disk {",
            f'    source_image = "{asg.get("image", "")}"',
            "    auto_delete  = true",
            "    boot         = true",
            "  }",
            "  network_interface {",
            f"    network = google_compute_network.{vpc_id}.id",
            "  }",
            "}",
            "",
            f'resource "google_compute_region_instance_group_manager" "{igm_id}" {{',
            f'  name               = "{asg_name}"',
            f'  region             = "{region}"',
            f'  base_instance_name = "{asg_name}"',
            f"  target_size        = {asg.get('desired_capacity', 1)}",
            "  version {",
            f"    instance_template = google_compute_instance_template.{it_id}.id",
            "  }",
            "}",
            "",
            f'resource "google_compute_region_autoscaler" "{asg_id}" {{',
            f'  name   = "{asg_name}-autoscaler"',
            f'  region = "{region}"',
            f"  target = google_compute_region_instance_group_manager.{igm_id}.id",
            "  autoscaling_policy {",
            f"    min_replicas = {asg.get('min_size', 1)}",
            f"    max_replicas = {asg.get('max_size', 1)}",
            "    cooldown_period = 60",
            "  }",
            "}",
            "",
        ]

    return lines


def _account_blocks_gcp(ctx: dict, region: str) -> list[str]:
    lines: list[str] = []

    for cgw in ctx["customer_gateways"]:
        cgw_name = cgw.get("name", "")
        cgw_id = tf_id(cgw_name)
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += [
            f'resource "google_compute_external_vpn_gateway" "{cgw_id}" {{',
            f'  name            = "{cgw_name}"',
            '  redundancy_type = "SINGLE_IP_INTERNALLY_REDUNDANT"',
            "  interface {",
            "    id         = 0",
            f'    ip_address = "{ip_val}"',
            "  }",
            "}",
            "",
        ]

    for peering in ctx["peerings"]:
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        req_id = tf_id(f"{req_vpc}_to_{acc_vpc}")
        acc_id = tf_id(f"{acc_vpc}_to_{req_vpc}")
        lines += [
            f'resource "google_compute_network_peering" "{req_id}" {{',
            f'  name         = "{req_vpc}-to-{acc_vpc}"',
            f"  network      = google_compute_network.{tf_id(req_vpc)}.id",
            f"  peer_network = google_compute_network.{tf_id(acc_vpc)}.id",
            "}",
            "",
            f'resource "google_compute_network_peering" "{acc_id}" {{',
            f'  name         = "{acc_vpc}-to-{req_vpc}"',
            f"  network      = google_compute_network.{tf_id(acc_vpc)}.id",
            f"  peer_network = google_compute_network.{tf_id(req_vpc)}.id",
            "}",
            "",
        ]

    for vif in ctx["vifs"]:
        vif_name = vif.get("name", "")
        vif_id = tf_id(vif_name)
        lines += [
            f'resource "google_compute_interconnect_attachment" "{vif_id}" {{',
            f'  name         = "{vif_name}"',
            '  router       = ""',
            f'  region       = "{region}"',
            '  interconnect = ""',
            f"  vlan_tag8021q = {vif.get('vlan_id') or 0}",
            "}",
            "",
        ]

    return lines
