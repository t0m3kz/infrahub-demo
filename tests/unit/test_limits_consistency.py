"""utils/limits.py mirrors what the schema and the bootstrap data say; this is the test that keeps them equal.

The schema (``parameters``) and ``data/bootstrap`` are the source of truth. Every constant of ``utils/limits.py``
that mirrors one of them is checked here, and a failure names the constant and the file to change. Constants that
hold a design relation (the VLAN, VNI and ASN bands that must not overlap) are checked against each other.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from generators.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.topology.segment import tenant_pool_name
from generators.topology.virtual_circuit import handoff_pool_name
from tests.integration.demo_data import attribute
from utils import limits
from utils.exchange_transit import (
    TRANSIT_SLOT,
    TRANSIT_VNI_MAX,
    TRANSIT_VNI_MIN,
    WAN_EXIT_VLAN_BASE,
    lb_leg_pool_name,
    lb_vip_pool_name,
    transit_pool_name,
)

BOOTSTRAP = "data/bootstrap"
# NX-OS reserves 3968-4094 for itself (system vlan reserve); the L3 VNI SVI band must end below it.
NXOS_RESERVED_VLAN_START = 3968
NXOS_RESERVED_VLAN_END = 4094


def _bootstrap_objects(root_dir: Path, kind: str) -> list[dict[str, Any]]:
    """Every object of ``kind`` declared in any document of data/bootstrap."""
    objects: list[dict[str, Any]] = []
    for path in sorted((root_dir / BOOTSTRAP).glob("*.y*ml")):
        for document in yaml.safe_load_all(path.read_text()):
            spec = (document or {}).get("spec") or {}
            if spec.get("kind") == kind:
                objects.extend(spec.get("data") or [])
    return objects


@pytest.fixture(scope="module")
def number_pools(root_dir: Path) -> dict[str, dict[str, Any]]:
    """The CoreNumberPool objects of data/bootstrap by name."""
    return {pool["name"]: pool for pool in _bootstrap_objects(root_dir, "CoreNumberPool")}


@pytest.fixture(scope="module")
def prefix_pools(root_dir: Path) -> dict[str, dict[str, Any]]:
    """The CoreIPPrefixPool objects of data/bootstrap by name."""
    return {pool["name"]: pool for pool in _bootstrap_objects(root_dir, "CoreIPPrefixPool")}


@pytest.fixture(scope="module")
def address_pools(root_dir: Path) -> dict[str, dict[str, Any]]:
    """The CoreIPAddressPool objects of data/bootstrap by name."""
    return {pool["name"]: pool for pool in _bootstrap_objects(root_dir, "CoreIPAddressPool")}


@pytest.fixture(scope="module")
def pool_names(
    number_pools: dict[str, dict[str, Any]],
    prefix_pools: dict[str, dict[str, Any]],
    address_pools: dict[str, dict[str, Any]],
) -> set[str]:
    """Every pool name data/bootstrap defines, of any pool kind."""
    return set(number_pools) | set(prefix_pools) | set(address_pools)


@pytest.fixture(scope="module")
def group_names(root_dir: Path) -> set[str]:
    """The CoreStandardGroup names of data/bootstrap/00_groups.yml."""
    return {group["name"] for group in _bootstrap_objects(root_dir, "CoreStandardGroup")}


@pytest.fixture(scope="module")
def namespaces(root_dir: Path) -> list[dict[str, Any]]:
    """The fixed VRF namespaces of data/bootstrap/22_namespaces.yml."""
    return _bootstrap_objects(root_dir, "IpamNamespace")


def _range(pool: dict[str, Any]) -> tuple[int, int]:
    return pool["start_range"], pool["end_range"]


class TestRangesMirrorTheData:
    """A range constant equals the range the schema or the bootstrap pool says."""

    def test_private_asn32_range_is_the_pool_range(self, number_pools: dict[str, dict[str, Any]]) -> None:
        """Change ``PRIVATE_ASN32_START``/``PRIVATE_ASN32_END`` in utils/limits.py, or the pool in 16_asn_pools.yml."""
        assert _range(number_pools[limits.PRIVATE_ASN32_POOL]) == (
            limits.PRIVATE_ASN32_START,
            limits.PRIVATE_ASN32_END,
        ), (
            "utils/limits.py PRIVATE_ASN32_START/PRIVATE_ASN32_END differ from data/bootstrap/16_asn_pools.yml "
            f"{limits.PRIVATE_ASN32_POOL}"
        )

    @pytest.mark.parametrize("pool_name", [limits.CUSTOMER_ROUTING_TAG_POOL, limits.SEGMENT_ROUTING_TAG_POOL])
    def test_routing_tag_range_is_the_pool_range(self, number_pools: dict[str, dict[str, Any]], pool_name: str) -> None:
        """Change ``ROUTING_TAG_MIN``/``ROUTING_TAG_MAX`` in utils/limits.py, or the pool in 19c_routing_tag_pools.yml."""
        assert _range(number_pools[pool_name]) == (limits.ROUTING_TAG_MIN, limits.ROUTING_TAG_MAX), (
            f"utils/limits.py ROUTING_TAG_MIN/ROUTING_TAG_MAX differ from data/bootstrap/19c_routing_tag_pools.yml {pool_name}"
        )

    @pytest.mark.parametrize("kind", ["OrganizationCustomer", "ManagedNetworkSegment"])
    def test_routing_tag_range_is_the_schema_parameters(self, kind: str) -> None:
        """Change ``ROUTING_TAG_MIN``/``ROUTING_TAG_MAX`` in utils/limits.py, or the ``parameters`` in the schema."""
        definition = attribute(kind, "routing_tag")

        assert definition is not None
        assert definition["parameters"] == {"min_value": limits.ROUTING_TAG_MIN, "max_value": limits.ROUTING_TAG_MAX}, (
            f"utils/limits.py ROUTING_TAG_MIN/ROUTING_TAG_MAX differ from the routing_tag parameters of {kind} "
            "(schemas/base/organization.yml, schemas/base/network_segment.yml)"
        )

    def test_infra_vlan_range_is_the_schema_parameters(self) -> None:
        """Change ``INFRA_VLAN_ID_MIN``/``INFRA_VLAN_ID_MAX`` in utils/limits.py, or the ``parameters`` in the schema."""
        definition = attribute("ManagedVlan", "vlan_id")

        assert definition is not None
        assert definition["parameters"] == {
            "min_value": limits.INFRA_VLAN_ID_MIN,
            "max_value": limits.INFRA_VLAN_ID_MAX,
        }, (
            "utils/limits.py INFRA_VLAN_ID_MIN/INFRA_VLAN_ID_MAX differ from the vlan_id parameters of ManagedVlan "
            "(schemas/extensions/service/vlan.yml)"
        )

    def test_the_transit_prefix_lengths_are_the_pool_defaults(self, prefix_pools: dict[str, dict[str, Any]]) -> None:
        """Change ``TRANSIT_PREFIXLEN``/``LB_VIP_PREFIXLEN`` in utils/limits.py, or default_prefix_length in 23_transit_pools.yml."""
        for namespace in ("PROD", "NON-PROD", "INTERNET", "MANAGEMENT"):
            pool = prefix_pools[transit_pool_name(namespace)]
            assert pool["default_prefix_length"] == limits.TRANSIT_PREFIXLEN, (
                f"utils/limits.py TRANSIT_PREFIXLEN differs from data/bootstrap/23_transit_pools.yml {pool['name']}"
            )
        for namespace in ("PROD", "NON-PROD"):
            assert prefix_pools[lb_leg_pool_name(namespace)]["default_prefix_length"] == limits.TRANSIT_PREFIXLEN, (
                f"utils/limits.py TRANSIT_PREFIXLEN differs from data/bootstrap/23_transit_pools.yml "
                f"{lb_leg_pool_name(namespace)}"
            )
            assert prefix_pools[lb_vip_pool_name(namespace)]["default_prefix_length"] == limits.LB_VIP_PREFIXLEN, (
                f"utils/limits.py LB_VIP_PREFIXLEN differs from data/bootstrap/23_transit_pools.yml "
                f"{lb_vip_pool_name(namespace)}"
            )

    def test_the_snat_prefix_length_is_the_pool_default(self, address_pools: dict[str, dict[str, Any]]) -> None:
        """Change ``SNAT_PREFIX_LENGTH`` in utils/limits.py, or default_prefix_length in 23_transit_pools.yml."""
        assert address_pools[limits.FW_SNAT_POOL]["default_prefix_length"] == limits.SNAT_PREFIX_LENGTH, (
            f"utils/limits.py SNAT_PREFIX_LENGTH differs from data/bootstrap/23_transit_pools.yml {limits.FW_SNAT_POOL}"
        )

    def test_the_context_vlan_bands_are_the_specs_vlan_ranges(self) -> None:
        """The firewall and load-balancer context specs allocate from the bands ``utils/limits.py`` names."""
        assert FIREWALL_CONTEXT.vlan_range == (limits.FW_CONTEXT_VLAN_START, limits.FW_CONTEXT_VLAN_END)
        assert LOADBALANCER_CONTEXT.vlan_range == (limits.LB_CONTEXT_VLAN_START, limits.LB_CONTEXT_VLAN_END)


class TestSetupDataNames:
    """Every pool and group name the code looks up exists in data/bootstrap."""

    @staticmethod
    def _constants(suffix: str) -> dict[str, str]:
        return {name: value for name, value in vars(limits).items() if name.endswith(suffix) and isinstance(value, str)}

    def test_the_pool_constants_are_found_by_introspection(self) -> None:
        """A renamed suffix would silently empty the checks below."""
        assert len(self._constants("_POOL")) >= 10
        assert len(self._constants("_POOL_TEMPLATE")) == 5

    @pytest.mark.parametrize(
        "constant", sorted(c for c, v in vars(limits).items() if c.endswith("_POOL") and isinstance(v, str))
    )
    def test_every_pool_name_exists_in_the_bootstrap_data(self, constant: str, pool_names: set[str]) -> None:
        """Rename the pool in data/bootstrap or change the constant in utils/limits.py."""
        name = getattr(limits, constant)
        assert name in pool_names, f"utils/limits.py {constant} = {name!r} is no pool of data/bootstrap"

    @pytest.mark.parametrize(
        ("template", "namespace_types"),
        [
            ("FW_TRANSIT_POOL_TEMPLATE", None),
            ("HANDOFF_POOL_TEMPLATE", None),
            ("LB_LEG_POOL_TEMPLATE", {"prod", "non_prod"}),
            ("LB_VIP_POOL_TEMPLATE", {"prod", "non_prod"}),
            ("TENANT_POOL_TEMPLATE", {"prod", "non_prod"}),
        ],
    )
    def test_every_namespace_has_the_pool_its_template_names(
        self,
        template: str,
        namespace_types: set[str] | None,
        namespaces: list[dict[str, Any]],
        pool_names: set[str],
    ) -> None:
        """One pool per fixed namespace (``None``: all four) or per tenant namespace (PROD, NON-PROD)."""
        pattern: str = getattr(limits, template)
        expected = {
            ns["name"] for ns in namespaces if namespace_types is None or ns["namespace_type"] in namespace_types
        }
        assert expected, "no namespace in data/bootstrap/22_namespaces.yml"

        for namespace in sorted(expected):
            assert pattern.format(namespace=namespace) in pool_names, (
                f"utils/limits.py {template} yields {pattern.format(namespace=namespace)!r} for the {namespace} "
                "namespace, which data/bootstrap does not define"
            )
        regex = re.compile("^" + re.escape(pattern).replace(re.escape("{namespace}"), "(?P<namespace>.+)") + "$")
        defined = {m["namespace"] for name in pool_names if (m := regex.match(name))}
        assert defined == expected, (
            f"data/bootstrap defines {template} pools for {sorted(defined)}, the namespaces say {sorted(expected)}"
        )

    @pytest.mark.parametrize(
        ("builder", "template"),
        [
            (transit_pool_name, "FW_TRANSIT_POOL_TEMPLATE"),
            (lb_leg_pool_name, "LB_LEG_POOL_TEMPLATE"),
            (lb_vip_pool_name, "LB_VIP_POOL_TEMPLATE"),
            (tenant_pool_name, "TENANT_POOL_TEMPLATE"),
            (handoff_pool_name, "HANDOFF_POOL_TEMPLATE"),
        ],
    )
    def test_the_builder_fills_its_template(self, builder: Callable[[str], str], template: str) -> None:
        """The name builders and the template constants are one definition."""
        assert builder("PROD") == getattr(limits, template).format(namespace="PROD")

    def test_every_group_constant_is_declared_in_the_bootstrap_groups(self, group_names: set[str]) -> None:
        """Declare the group in data/bootstrap/00_groups.yml or change the constant in utils/limits.py."""
        constants = {name: value for name, value in vars(limits).items() if name.startswith("GROUP_")}
        assert constants, "no GROUP_* constant in utils/limits.py"
        for constant, name in constants.items():
            assert name in group_names, (
                f"utils/limits.py {constant} = {name!r} is no group of data/bootstrap/00_groups.yml"
            )

    @pytest.mark.parametrize(
        "mapping",
        [
            "ROLE_GROUP_NAMES",
            "TOPOLOGY_ORCHESTRATOR_GROUPS",
            "APPLICATION_ORCHESTRATOR_GROUPS",
            "VIRTUAL_COLOCATION_ORCHESTRATOR_GROUPS",
        ],
    )
    def test_every_mapped_group_is_declared_in_the_bootstrap_groups(self, mapping: str, group_names: set[str]) -> None:
        """The values of the role and orchestrator maps are groups data/bootstrap/00_groups.yml declares."""
        groups = getattr(limits, mapping)
        assert groups, f"utils/limits.py {mapping} is empty"
        for key, name in groups.items():
            assert name in group_names, f"utils/limits.py {mapping}[{key!r}] = {name!r} is no group of 00_groups.yml"


class TestBandsDoNotOverlap:
    """The VLAN, VNI and ASN bands that share one number space."""

    def test_vlan_bands_are_ordered_and_disjoint(self) -> None:
        """customer < firewall contexts < load-balancer contexts; their transit legs and the WAN exit end below the SVI band."""
        slots = max(TRANSIT_SLOT.values())
        assert 1 < limits.INFRA_VLAN_ID_MIN <= limits.INFRA_VLAN_ID_MAX < limits.CUSTOMER_VLAN_ID_MIN
        assert limits.CUSTOMER_VLAN_ID_MIN < limits.CUSTOMER_VLAN_ID_MAX < limits.FW_CONTEXT_VLAN_START
        assert limits.FW_CONTEXT_VLAN_START <= limits.FW_CONTEXT_VLAN_END < limits.LB_CONTEXT_VLAN_START
        assert limits.LB_CONTEXT_VLAN_START <= limits.LB_CONTEXT_VLAN_END
        assert limits.LB_CONTEXT_VLAN_END - limits.FW_CONTEXT_VLAN_START < limits.TRANSIT_VLAN_BAND, (
            "a context VLAN + TRANSIT_VLAN_BAND x slot would land in the context band of another slot"
        )
        assert limits.LB_CONTEXT_VLAN_END + limits.TRANSIT_VLAN_BAND * slots < WAN_EXIT_VLAN_BASE
        assert WAN_EXIT_VLAN_BASE + slots < limits.L3VNI_SVI_VLAN_BASE

    def test_the_l3vni_svi_band_sits_above_the_customer_band_and_below_the_nxos_reserved_vlans(self) -> None:
        """Raise ``CUSTOMER_VLAN_ID_MAX`` and it would collide with the L3 VNI SVI band."""
        assert limits.CUSTOMER_VLAN_ID_MAX < limits.L3VNI_SVI_VLAN_BASE <= limits.L3VNI_SVI_VLAN_MAX
        assert limits.L3VNI_SVI_VLAN_MAX < NXOS_RESERVED_VLAN_START <= NXOS_RESERVED_VLAN_END

    def test_vni_bands_are_ordered_and_encodable(
        self, number_pools: dict[str, dict[str, Any]], namespaces: list[dict[str, Any]]
    ) -> None:
        """site pool < GLOBAL-L2VNI < fixed L3 VNIs < transit VNIs, all within a 16-bit RD/RT field."""
        global_start, global_end = _range(number_pools[limits.STRETCHED_VNI_POOL])
        fixed = [ns["l3_vni"] for ns in namespaces]

        assert limits.SITE_VNI_START <= limits.SITE_VNI_END < global_start <= global_end
        assert global_end < min(fixed)
        assert max(fixed) < TRANSIT_VNI_MIN <= TRANSIT_VNI_MAX <= limits.MAX_ENCODABLE_VNI
        assert limits.TRANSIT_VNI_BASE + limits.FW_CONTEXT_VLAN_START == TRANSIT_VNI_MIN

    def test_asn_and_tag_spaces_fit_their_fields(self) -> None:
        """A routing tag is a 16-bit community value (65535 is reserved); a private 32-bit ASN is above the 16-bit space."""
        assert 1 <= limits.ROUTING_TAG_MIN <= limits.ROUTING_TAG_MAX < 65535
        assert 65535 < limits.PRIVATE_ASN32_START < limits.PRIVATE_ASN32_END < 2**32 - 1


class TestAclSequenceBands:
    """The leaf ACL of a segment is numbered in bands that must not run into each other."""

    def test_own_rules_return_legs_internet_band_and_closing_rule_are_ordered(self) -> None:
        """Own rules (rule index) < return legs < internet band < closing rule; change utils/limits.py ACL_*_SEQ*."""
        from generators.segment_firewall import RULE_INDEX_START, RULE_INDEX_STEP

        assert 0 < RULE_INDEX_START
        assert RULE_INDEX_START + RULE_INDEX_STEP < limits.ACL_RETURN_LEG_SEQ_START
        assert limits.ACL_RETURN_LEG_SEQ_START < limits.ACL_INTERNET_BAND_SEQ_START < limits.ACL_CLOSING_SEQ

    def test_a_band_leaves_room_for_the_entries_it_numbers(self) -> None:
        """The return legs and the internet band are numbered in steps of 10 (transforms/helpers/acl.py)."""
        assert (limits.ACL_INTERNET_BAND_SEQ_START - limits.ACL_RETURN_LEG_SEQ_START) // 10 >= 100
        assert (limits.ACL_CLOSING_SEQ - limits.ACL_INTERNET_BAND_SEQ_START) // 10 >= 100


class TestTransitOffsets:
    """The host offsets of a transit leg fit the /29 the bootstrap pools allocate."""

    def test_the_largest_offset_is_a_host_of_the_transit_prefix(self, prefix_pools: dict[str, dict[str, Any]]) -> None:
        """Change ``OFFSET_*`` or ``TRANSIT_PREFIXLEN`` in utils/limits.py, or default_prefix_length in 23_transit_pools.yml."""
        offsets = (limits.OFFSET_ANYCAST, limits.OFFSET_VIP, limits.OFFSET_MEMBER_A, limits.OFFSET_MEMBER_B)
        for namespace in ("PROD", "NON-PROD", "INTERNET", "MANAGEMENT"):
            prefix_length = prefix_pools[transit_pool_name(namespace)]["default_prefix_length"]
            hosts = 2 ** (32 - prefix_length) - 2
            assert max(offsets) <= hosts, (
                f"utils/limits.py OFFSET_* reaches .{max(offsets)}, a /{prefix_length} "
                f"({transit_pool_name(namespace)} in data/bootstrap/23_transit_pools.yml) has {hosts} hosts"
            )

    def test_the_offsets_are_distinct_hosts(self) -> None:
        """Anycast, VIP and the two members are four different addresses; .0 is the network."""
        offsets = [limits.OFFSET_ANYCAST, limits.OFFSET_VIP, limits.OFFSET_MEMBER_A, limits.OFFSET_MEMBER_B]
        assert len(set(offsets)) == len(offsets)
        assert min(offsets) >= 1


class TestUnderlayMtu:
    """One MTU for every routed underlay port and the NX-OS L3 VNI SVI."""

    def test_the_nxos_l3vni_svi_golden_renders_it(self, root_dir: Path) -> None:
        """Every rendered L3 VNI SVI carries ``mtu {UNDERLAY_MTU}``; the template reads ``underlay_mtu``."""
        rendered = 0
        for golden in sorted((root_dir / "tests/smoke/configs").glob("*/output.txt")):
            lines = golden.read_text().splitlines()
            for index, line in enumerate(lines):
                if line.startswith("  description L3VNI transit SVI for VRF"):
                    window = lines[index : index + 4]
                    assert f"  mtu {limits.UNDERLAY_MTU}" in window, (
                        f"{golden.relative_to(root_dir)}: the L3 VNI SVI does not render mtu {limits.UNDERLAY_MTU} "
                        "(utils/limits.py UNDERLAY_MTU, templates/configs/common/cisco_nxos_l3vni_svi.j2)"
                    )
                    rendered += 1
        assert rendered, "no NX-OS golden renders an L3 VNI SVI"

    def test_the_template_has_no_literal_mtu(self, root_dir: Path) -> None:
        """The value comes from the transform context, never from the template text."""
        template = (root_dir / "templates/configs/common/cisco_nxos_l3vni_svi.j2").read_text()

        assert "mtu {{ underlay_mtu }}" in template
        assert str(limits.UNDERLAY_MTU) not in template

    def test_the_base_transform_context_carries_it(self) -> None:
        """BaseDeviceTransform._build_config sets ``underlay_mtu`` once for every template."""
        from tests.unit.base_device_transform_helpers import _device_data, _make_transform

        config = _make_transform()._build_config(_device_data(), "cisco_nxos")

        assert config["underlay_mtu"] == limits.UNDERLAY_MTU

    def test_the_dci_planner_uses_it(self) -> None:
        """The DCI ports and sessions of generators/topology/virtual_circuit.py read the same constant."""
        from generators.topology import virtual_circuit

        assert virtual_circuit.UNDERLAY_MTU == limits.UNDERLAY_MTU


class TestNoShadowDefinitions:
    """A value defined in utils/limits.py is not defined again next to the code that uses it."""

    @staticmethod
    def _module_constants(path: Path) -> set[str]:
        names: set[str] = set()
        for node in ast.parse(path.read_text()).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        return names

    def test_no_module_redefines_a_limit(self, root_dir: Path) -> None:
        """Import it from utils.limits instead of assigning it again (a copy drifts)."""
        limit_names = {
            name.lstrip("_") for name in self._module_constants(root_dir / "utils/limits.py") if name.isupper()
        }
        assert len(limit_names) > 50
        redefined: list[str] = []
        for directory in ("generators", "transforms", "checks", "utils"):
            for path in sorted((root_dir / directory).rglob("*.py")):
                if path == root_dir / "utils/limits.py":
                    continue
                redefined += [
                    f"{path.relative_to(root_dir)}: {name}"
                    for name in sorted(self._module_constants(path))
                    if name.lstrip("_") in limit_names
                ]

        assert not redefined, "defined in utils/limits.py and again in: " + "; ".join(redefined)
