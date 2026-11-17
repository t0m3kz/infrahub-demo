"""A RackGenerator wired the way ``generate()`` leaves it before it plans: one network rack of a mixed pod."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

from generators.dc.rack import RackGenerator
from generators.dc.rack_data import TopologyRackData
from generators.dc.rack_plan import RackReads
from generators.dcim.template_interfaces import TemplatePort
from tests.unit.simulators.fabric_designs import pod_design


def build_rack_generator() -> Any:
    """A rack generator with two leafs and one ToR, a spine pair and the pod's pools, no client."""
    parent = {
        "id": "dc-1",
        "name": "DC1",
        "index": 1,
        "naming_convention": "standard",
        "management_pool": {"id": "mgmt-pool", "name": "mgmt"},
    }
    pod = {
        "id": "pod-1",
        "name": "pod-1",
        "index": 1,
        "parent": parent,
        "leaf_interface_sorting_method": "top_down",
        "spine_interface_sorting_method": "bottom_up",
        "loopback_pool": {"id": "lo-pool", "name": "lo"},
        "prefix_pool": {"id": "p2p-pool", "name": "p2p"},
        "design": pod_design("S_MIXED"),
        "fabric_templates": [
            {
                "role": "spine",
                "quantity": 2,
                "template": {"id": "tmpl-spine", "interfaces": [{"name": "Eth1/10"}, {"name": "Eth1/11"}]},
            }
        ],
    }
    suite = {"index": 1}
    leaf_t = {
        "id": "tmpl-leaf",
        "interfaces": [{"name": "Eth1/1", "role": "uplink"}, {"name": "Eth1/2", "role": "uplink"}],
    }
    tor_t = {"id": "tmpl-tor", "interfaces": [{"name": "Eth1/47", "role": "uplink"}]}

    rack = {
        "id": "rack-1",
        "name": "RACK-1",
        "index": 2,
        "rack_type": "network",
        "row_index": 1,
        "parent": suite,
        "deployment": pod,
        "leafs": [{"role": "leaf", "quantity": 2, "template": leaf_t}],
        "tors": [{"role": "tor", "quantity": 1, "template": tor_t}],
        "l2_leafs": [],
        "access_leafs": [],
    }

    gen = RackGenerator.__new__(RackGenerator)

    gen._controllers = []
    gen.data = cast(TopologyRackData, rack)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []

    gen.fabric_name = "dc1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 1, 1, 1, 2]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._loopback_prefix_length = 32
    gen._spine_device_names = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
    gen._spine_interfaces = ["Eth1/10", "Eth1/11"]
    gen._technical_pool_id = "p2p-pool"
    gen._p2p_prefix_length = 31
    gen._routing_options = {"design": {"routing_strategy": "ebgp-ebgp"}}
    gen._created_device_names = set()
    gen._role_of = {}
    gen._leaf_ports = {}
    gen._layers = []
    gen._mlag_groups = []
    gen._problems = []

    return gen


_SFP = "10gbase-x-sfpp"


def rack_reads(**overrides: Any) -> RackReads:
    """What the plan reads before it runs, for the generator ``build_rack_generator`` returns: nothing exists yet, the
    leaf and spine templates have their ports, none of the devices has a rack position.
    """
    reads = RackReads(
        template_ports={
            "tmpl-leaf": [TemplatePort("Eth1/1", "uplink", _SFP), TemplatePort("Eth1/2", "uplink", _SFP)],
            "tmpl-tor": [TemplatePort("Eth1/47", "uplink", _SFP)],
            "tmpl-spine": [TemplatePort("Eth1/10", "downlink", _SFP), TemplatePort("Eth1/11", "downlink", _SFP)],
        }
    )
    return RackReads(**{**reads.__dict__, **overrides})
