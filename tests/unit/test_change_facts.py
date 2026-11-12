"""utils/change_facts.py: the devices, links, firewall rules and segments of a change, from the raw DiffTree nodes."""

from __future__ import annotations

from tests.unit.change_text_helpers import RAW_OBJECTS, artifact, objects_of, raw_node, raw_rule, realistic_inputs
from utils.change_facts import NEW, NO_SITE, OBJECT_DIFF_KINDS, build_facts
from utils.change_rules import affected_customers, prod_objects


def _facts(raw: list | None = None, **overrides: object):  # noqa: ANN202 - the dataclass under test
    inputs = realistic_inputs(**overrides)
    objects = inputs.objects if raw is None else objects_of(raw)
    return build_facts(inputs.rows, inputs.artifacts, objects)


class TestDevices:
    """Artifact targets, by site then role."""

    def test_a_device_has_its_site_role_pair_and_config_action(self) -> None:
        """New when the artifact is added, updated otherwise; the MLAG peer is named."""
        by_name = {d.name: d for d in _facts().devices}
        leaf = by_name["dc10-leaf-01"]
        assert (leaf.site, leaf.role, leaf.pair) == ("DC10", "leaf", "dc10-leaf-02")
        assert by_name["dc10-leaf-02"].action == NEW
        assert by_name["dc10-spine-01"].action == "updated"
        assert by_name["dc10-spine-01"].artifacts[0].change_text == "p3 -> n3"

    def test_a_removed_artifact_is_a_removed_config_of_a_device_the_branch_no_longer_has(self) -> None:
        """Every artifact of the target removed: the config is removed; no row: no site, no role."""
        facts = _facts(artifacts=[artifact("old-sw", action="removed")])
        (device,) = facts.devices
        assert (device.name, device.action, device.site, device.role, device.pair) == (
            "old-sw",
            "removed",
            NO_SITE,
            None,
            None,
        )

    def test_devices_are_ordered_by_site_role_and_name(self) -> None:
        """The order the description groups them in."""
        assert [d.name for d in _facts().devices] == ["dc10-leaf-01", "dc10-leaf-02", "dc10-spine-01"]


class TestLinks:
    """Cables and circuits, with their ends."""

    def test_a_cable_names_its_ends_type_id_and_place(self) -> None:
        """The ends are the diff's peer labels (``device-interface``)."""
        cable = next(link for link in _facts().links if link.family == "cable")
        assert cable.ends == ("dc10-leaf-01-Ethernet1/49", "dc10-spine-01-Ethernet1/1")
        assert (cable.status, cable.name, cable.link_type, cable.cable_id, cable.places) == (
            "ADDED",
            "CBL-1",
            "smf",
            "C-100",
            ("DC10",),
        )

    def test_a_carrier_circuit_has_provider_ends_and_places(self) -> None:
        """The circuit id is its name."""
        circuit = next(link for link in _facts().links if link.family == "carrier circuit")
        assert (circuit.name, circuit.link_type, circuit.provider, circuit.places) == (
            "DF10001",
            "dark_fiber",
            "Equinix (P016)",
            ("DC10", "DC20"),
        )
        assert circuit.ends[0] == "dc10-spine-01-Ethernet1/30"

    def test_a_session_circuit_has_its_type_and_vrf(self) -> None:
        """name, circuit_type and the VRF of the session."""
        session = raw_node(
            "TopologySessionCircuit",
            "ktw1-dci-a-waw1",
            "ADDED",
            "sess-1",
            {"name": "ktw1-dci-a-waw1", "circuit_type": "dci"},
            {"vrf": [("dc10-vrf-prod", "vrf-1")], "capabilities": [("a-Eth1", "i1"), ("b-Eth1", "i2")]},
        )
        (link,) = _facts([session]).links
        assert (link.family, link.name, link.link_type, link.vrf, link.ends) == (
            "session circuit",
            "ktw1-dci-a-waw1",
            "dci",
            "dc10-vrf-prod",
            ("a-Eth1", "b-Eth1"),
        )

    def test_a_removed_link_is_described_from_the_previous_values(self) -> None:
        """Gone from the branch, the diff still holds what it was."""
        gone = raw_node(
            "DcimCable",
            "CBL-9",
            "REMOVED",
            "c9",
            {"name": "CBL-9", "type": "copper"},
            {"endpoints": [("a-1", "i1"), ("b-1", "i2")]},
        )
        (link,) = _facts([gone]).links
        assert (link.status, link.link_type, link.ends) == ("REMOVED", "copper", ("a-1", "b-1"))

    def test_an_updated_link_lists_what_changed(self) -> None:
        """A changed attribute, old -> new, and a dropped peer."""
        node = raw_node(
            "DcimCable", "CBL-1", "UPDATED", "c1", {"name": "CBL-1", "type": "mmf"}, previous={"type": "smf"}
        )
        node["relationships"] = [
            {"name": "endpoints", "elements": [{"peer_label": "x-1", "peer_id": "i", "status": "REMOVED"}]}
        ]
        (link,) = _facts([node]).links
        assert link.changed == ("type smf -> mmf", "no longer endpoints x-1")


class TestRules:
    """Firewall rules: source, destination, service."""

    def test_source_defaults_to_the_policy_segment_and_zones_are_named(self) -> None:
        """No explicit source: the policy's segment; INTERNET is a zone; a port range is ``start-end``."""
        rules = {r.name: r for r in _facts().rules}
        assert (rules["web-to-app"].source, rules["web-to-app"].destination, rules["web-to-app"].service) == (
            "the policy's segment",
            "segment c005-app-p",
            "tcp/443",
        )
        assert (rules["web-to-internet"].destination, rules["web-to-internet"].service) == ("zone INTERNET", "any")
        assert (rules["mgmt-to-web"].source, rules["mgmt-to-web"].service) == ("segment c005-mgmt-p", "tcp/8000-8010")
        assert rules["mgmt-to-web"].changed == ("action deny -> permit", "port_start 443 -> 8000")

    def test_prefixes_addresses_and_a_disabled_flag(self) -> None:
        """Every selector is listed (the namespace of an IPAM label is dropped); ``disabled`` reads as not enabled."""
        raw = raw_rule(
            "r",
            "to-net",
            5,
            disabled="True",
            destination_segment_peers=[],
            destination_prefixes_peers=[("10.0.0.0/8 - default", "p1")],
            destination_ip_addresses_peers=[("192.0.2.1/32 - default", "a1")],
            source_security_tag_peers=[("web-tier", "t1")],
        )
        (rule,) = _facts([raw]).rules
        assert rule.destination == "prefix 10.0.0.0/8, address 192.0.2.1/32"
        assert rule.source == "tag web-tier"
        assert rule.enabled is False

    def test_rules_are_ordered_by_status_then_policy_and_index(self) -> None:
        """Added, updated, removed; a removed rule keeps its values."""
        assert [(r.status, r.name) for r in _facts().rules] == [
            ("ADDED", "web-to-app"),
            ("ADDED", "web-to-internet"),
            ("UPDATED", "mgmt-to-web"),
            ("REMOVED", "old-rule"),
        ]
        assert _facts().rules[-1].service == "tcp/443"


class TestSegments:
    """Network segments: addressing, scope and footprints."""

    def test_a_segment_has_prefix_gateway_scope_and_footprints(self) -> None:
        """The prefix is the network of the gateway."""
        segments = {s.name: s for s in _facts().segments}
        web = segments["c005-web-p"]
        assert (web.owner, web.environment, web.ip_version) == ("C005", "p", "ipv4")
        assert (web.prefix, web.gateway) == ("10.5.1.0/24", "10.5.1.1")
        assert (web.vlan_scope, web.stretch_scope, web.footprints) == ("local", "local", ("C005-P-DC-DC10",))
        assert segments["c005-app-p"].firewall_gateway is True

    def test_a_removed_cloud_segment_is_described_from_the_previous_values(self) -> None:
        """The previous value of every attribute, the peers it had."""
        gone = raw_node(
            "CloudNetworkSegment",
            "gone-seg",
            "REMOVED",
            "g",
            {"name": "gone-seg", "environment": "n"},
            {"owner": [("C007", "c7")]},
        )
        (segment,) = _facts([gone]).segments
        assert (segment.status, segment.environment, segment.owner) == ("REMOVED", "n", "C007")


class TestOwners:
    """Customers and PROD come from the same objects."""

    def test_the_customers_and_prod_follow_the_owners_and_footprints_of_the_diff(self) -> None:
        """The segments' and the footprint's ``owner`` peer, the footprint's environment."""
        objects = objects_of()
        assert affected_customers({}, objects) == {"cust-c005": "C005"}
        assert prod_objects(objects)

    def test_the_read_kinds_cover_the_families_and_the_owner_kinds(self) -> None:
        """What the diff query asks for."""
        assert {
            "SecurityPolicyRule",
            "DcimCable",
            "TopologyCarrierCircuit",
            "TopologySessionCircuit",
        } <= OBJECT_DIFF_KINDS
        assert {
            "ManagedVxlanSegment",
            "CloudNetworkSegment",
            "TopologyCustomerDC",
            "AppApplication",
        } <= OBJECT_DIFF_KINDS
        assert {o["kind"] for o in RAW_OBJECTS} <= OBJECT_DIFF_KINDS
