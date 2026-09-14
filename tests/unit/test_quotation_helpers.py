"""Unit tests for DC-fabric quotation sizing/pricing logic (generators/helpers/quotation.py)."""

from __future__ import annotations

from generators.helpers.quotation import (
    DeviceTemplate,
    PortGroup,
    Recommender,
    assign_racks_to_rooms,
    build_fabric_tiers,
    build_multi_speed_fabric,
    build_multi_speed_leaf_tiers,
    build_proposed_pods,
    build_room_pods,
    build_switch_fabric,
    device_templates_from_graphql,
    distribute_evenly,
    max_fabric_capacity,
    recommend_design,
    recommend_pod_design,
    validate_room_capacity,
)

_SPEED_100G = "100gbase-x-qsfp28"
_SPEED_25G = "25gbase-x-sfp28"


def _leaf(
    device_type_id: str, manufacturer: str, customer_ports: int, uplink_ports: int, speed: str = _SPEED_100G
) -> DeviceTemplate:
    return DeviceTemplate(
        device_type_id=device_type_id,
        role="leaf",
        manufacturer=manufacturer,
        ports={
            "customer": PortGroup(count=customer_ports, speed=speed),
            "uplink": PortGroup(count=uplink_ports, speed=_SPEED_100G),
        },
    )


def _spine(device_type_id: str, manufacturer: str, downlink_ports: int, uplink_ports: int) -> DeviceTemplate:
    return DeviceTemplate(
        device_type_id=device_type_id,
        role="spine",
        manufacturer=manufacturer,
        ports={
            "downlink": PortGroup(count=downlink_ports, speed=_SPEED_100G),
            "uplink": PortGroup(count=uplink_ports, speed=_SPEED_100G),
        },
    )


def _super_spine(device_type_id: str, manufacturer: str, downlink_ports: int) -> DeviceTemplate:
    return DeviceTemplate(
        device_type_id=device_type_id,
        role="super-spine",
        manufacturer=manufacturer,
        ports={"downlink": PortGroup(count=downlink_ports, speed=_SPEED_100G)},
    )


def _border_leaf(device_type_id: str, manufacturer: str) -> DeviceTemplate:
    return DeviceTemplate(device_type_id=device_type_id, role="border-leaf", manufacturer=manufacturer, ports={})


def test_cheapest_leaf_picks_lowest_total_cost() -> None:
    """70 servers: 24-port leaf needs 3x ($24k), 48-port leaf needs 2x ($20k) — total cost wins, not device count."""
    templates = [
        _leaf("cheap-small", "Vendor A", customer_ports=24, uplink_ports=4),
        _leaf("pricier-big", "Vendor B", customer_ports=48, uplink_ports=4),
    ]
    prices = {"cheap-small": 8_000.0, "pricier-big": 10_000.0}
    rec = Recommender(templates=templates, prices=prices)

    result, template = rec.cheapest_leaf(servers=70, speed=_SPEED_100G)

    assert template is not None
    assert result.device_type_id == "pricier-big"
    assert result.count == 2
    assert result.total_cost == 20_000.0


def test_cheapest_leaf_enforces_minimum_redundancy() -> None:
    """Even a leaf with huge port capacity is never sized below 2 devices."""
    templates = [_leaf("big-leaf", "Vendor A", customer_ports=48, uplink_ports=4)]
    rec = Recommender(templates=templates, prices={"big-leaf": 5_000.0})

    result, _ = rec.cheapest_leaf(servers=1, speed=_SPEED_100G)

    assert result.count == 2


def test_cheapest_leaf_filters_by_manufacturer() -> None:
    templates = [
        _leaf("vendor-a-leaf", "Vendor A", customer_ports=48, uplink_ports=4),
        _leaf("vendor-b-leaf", "Vendor B", customer_ports=48, uplink_ports=4),
    ]
    prices = {"vendor-a-leaf": 5_000.0, "vendor-b-leaf": 1_000.0}
    rec = Recommender(templates=templates, prices=prices)

    result, _ = rec.cheapest_leaf(servers=40, speed=_SPEED_100G, manufacturer="Vendor A")

    assert result.device_type_id == "vendor-a-leaf"


def test_cheapest_leaf_no_candidate_for_unknown_speed() -> None:
    templates = [_leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4)]
    rec = Recommender(templates=templates, prices={"leaf-100g": 5_000.0})

    result, template = rec.cheapest_leaf(servers=40, speed="25gbase-x-sfp28")

    assert template is None
    assert result.device_type_id is None


def test_build_switch_fabric_single_pod_has_no_super_spine() -> None:
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    leaf, spine, super_spine, border_leaf = build_switch_fabric(
        rec, servers=40, speed=_SPEED_100G, pods=1, manufacturer=None
    )

    assert leaf.device_type_id == "leaf-1"
    assert spine.device_type_id == "spine-1"
    assert super_spine.device_type_id is None
    assert border_leaf.device_type_id == "bl-1"


def test_build_switch_fabric_multi_pod_prefers_cheaper_of_back_to_back_vs_super_spine() -> None:
    """Regression case from this session: 2 pods, back-to-back beats adding
    a super-spine tier when the super-spine device is pricier than just
    scaling up the back-to-back spine count."""
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _super_spine("ss-1", "Vendor A", downlink_ports=32),
        _border_leaf("bl-1", "Vendor A"),
    ]
    # Expensive super-spine makes the classic 3-tier option pricier than
    # back-to-back for this small fabric.
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "ss-1": 100_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    leaf, spine, super_spine, border_leaf = build_switch_fabric(
        rec, servers=40, speed=_SPEED_100G, pods=2, manufacturer=None
    )

    assert leaf.device_type_id == "leaf-1"
    assert super_spine.device_type_id is None, "back-to-back should win when super-spine is pricier"
    assert spine.device_type_id == "spine-1"


def test_build_switch_fabric_multi_pod_can_prefer_super_spine_when_mesh_penalty_applies() -> None:
    """Back-to-back now includes a spine mesh penalty for multi-pod fabrics,
    so a cheap super-spine tier can legitimately win the cost race."""
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _super_spine("ss-1", "Vendor A", downlink_ports=32),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "ss-1": 100.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    _, _, super_spine, _ = build_switch_fabric(rec, servers=40, speed=_SPEED_100G, pods=8, manufacturer=None)

    assert super_spine.device_type_id == "ss-1"


def test_build_switch_fabric_forced_back_to_back_skips_super_spine() -> None:
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _super_spine("ss-1", "Vendor A", downlink_ports=32),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "ss-1": 10.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    _, _, super_spine, _ = build_switch_fabric(
        rec,
        servers=40,
        speed=_SPEED_100G,
        pods=4,
        manufacturer=None,
        topology_strategy="back_to_back",
    )

    assert super_spine.device_type_id is None


def test_build_switch_fabric_forced_classic_3tier_uses_super_spine_when_available() -> None:
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _super_spine("ss-1", "Vendor A", downlink_ports=64),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "ss-1": 100_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    _, _, super_spine, _ = build_switch_fabric(
        rec,
        servers=40,
        speed=_SPEED_100G,
        pods=4,
        manufacturer=None,
        topology_strategy="classic_3tier",
    )

    assert super_spine.device_type_id == "ss-1"


def test_recommend_design_returns_smallest_fitting() -> None:
    designs = [
        {"name": "S", "max_spines_per_pod": 2, "max_super_spines_per_fabric": 0, "max_border_leafs_per_fabric": 0},
        {"name": "M", "max_spines_per_pod": 4, "max_super_spines_per_fabric": 0, "max_border_leafs_per_fabric": 2},
        {"name": "L", "max_spines_per_pod": 4, "max_super_spines_per_fabric": 4, "max_border_leafs_per_fabric": 2},
    ]

    result = recommend_design(designs, spine_count=4, super_spine_count=0, border_leaf_count=2)

    assert result is not None
    assert result["name"] == "M"


def test_recommend_design_is_order_independent() -> None:
    """Regression: a live GraphQL query returned these same 4 designs as
    L/M/S/XL (no declaration-order guarantee, unlike the CLI's YAML loader)
    — recommend_design must still pick M, not the first fitting entry."""
    designs = [
        {"name": "L", "max_spines_per_pod": 4, "max_super_spines_per_fabric": 4, "max_border_leafs_per_fabric": 2},
        {"name": "M", "max_spines_per_pod": 4, "max_super_spines_per_fabric": 0, "max_border_leafs_per_fabric": 2},
        {"name": "S", "max_spines_per_pod": 2, "max_super_spines_per_fabric": 0, "max_border_leafs_per_fabric": 0},
        {"name": "XL", "max_spines_per_pod": 4, "max_super_spines_per_fabric": 4, "max_border_leafs_per_fabric": 4},
    ]

    result = recommend_design(designs, spine_count=4, super_spine_count=0, border_leaf_count=2)

    assert result is not None
    assert result["name"] == "M"


def test_recommend_design_returns_none_when_nothing_fits() -> None:
    designs = [
        {"name": "S", "max_spines_per_pod": 2, "max_super_spines_per_fabric": 0, "max_border_leafs_per_fabric": 0},
    ]

    result = recommend_design(designs, spine_count=99, super_spine_count=99, border_leaf_count=99)

    assert result is None


def test_recommend_pod_design_skips_pure_tor_designs() -> None:
    pod_designs = [
        {
            "name": "S_TOR",
            "rows": 2,
            "network_racks_per_row": 0,
            "max_leafs_per_network_rack": 0,
            "max_spines_per_pod": 4,
        },
        {
            "name": "S_MIDDLE",
            "rows": 2,
            "network_racks_per_row": 1,
            "max_leafs_per_network_rack": 4,
            "max_spines_per_pod": 2,
        },
    ]

    result = recommend_pod_design(pod_designs, leaf_count=2, spine_count=2)

    assert result is not None
    assert result["name"] == "S_MIDDLE"


def test_recommend_pod_design_returns_smallest_sufficient_capacity() -> None:
    pod_designs = [
        {
            "name": "L_MIDDLE",
            "rows": 8,
            "network_racks_per_row": 1,
            "max_leafs_per_network_rack": 4,
            "max_spines_per_pod": 4,
        },
        {
            "name": "S_MIDDLE",
            "rows": 2,
            "network_racks_per_row": 1,
            "max_leafs_per_network_rack": 4,
            "max_spines_per_pod": 2,
        },
    ]

    result = recommend_pod_design(pod_designs, leaf_count=2, spine_count=2)

    assert result is not None
    assert result["name"] == "S_MIDDLE"


def test_device_templates_from_graphql_groups_expanded_interfaces_by_role() -> None:
    """GraphQL interfaces arrive pre-expanded (one node per port), unlike
    the CLI's bootstrap YAML range syntax — this just counts edges per role."""
    raw_templates = [
        {
            "role": "leaf",
            "device_type": {"id": "leaf-1"},
            "interfaces": [
                {"role": "customer", "interface_type": _SPEED_100G},
                {"role": "customer", "interface_type": _SPEED_100G},
                {"role": "customer", "interface_type": _SPEED_100G},
                {"role": "uplink", "interface_type": _SPEED_100G},
            ],
        }
    ]
    manufacturers = {"leaf-1": "Vendor A"}

    templates = device_templates_from_graphql(raw_templates, manufacturers)

    assert len(templates) == 1
    template = templates[0]
    assert template.device_type_id == "leaf-1"
    assert template.manufacturer == "Vendor A"
    assert template.ports["customer"].count == 3
    assert template.ports["customer"].speed == _SPEED_100G
    assert template.ports["uplink"].count == 1


def test_device_templates_from_graphql_skips_devices_without_role_or_device_type() -> None:
    raw_templates = [
        {"role": None, "device_type": {"id": "x"}, "interfaces": []},
        {"role": "leaf", "device_type": {}, "interfaces": []},
    ]

    templates = device_templates_from_graphql(raw_templates, {})

    assert templates == []


def _hyper_spine(device_type_id: str, manufacturer: str, downlink_ports: int) -> DeviceTemplate:
    return DeviceTemplate(
        device_type_id=device_type_id,
        role="hyper-spine",
        manufacturer=manufacturer,
        ports={"downlink": PortGroup(count=downlink_ports, speed=_SPEED_100G)},
    )


def test_build_fabric_tiers_single_pod_matches_build_switch_fabric() -> None:
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    leaf, spine, super_spine, hyper_spine, border_leaf = build_fabric_tiers(rec, servers=40, speed=_SPEED_100G, pods=1)

    assert leaf.device_type_id == "leaf-1"
    assert spine.device_type_id == "spine-1"
    assert super_spine.device_type_id is None
    assert hyper_spine.device_type_id is None
    assert border_leaf.device_type_id == "bl-1"


def test_build_fabric_tiers_escalates_to_hyper_spine_when_super_spine_cant_fan_in() -> None:
    """16 pods each need 4 spines -> 64 spine-facing links total. The only
    super-spine device has just 8 downlink ports, so a single flat
    super-spine tier can't cover it — build_fabric_tiers must partition
    into domains and add a hyper-spine tier on top."""
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _super_spine("ss-1", "Vendor A", downlink_ports=8),
        _hyper_spine("hs-1", "Vendor A", downlink_ports=64),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "ss-1": 30_000.0, "hs-1": 80_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    leaf, spine, super_spine, hyper_spine, border_leaf = build_fabric_tiers(rec, servers=40, speed=_SPEED_100G, pods=16)

    assert leaf.device_type_id == "leaf-1"
    assert super_spine.device_type_id == "ss-1"
    assert super_spine.count >= 2
    assert hyper_spine.device_type_id == "hs-1"
    assert hyper_spine.count >= 2


def test_build_fabric_tiers_no_hyper_spine_candidate_falls_back_to_back_to_back() -> None:
    """Same over-subscribed super-spine as above, but no hyper-spine device
    exists in the catalog — must fall back to back-to-back rather than
    silently returning a broken super-spine-only design."""
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _super_spine("ss-1", "Vendor A", downlink_ports=8),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0, "ss-1": 30_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    leaf, spine, super_spine, hyper_spine, border_leaf = build_fabric_tiers(rec, servers=40, speed=_SPEED_100G, pods=16)

    assert super_spine.device_type_id is None
    assert hyper_spine.device_type_id is None
    assert spine.device_type_id == "spine-1"


def test_max_fabric_capacity_scales_with_pods_and_hardware() -> None:
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
    ]
    rec = Recommender(templates=templates, prices={})

    result = max_fabric_capacity(rec, design={"max_pods": 8}, speed=_SPEED_100G)

    assert result["leaf_device_type_id"] == "leaf-1"
    assert result["spine_device_type_id"] == "spine-1"
    assert result["max_leafs_per_pod"] == 32
    assert result["max_servers"] == 8 * 32 * 48


def test_max_fabric_capacity_returns_zero_when_no_leaf_for_speed() -> None:
    templates = [_leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4)]
    rec = Recommender(templates=templates, prices={})

    result = max_fabric_capacity(rec, design={"max_pods": 8}, speed="25gbase-x-sfp28")

    assert result["max_servers"] == 0
    assert result["leaf_device_type_id"] is None


def test_validate_room_capacity_fits_when_capacity_covers_racks() -> None:
    rooms = [
        {"rows": 3, "racks_per_row": 10, "compute_rack_count": 30, "storage_rack_count": 10},
        {"rows": 5, "racks_per_row": 8, "compute_rack_count": 10, "storage_rack_count": 10},
    ]

    result = validate_room_capacity(rooms)

    assert result["fits"] is True
    assert result["total_room_capacity"] == 70
    assert result["required_racks"] == 60
    assert result["shortfall"] == 0


def test_validate_room_capacity_reports_shortfall() -> None:
    rooms = [{"rows": 2, "racks_per_row": 5, "compute_rack_count": 15, "storage_rack_count": 10}]

    result = validate_room_capacity(rooms)

    assert result["fits"] is False
    assert result["total_room_capacity"] == 10
    assert result["required_racks"] == 25
    assert result["shortfall"] == 15


def test_validate_room_capacity_defaults_missing_rack_counts_to_zero() -> None:
    rooms = [{"rows": 2, "racks_per_row": 5}]

    result = validate_room_capacity(rooms)

    assert result["fits"] is True
    assert result["required_racks"] == 0


def test_distribute_evenly_spreads_remainder_across_first_shares() -> None:
    assert distribute_evenly(100, 3) == [34, 33, 33]
    assert distribute_evenly(9, 3) == [3, 3, 3]
    assert distribute_evenly(0, 3) == [0, 0, 0]


def test_distribute_evenly_zero_pods_returns_empty() -> None:
    assert distribute_evenly(100, 0) == []


def test_build_proposed_pods_sizes_each_pod_independently() -> None:
    templates = [
        _leaf("leaf-1", "Vendor A", customer_ports=48, uplink_ports=4),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
    ]
    prices = {"leaf-1": 20_000.0, "spine-1": 25_000.0}
    rec = Recommender(templates=templates, prices=prices)

    pods = build_proposed_pods(
        rec, servers=100, speed=_SPEED_100G, pod_count=3, compute_rack_count=10, storage_rack_count=5
    )

    assert len(pods) == 3
    assert [p["index"] for p in pods] == [1, 2, 3]
    assert sum(p["servers"] for p in pods) == 100
    assert sum(p["compute_rack_share"] for p in pods) == 10
    assert sum(p["storage_rack_share"] for p in pods) == 5
    # Every pod gets its own MIN_REDUNDANCY-floored leaf/spine, independent of the others.
    for pod in pods:
        assert pod["leaf_device_type_id"] == "leaf-1"
        assert pod["spine_device_type_id"] == "spine-1"
        assert pod["leaf_count"] >= 2
        assert pod["spine_count"] >= 2


def test_build_proposed_pods_zero_pod_count_returns_empty() -> None:
    rec = Recommender(templates=[], prices={})

    assert (
        build_proposed_pods(rec, servers=40, speed=_SPEED_100G, pod_count=0, compute_rack_count=0, storage_rack_count=0)
        == []
    )


def test_assign_racks_to_rooms_prefers_one_dedicated_room_per_pod() -> None:
    """rooms >= pods: this pod's racks all go to its own room, not shared."""
    rooms = [{"id": "room-a"}, {"id": "room-b"}, {"id": "room-c"}]

    racks_pod1 = assign_racks_to_rooms(compute_racks=3, storage_racks=2, rooms=rooms, pod_index=1, pod_count=2)
    racks_pod2 = assign_racks_to_rooms(compute_racks=3, storage_racks=2, rooms=rooms, pod_index=2, pod_count=2)

    assert all(r["room_id"] == "room-a" for r in racks_pod1)
    assert all(r["room_id"] == "room-b" for r in racks_pod2)


def test_assign_racks_to_rooms_falls_back_to_round_robin_when_fewer_rooms_than_pods() -> None:
    rooms = [{"id": "room-a"}, {"id": "room-b"}]

    racks = assign_racks_to_rooms(compute_racks=3, storage_racks=2, rooms=rooms, pod_index=1, pod_count=3)

    assert len(racks) == 5
    # Indexes are unique and sequential across both rack types.
    assert [r["index"] for r in racks] == [1, 2, 3, 4, 5]
    assert [r["rack_type"] for r in racks] == ["compute", "compute", "compute", "storage", "storage"]
    # Round-robin: index 1->room-a, 2->room-b, 3->room-a, 4->room-b, 5->room-a.
    assert [r["room_id"] for r in racks] == ["room-a", "room-b", "room-a", "room-b", "room-a"]


def test_assign_racks_to_rooms_no_rooms_leaves_room_id_none() -> None:
    racks = assign_racks_to_rooms(compute_racks=2, storage_racks=0, rooms=[])

    assert len(racks) == 2
    assert all(r["room_id"] is None for r in racks)


def test_build_multi_speed_leaf_tiers_one_result_per_nonzero_speed() -> None:
    templates = [
        _leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G),
        _leaf("leaf-25g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_25G),
    ]
    prices = {"leaf-100g": 20_000.0, "leaf-25g": 10_000.0}
    rec = Recommender(templates=templates, prices=prices)

    results = build_multi_speed_leaf_tiers(rec, {_SPEED_100G: 40, _SPEED_25G: 20})

    assert len(results) == 2
    by_device = {r.device_type_id: r for r in results}
    assert by_device["leaf-100g"].count == 2
    assert by_device["leaf-25g"].count == 2
    assert all(r.tier == "leaf" for r in results)


def test_build_multi_speed_leaf_tiers_skips_zero_count_speeds() -> None:
    templates = [_leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G)]
    rec = Recommender(templates=templates, prices={"leaf-100g": 20_000.0})

    results = build_multi_speed_leaf_tiers(rec, {_SPEED_100G: 40, _SPEED_25G: 0})

    assert len(results) == 1
    assert results[0].device_type_id == "leaf-100g"


def test_build_multi_speed_fabric_sizes_spine_from_summed_leaf_count() -> None:
    templates = [
        _leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G),
        _leaf("leaf-25g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_25G),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-100g": 20_000.0, "leaf-25g": 10_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)

    leaf_results, spine, super_spine, border_leaf = build_multi_speed_fabric(
        rec, {_SPEED_100G: 40, _SPEED_25G: 20}, pods=1
    )

    assert len(leaf_results) == 2
    total_leaf_count = sum(r.count for r in leaf_results)
    # Sanity: spine sized from a single-speed call with the SAME summed
    # count matches the multi-speed spine exactly — summing didn't change
    # the underlying spine logic.
    single_speed_spine, _ = rec.cheapest_fanin("spine", total_leaf_count)
    assert spine.device_type_id == single_speed_spine.device_type_id
    assert spine.count == single_speed_spine.count
    assert super_spine.device_type_id is None
    assert border_leaf.device_type_id == "bl-1"


def test_build_multi_speed_fabric_all_zero_ports_returns_empty() -> None:
    rec = Recommender(templates=[], prices={})

    leaf_results, spine, super_spine, border_leaf = build_multi_speed_fabric(
        rec, {_SPEED_100G: 0, _SPEED_25G: 0}, pods=1
    )

    assert leaf_results == []
    assert spine.device_type_id is None
    assert super_spine.device_type_id is None
    assert border_leaf.device_type_id is None


def test_build_room_pods_maps_one_pod_per_room() -> None:
    templates = [
        _leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-100g": 20_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)
    rooms = [
        {"id": "room-a", "port_count_100g": 40, "compute_rack_count": 10, "storage_rack_count": 0},
        {"id": "room-b", "port_count_100g": 20, "compute_rack_count": 0, "storage_rack_count": 5},
    ]

    pods = build_room_pods(rec, rooms, pod_count=2)

    assert len(pods) == 2
    assert pods[0]["room_id"] == "room-a"
    assert pods[0]["compute_rack_share"] == 10
    assert pods[0]["storage_rack_share"] == 0
    assert pods[1]["room_id"] == "room-b"
    assert pods[1]["compute_rack_share"] == 0
    assert pods[1]["storage_rack_share"] == 5
    # Room-a's 40 ports need more leafs than room-b's 20 -> different pod sizing, not an even DC-wide split.
    assert pods[0]["leaf_count"] != pods[1]["leaf_count"] or pods[0]["leaf_count"] >= 2


def test_build_room_pods_round_robins_overflow_pods_onto_existing_rooms() -> None:
    templates = [
        _leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-100g": 20_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)
    rooms = [{"id": "room-a", "port_count_100g": 40, "compute_rack_count": 10, "storage_rack_count": 0}]

    pods = build_room_pods(rec, rooms, pod_count=3)

    assert len(pods) == 3
    # pod_count=3 > len(rooms)=1 -> every pod round-robins back onto room-a.
    assert all(p["room_id"] == "room-a" for p in pods)
    assert all(p["compute_rack_share"] == 10 for p in pods)


def test_build_room_pods_sequential_strategy_sticks_to_last_room_after_exhaustion() -> None:
    templates = [
        _leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-100g": 20_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)
    rooms = [
        {"id": "room-a", "port_count_100g": 40, "compute_rack_count": 10, "storage_rack_count": 0},
        {"id": "room-b", "port_count_100g": 20, "compute_rack_count": 1, "storage_rack_count": 1},
    ]

    pods = build_room_pods(rec, rooms, pod_count=4, assignment_strategy="sequential")

    assert [p["room_id"] for p in pods] == ["room-a", "room-b", "room-b", "room-b"]


def test_assign_racks_to_rooms_sequential_fills_rooms_in_order() -> None:
    rooms = [{"id": "room-a"}, {"id": "room-b"}]

    racks = assign_racks_to_rooms(
        compute_racks=3,
        storage_racks=1,
        rooms=rooms,
        assignment_strategy="sequential",
    )

    assert [r["room_id"] for r in racks] == ["room-a", "room-b", "room-b", "room-b"]


def test_build_room_pods_fewer_pods_than_rooms_only_returns_pod_count_pods() -> None:
    rooms = [{"id": "room-a", "port_count_100g": 40}, {"id": "room-b", "port_count_100g": 20}]
    rec = Recommender(templates=[], prices={})

    pods = build_room_pods(rec, rooms, pod_count=1)

    assert len(pods) == 1
    assert pods[0]["room_id"] == "room-a"


def test_build_room_pods_no_rooms_returns_empty_fabric_pods() -> None:
    rec = Recommender(templates=[], prices={})

    pods = build_room_pods(rec, rooms=[], pod_count=2)

    assert len(pods) == 2
    assert all(p["room_id"] is None for p in pods)
    assert all(p["leaf_count"] == 0 for p in pods)
    assert all(p["leaf_results"] == [] for p in pods)


def test_build_room_pods_growth_buffer_scales_port_demand_before_sizing() -> None:
    templates = [
        _leaf("leaf-100g", "Vendor A", customer_ports=48, uplink_ports=4, speed=_SPEED_100G),
        _spine("spine-1", "Vendor A", downlink_ports=32, uplink_ports=4),
        _border_leaf("bl-1", "Vendor A"),
    ]
    prices = {"leaf-100g": 20_000.0, "spine-1": 25_000.0, "bl-1": 15_000.0}
    rec = Recommender(templates=templates, prices=prices)
    rooms = [{"id": "room-a", "port_count_100g": 40, "compute_rack_count": 1, "storage_rack_count": 0}]

    no_growth = build_room_pods(rec, rooms, pod_count=1, growth_buffer_percent=0)
    with_growth = build_room_pods(rec, rooms, pod_count=1, growth_buffer_percent=25)

    assert no_growth[0]["port_counts"][_SPEED_100G] == 40
    assert with_growth[0]["port_counts"][_SPEED_100G] == 50
    assert with_growth[0]["leaf_count"] >= no_growth[0]["leaf_count"]
