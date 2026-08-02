from __future__ import annotations

from generators.helpers.template_interfaces import (
    build_ethernet_interface_names,
    build_spine_downlink_template_names,
    role_interface_names_or_dynamic,
    template_interface_names_by_role,
)


def test_build_ethernet_interface_names_basic_range() -> None:
    names = build_ethernet_interface_names(count=4)

    assert names == ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3", "Ethernet1/4"]


def test_build_ethernet_interface_names_empty_for_non_positive_count() -> None:
    assert build_ethernet_interface_names(count=0) == []
    assert build_ethernet_interface_names(count=-2) == []


def test_build_spine_downlink_template_names_applies_reservation() -> None:
    names = build_spine_downlink_template_names(
        spine_downlink_ports_per_spine=32,
        reserved_spine_downlinks_per_spine=4,
    )

    assert len(names) == 28
    assert names[0] == "Ethernet1/1"
    assert names[-1] == "Ethernet1/28"


def test_template_interface_names_by_role_filters_model_and_dict_items() -> None:
    interfaces = [
        {"name": "Ethernet1/1", "role": "uplink"},
        {"name": "Ethernet1/2", "role": "downlink"},
        {"name": "Ethernet1/3", "role": "uplink"},
    ]

    names = template_interface_names_by_role(interfaces=interfaces, role="uplink")

    assert names == ["Ethernet1/1", "Ethernet1/3"]


def test_role_interface_names_or_dynamic_uses_fallback_when_role_missing() -> None:
    names = role_interface_names_or_dynamic(
        interfaces=[{"name": "Ethernet1/10", "role": "customer"}],
        role="uplink",
        fallback_count=3,
    )

    assert names == ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]


def test_role_interface_names_or_dynamic_prefers_template_role_interfaces() -> None:
    names = role_interface_names_or_dynamic(
        interfaces=[
            {"name": "Ethernet1/21", "role": "uplink"},
            {"name": "Ethernet1/22", "role": "uplink"},
        ],
        role="uplink",
        fallback_count=8,
    )

    assert names == ["Ethernet1/21", "Ethernet1/22"]
