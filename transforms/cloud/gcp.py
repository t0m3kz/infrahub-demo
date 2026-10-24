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
        rules = sg.get("rules") or []

        # A rule has its own peer range, so it gets its own firewall, targeted at the
        # instances of its segment by tag. Ingress firewalls come first, then egress.
        firewalls = [
            (direction, rule)
            for direction in ("ingress", "egress")
            for rule in rules
            if rule.get("direction", "ingress") == direction
        ]

        for n, (direction, rule) in enumerate(firewalls):
            fw_id = f"{sg_id}_{n}_{direction}"
            peer_key, ranges_key = (
                ("source_cidr", "source_ranges") if direction == "ingress" else ("dest_cidr", "destination_ranges")
            )
            lines += [
                f'resource "google_compute_firewall" "{fw_id}" {{',
                f'  name      = "{fw_id.replace("_", "-")}"',
                f"  network   = google_compute_network.{vpc_id}.name",
                f'  direction = "{direction.upper()}"',
                f"  priority  = {rule.get('priority') or 1000}",
                f'  {ranges_key} = ["{rule.get(peer_key)}"]',
                f'  target_tags = ["{sg_id}"]',
            ]
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
        tags = [tf_id(sg["name"]) for sg in inst.get("security_groups") or [] if sg.get("name")]
        if tags:
            quoted = ", ".join(f'"{tag}"' for tag in tags)
            lines.append(f"  tags = [{quoted}]")
        lines += [
            f'  labels = {{ name = "{tf_id(inst_name)}", managed_by = "infrahub" }}',
            "}",
            "",
        ]

    return lines


def _account_blocks_gcp(ctx: dict, region: str) -> list[str]:
    lines: list[str] = []

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
