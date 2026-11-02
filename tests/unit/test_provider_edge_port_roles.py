"""The provider-facing interface roles (cloud, saas, partner) and the edge template that carries them."""

from __future__ import annotations

import yaml

from tests.integration.demo_data import ROOT

_DCIM = ROOT / "schemas" / "base" / "dcim.yml"
_TEMPLATES = ROOT / "data" / "bootstrap" / "10_physical_devices_templates_cisco_nxos.yaml"


def _role_choices() -> dict[str, dict[str, str]]:
    document = yaml.safe_load(_DCIM.read_text())
    interface = next(g for g in document["generics"] if g["name"] == "Interface")
    role = next(a for a in interface["attributes"] if a["name"] == "role")
    return {c["name"]: c for c in role["choices"]}


def _edge_ports() -> dict[str, str]:
    for document in yaml.safe_load_all(_TEMPLATES.read_text()):
        for template in (document or {}).get("spec", {}).get("data", []):
            if template.get("template_name") == "N9K-C9316D-GX_EDGE":
                return {p["name"]: p["role"] for p in template["interfaces"]["data"]}
    raise AssertionError("template not found")


def test_the_three_provider_roles_exist_beside_customer_with_distinct_colors() -> None:
    """Additive choices: customer stays (sdwan_edge, endpoint targets and segment.py use it)."""
    choices = _role_choices()
    new = ("cloud", "saas", "partner")

    assert "customer" in choices and set(new) <= set(choices)
    colors = [choices[n]["color"] for n in (*new, "customer")]
    assert len(set(colors)) == 4
    assert all(choices[n]["description"] for n in new)


def test_the_cage_edge_template_carries_the_roles_on_the_right_ports() -> None:
    """Ethernet1/[1-2] partner, [3-5] cloud, [6-7] saas; the other ports keep their roles."""
    ports = _edge_ports()

    assert ports["Ethernet1/[1-2]"] == "partner"
    assert ports["Ethernet1/[3-5]"] == "cloud"
    assert ports["Ethernet1/[6-7]"] == "saas"
    assert "Ethernet1/[1-7]" not in ports and "customer" not in ports.values()
    assert ports["Ethernet1/8"] == "interlink"
    assert ports["Ethernet1/[9-12]"] == "dci"
    assert ports["Ethernet1/[13-14]"] == "uplink"
    assert ports["Ethernet1/[15-16]"] == "firewall"
