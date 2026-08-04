from __future__ import annotations

from generators.helpers.template_interfaces import template_interface_names_by_role


def test_template_interface_names_by_role_filters_model_and_dict_items() -> None:
    interfaces = [
        {"name": "Ethernet1/1", "role": "uplink"},
        {"name": "Ethernet1/2", "role": "downlink"},
        {"name": "Ethernet1/3", "role": "uplink"},
    ]

    names = template_interface_names_by_role(interfaces=interfaces, role="uplink")

    assert names == ["Ethernet1/1", "Ethernet1/3"]


def test_template_interface_names_by_role_no_filter_returns_all() -> None:
    interfaces = [
        {"name": "Ethernet1/1", "role": "uplink"},
        {"name": "Ethernet1/2", "role": "downlink"},
    ]

    names = template_interface_names_by_role(interfaces=interfaces)

    assert names == ["Ethernet1/1", "Ethernet1/2"]
