"""Unit tests for transforms/colocation/virtual_terraform.py (Equinix Network Edge HCL for one virtual colocation).

The Jinja2 template (templates/configs/equinix/virtual_colocation.tf.j2) is pinned byte for byte by
tests/unit/golden/virtual_colocation_london.tf, captured from the pure-Python renderer before it moved to a template.

The fixture is one virtual colocation in London (metro LD): two Arista routers as an HA pair, a Palo Alto firewall
cluster and an SD-WAN device placed away from the routers. Type, package and version values are the ones the
provider's own examples use; they are fixture data, not a statement about what a design should order.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from transforms.colocation.virtual_terraform import render_equinix_network_devices

_GOLDEN = Path(__file__).resolve().parent / "golden" / "virtual_colocation_london.tf"

_ARISTA = {
    "metro_code": "LD",
    "type_code": "ARISTA-ROUTER",
    "package_code": "CloudEOS",
    "version": "4.29.0",
    "core_count": 4,
    "term_length": 12,
}
_PALO_ALTO = {
    "metro_code": "LD",
    "type_code": "PA-VM",
    "package_code": "VM100",
    "version": "10.1.3",
    "core_count": 2,
    "term_length": 12,
}
_SDWAN = {
    "metro_code": "LD",
    "type_code": "FG-SDWAN",
    "package_code": "VM02",
    "version": "7.0.14",
    "core_count": 2,
    "term_length": 12,
}


_ORDER = {"account_number": "1000001", "notifications": ["noc@example.com", "network-ops@example.com"]}
"""The provider order data a virtual colocation carries (provider_account_number, notification_emails)."""


def _london() -> dict[str, Any]:
    """One virtual colocation in London: Arista HA pair, PA-VM cluster, SD-WAN away from the routers."""
    return {
        "name": "LD-NE",
        "provider": "equinix",
        **_ORDER,
        "devices": [
            {"name": "eg-ld01", **_ARISTA, "ha_peer": "eg-ld02"},
            {"name": "eg-ld02", **_ARISTA},
            {"name": "fw-ld01", **_PALO_ALTO, "cluster": {"name": "fw-ld", "node": 0}},
            {"name": "fw-ld02", **_PALO_ALTO, "cluster": {"name": "fw-ld", "node": 1}},
            {"name": "sdwan-ld01", **_SDWAN, "diverse_from": "eg-ld01"},
        ],
    }


def _device(vc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(device for device in vc["devices"] if device["name"] == name)


def test_london_presence_matches_the_golden_file_byte_for_byte() -> None:
    """Whitespace, alignment and the single trailing newline are the contract of the rendered file."""
    assert render_equinix_network_devices(_london()) == _GOLDEN.read_text()


def test_presence_without_a_cluster_declares_no_licence_variables() -> None:
    """No cluster, no licence token variable: the file declares no variable at all."""
    vc = {"name": "AM-NE", "provider": "equinix", **_ORDER, "devices": [{"name": "eg-am-ne01", **_ARISTA}]}

    hcl = render_equinix_network_devices(vc)

    assert "license_token" not in hcl
    assert "variable " not in hcl
    assert hcl.endswith("}\n") and not hcl.endswith("}\n\n")


def test_london_presence_renders_one_resource_per_unit_not_per_device() -> None:
    """Five devices become three resources: the Arista pair, the firewall cluster and the SD-WAN device."""
    hcl = render_equinix_network_devices(_london())

    assert hcl.count('resource "equinix_network_device"') == 3
    assert 'resource "equinix_network_device" "ld_ne_eg_ld01"' in hcl
    assert 'resource "equinix_network_device" "ld_ne_fw_ld"' in hcl
    assert 'resource "equinix_network_device" "ld_ne_sdwan_ld01"' in hcl
    assert 'resource "equinix_network_device" "ld_ne_eg_ld02"' not in hcl


def test_arista_ha_pair_is_a_secondary_device_block_in_the_london_metro() -> None:
    """The second router is the primary's secondary_device, in the same metro, not a resource of its own."""
    hcl = render_equinix_network_devices(_london())

    assert 'type_code      = "ARISTA-ROUTER"' in hcl
    assert "secondary_device {" in hcl
    assert 'name           = "eg-ld02"' in hcl
    assert hcl.count('metro_code     = "LD"') >= 3  # arista primary, secondary, and the other resources


def test_secondary_may_sit_in_another_metro_for_geo_redundancy() -> None:
    """Equinix allows the second node of a pair elsewhere; the secondary block carries its own metro."""
    vc = _london()
    _device(vc, "eg-ld02")["metro_code"] = "AM"

    hcl = render_equinix_network_devices(vc)

    secondary = hcl.split("secondary_device {")[1].split("}")[0]
    assert 'metro_code     = "AM"' in secondary


def test_firewall_cluster_is_one_resource_with_both_nodes_and_licence_variables() -> None:
    """A PA-VM cluster is one resource named after the cluster; node licences are sensitive variables."""
    hcl = render_equinix_network_devices(_london())

    assert 'name           = "fw-ld"' in hcl
    assert 'cluster_name = "fw-ld-cluster"' in hcl
    assert 'hostname = "fw-ld01"' in hcl
    assert 'hostname = "fw-ld02"' in hcl
    assert "license_token = var.ld_ne_fw_ld_node0_license_token" in hcl
    assert "license_token = var.ld_ne_fw_ld_node1_license_token" in hcl
    assert 'variable "ld_ne_fw_ld_node0_license_token" {\n  type      = string\n  sensitive = true\n}' in hcl


def test_sdwan_device_points_at_the_router_resource_through_diverse_device_id() -> None:
    """Anti-affinity from the router pair is expressed as a reference to the pair's resource uuid."""
    hcl = render_equinix_network_devices(_london())

    assert 'type_code      = "FG-SDWAN"' in hcl
    assert "diverse_device_id = equinix_network_device.ld_ne_eg_ld01.uuid" in hcl


def test_diverse_from_a_cluster_member_points_at_the_cluster_resource() -> None:
    """Any member of a unit resolves to that unit's resource, so a cluster node is a valid anchor."""
    vc = _london()
    _device(vc, "sdwan-ld01")["diverse_from"] = "fw-ld02"

    hcl = render_equinix_network_devices(vc)

    assert "diverse_device_id = equinix_network_device.ld_ne_fw_ld.uuid" in hcl


def test_account_and_notifications_come_from_the_presence_and_secrets_are_never_rendered() -> None:
    """The order data of the presence is printed (also in the secondary block); no licence, key or password is."""
    hcl = render_equinix_network_devices(_london())

    expected_list = '["noc@example.com", "network-ops@example.com"]'
    assert hcl.count('account_number = "1000001"') == 4  # arista primary + secondary, firewall cluster, sd-wan
    assert hcl.count(f"notifications  = {expected_list}") == 4
    assert "var.equinix_account_number" not in hcl
    assert "var.notification_emails" not in hcl
    lowered = hcl.lower()
    assert "password" not in lowered
    assert "ssh_key" not in lowered
    assert "license_file" not in lowered
    assert "license_token = var." in hcl  # tokens stay Terraform variables, never a value


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda vc: vc.pop("account_number"), "account_number"),
        (lambda vc: vc.update(account_number="  "), "account_number"),
        (lambda vc: vc.update(notifications=[]), "notification"),
        (lambda vc: vc.pop("notifications"), "notification"),
        (lambda vc: vc.update(notifications=["", " "]), "notification"),
    ],
    ids=["no-account", "blank-account", "empty-list", "no-list", "blank-entries"],
)
def test_presence_without_order_data_is_refused(mutation: Any, message: str) -> None:
    """The provider order cannot be placed without a billing account and a notification address."""
    vc = _london()
    mutation(vc)

    with pytest.raises(ValueError, match=message):
        render_equinix_network_devices(vc)


def test_order_data_with_hcl_interpolation_is_escaped_not_evaluated() -> None:
    """The account and the e-mails are data from Infrahub: ${...} in them must not run at apply time."""
    vc = _london()
    vc["account_number"] = '1000001${file("/etc/passwd")}'
    vc["notifications"] = ["x@example.com%{if true}y%{endif}"]

    hcl = render_equinix_network_devices(vc)

    assert "$${file(" in hcl
    assert "%%{if true}" in hcl
    assert "${file(" not in hcl.replace("$${file(", "")


def test_rendering_is_deterministic_and_independent_of_device_order() -> None:
    """Idempotency: same data twice, and any ordering of the device list, gives the same bytes."""
    vc = _london()
    shuffled = copy.deepcopy(vc)
    shuffled["devices"].reverse()

    assert render_equinix_network_devices(vc) == render_equinix_network_devices(vc)
    assert render_equinix_network_devices(vc) == render_equinix_network_devices(shuffled)


def test_non_equinix_provider_is_refused() -> None:
    """Only Equinix is rendered; another provider needs another renderer."""
    vc = _london()
    vc["provider"] = "megaport"

    with pytest.raises(ValueError, match="only Equinix"):
        render_equinix_network_devices(vc)


def test_empty_presence_is_refused() -> None:
    """A presence with no devices is an error, not an empty file that looks like success."""
    with pytest.raises(ValueError, match="no devices"):
        render_equinix_network_devices({"name": "LD-NE", "provider": "equinix", **_ORDER, "devices": []})


def test_device_missing_a_required_field_is_named_in_the_error() -> None:
    """A device without a package code lists exactly what is missing."""
    vc = _london()
    del _device(vc, "sdwan-ld01")["package_code"]

    with pytest.raises(ValueError, match="sdwan-ld01.*package_code"):
        render_equinix_network_devices(vc)


def test_ha_peer_outside_the_presence_is_refused() -> None:
    """A primary naming a secondary that is not in the presence cannot be rendered."""
    vc = _london()
    _device(vc, "eg-ld01")["ha_peer"] = "eg-ld99"

    with pytest.raises(ValueError, match="eg-ld99"):
        render_equinix_network_devices(vc)


def test_secondary_claimed_by_two_primaries_is_refused() -> None:
    """One router cannot be the secondary of two primaries."""
    vc = _london()
    vc["devices"].append({"name": "eg-ld03", **_ARISTA, "ha_peer": "eg-ld02"})

    with pytest.raises(ValueError, match="cannot be the secondary"):
        render_equinix_network_devices(vc)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda vc: _device(vc, "fw-ld02").update({"cluster": {"name": "fw-ld", "node": 0}}), "bad or duplicate node"),
        (lambda vc: vc["devices"].remove(_device(vc, "fw-ld02")), "exactly node 0 and node 1"),
        (lambda vc: _device(vc, "fw-ld02").update({"core_count": 8}), "differ in core_count"),
        (lambda vc: _device(vc, "fw-ld02").update({"metro_code": "AM"}), "differ in metro_code"),
        (lambda vc: _device(vc, "fw-ld01").update({"diverse_from": "eg-ld01"}), "cannot also be"),
    ],
    ids=["duplicate-node", "missing-node", "different-cores", "different-metro", "cluster-with-diverse"],
)
def test_malformed_cluster_is_refused(mutation: Any, message: str) -> None:
    """A cluster needs two nodes, one metro, one template, and no placement rules of its own."""
    vc = _london()
    mutation(vc)

    with pytest.raises(ValueError, match=message):
        render_equinix_network_devices(vc)


def test_cluster_of_an_unsupported_vendor_is_refused() -> None:
    """Only PA-VM clusters are rendered; another vendor is refused rather than guessed at."""
    vc = _london()
    for name in ("fw-ld01", "fw-ld02"):
        _device(vc, name)["type_code"] = "FG-VM"

    with pytest.raises(ValueError, match="only .*PA-VM"):
        render_equinix_network_devices(vc)


@pytest.mark.parametrize(
    "device, target",
    [("eg-ld01", "sdwan-ld01"), ("sdwan-ld01", "sdwan-ld01"), ("sdwan-ld01", "nowhere")],
    ids=["ha-primary", "itself", "unknown-target"],
)
def test_invalid_diverse_from_is_refused(device: str, target: str) -> None:
    """diverse_device_id is for a single device, never against itself, and the target must exist."""
    vc = _london()
    _device(vc, device)["diverse_from"] = target

    with pytest.raises(ValueError):
        render_equinix_network_devices(vc)


def test_hcl_interpolation_in_a_field_is_escaped_not_evaluated() -> None:
    """${...} and %{...} inside a quoted HCL string are evaluated by Terraform: no field may smuggle them in.

    (A device name carrying them is refused earlier, as an invalid resource label.)"""
    vc = _london()
    _device(vc, "sdwan-ld01")["version"] = '7.0${file("/etc/passwd")}%{if true}y%{endif}'

    hcl = render_equinix_network_devices(vc)

    assert "$${file(" in hcl
    assert "%%{if true}" in hcl
    assert "7.0${" not in hcl


def test_a_device_name_with_interpolation_is_refused_as_a_label() -> None:
    """The name is part of a resource label, which HCL does not take with ${ in it."""
    vc = _london()
    _device(vc, "sdwan-ld01")["name"] = "x${y}"

    with pytest.raises(ValueError, match="valid Terraform identifier"):
        render_equinix_network_devices(vc)


@pytest.mark.parametrize("name", ["9-NE", "ld/ne", "ld ne+1"])
def test_a_name_that_is_no_terraform_identifier_is_refused(name: str) -> None:
    """The virtual colocation name becomes part of every resource label: a leading digit or a symbol is invalid HCL."""
    vc = _london()
    vc["name"] = name

    with pytest.raises(ValueError, match="valid Terraform identifier"):
        render_equinix_network_devices(vc)
