"""Consolidated tests for data cleaning utilities.

This module tests the clean_data and get_data functions with real-world GraphQL responses
and edge cases. Trivial Python behavior tests have been removed.
"""

import pytest

from utils.data_cleaning import clean_data, get_data


class TestValueExtraction:
    """Test extraction of various GraphQL wrapper patterns."""

    def test_extract_value_field(self) -> None:
        """Test extraction of value field."""
        data = {"name": {"value": "DC-1"}}
        result = clean_data(data)
        assert result == {"name": "DC-1"}

    def test_extract_count_field(self) -> None:
        """Test extraction of count field from aggregations."""
        data = {"spine_count": {"count": 4}}
        result = clean_data(data)
        assert result == {"spine_count": 4}

    def test_extract_id_field(self) -> None:
        """Test extraction of id field."""
        data = {"device": {"id": "123e4567-e89b-12d3-a456-426614174000"}}
        result = clean_data(data)
        assert result == {"device": "123e4567-e89b-12d3-a456-426614174000"}

    def test_extract_node_field(self) -> None:
        """Test unwrapping node field."""
        data = {"device": {"node": {"name": {"value": "leaf-01"}}}}
        result = clean_data(data)
        assert result == {"device": {"name": "leaf-01"}}

    def test_extract_edges_field(self) -> None:
        """Test unwrapping edges field."""
        data = {
            "interfaces": {
                "edges": [
                    {"node": {"name": {"value": "eth0"}}},
                    {"node": {"name": {"value": "eth1"}}},
                ]
            }
        }
        result = clean_data(data)
        assert result == {"interfaces": [{"name": "eth0"}, {"name": "eth1"}]}


class TestEdgeCaseValues:
    """Test handling of special values (None, 0, empty string)."""

    def test_handle_zero_value(self) -> None:
        """Test that value of 0 is not treated as falsy."""
        data = {"count": {"value": 0}}
        result = clean_data(data)
        assert result == {"count": 0}

    def test_handle_none_value(self) -> None:
        """Test that None values are preserved."""
        data = {"optional_field": {"value": None}}
        result = clean_data(data)
        assert result == {"optional_field": None}

    def test_value_wrapper_with_empty_string(self) -> None:
        """Test clean_data with value wrapper containing empty string."""
        data = {"field": {"value": ""}}
        result = clean_data(data)
        assert result == {"field": ""}


class TestComplexStructures:
    """Test complex nested GraphQL structures."""

    def test_nested_structure(self) -> None:
        """Test complex nested structure."""
        data = {
            "datacenter": {
                "node": {
                    "name": {"value": "DC-1"},
                    "pods": {
                        "edges": [
                            {
                                "node": {
                                    "name": {"value": "pod-1"},
                                    "spine_count": {"count": 2},
                                }
                            }
                        ]
                    },
                }
            }
        }
        result = clean_data(data)
        expected = {
            "datacenter": {
                "name": "DC-1",
                "pods": [{"name": "pod-1", "spine_count": 2}],
            }
        }
        assert result == expected

    def test_nested_value_wrappers(self) -> None:
        """Test clean_data recursively extracts value wrappers from nested dicts."""
        data = {
            "pod": {
                "name": {"value": "Pod-A1"},
                "index": {"value": 1},
                "role": {"value": "cpu"},
            }
        }
        result = clean_data(data)
        assert result == {"pod": {"name": "Pod-A1", "index": 1, "role": "cpu"}}

    def test_nested_list_of_dicts(self) -> None:
        """Test clean_data with nested lists of dictionaries."""
        data = {
            "devices": [
                {
                    "name": {"value": "Device-1"},
                    "interfaces": {
                        "edges": [
                            {"node": {"name": {"value": "Eth0"}}},
                            {"node": {"name": {"value": "Eth1"}}},
                        ]
                    },
                },
                {
                    "name": {"value": "Device-2"},
                    "interfaces": {
                        "edges": [
                            {"node": {"name": {"value": "Eth2"}}},
                        ]
                    },
                },
            ]
        }
        result = clean_data(data)

        assert len(result["devices"]) == 2
        assert result["devices"][0]["name"] == "Device-1"
        assert result["devices"][0]["interfaces"][0]["name"] == "Eth0"
        assert result["devices"][1]["name"] == "Device-2"
        assert result["devices"][1]["interfaces"][0]["name"] == "Eth2"


class TestWrapperPriority:
    """Test that wrappers are processed in correct priority order."""

    def test_priority_value_over_count(self) -> None:
        """Test that value takes priority over count when both present."""
        data = {"field": {"value": 5, "count": 10}}
        result = clean_data(data)
        assert result == {"field": 5}

    def test_priority_value_over_id(self) -> None:
        """Test that value takes priority over id when both present."""
        data = {"field": {"value": "name-value", "id": "some-id"}}
        result = clean_data(data)
        assert result == {"field": "name-value"}

    def test_priority_edges_over_count(self) -> None:
        """Test that edges take priority over count when both present."""
        data = {
            "interfaces": {
                "count": 48,
                "edges": [{"node": {"name": {"value": "eth0"}}}],
            }
        }
        result = clean_data(data)
        assert result == {"interfaces": [{"name": "eth0"}]}


class TestGraphQLTypenames:
    """Test handling of GraphQL __typename fields."""

    def test_typename_normalized(self) -> None:
        """Test clean_data normalizes __typename to typename."""
        data = {
            "device": {
                "node": {
                    "__typename": "DcimPhysicalDevice",
                    "id": "1",
                    "name": {"value": "Device-1"},
                }
            }
        }
        result = clean_data(data)
        expected = {"device": {"typename": "DcimPhysicalDevice", "id": "1", "name": "Device-1"}}
        assert result == expected


class TestRealGraphQLResponses:
    """Test clean_data with actual Infrahub GraphQL responses."""

    def test_datacenter_capacity_validation_response(self) -> None:
        """Test clean_data with real DataCenterCapacityValidation query response."""
        data = {
            "data": {
                "TopologyDataCenter": {
                    "edges": [
                        {
                            "node": {
                                "id": "187a1cd7-11d2-663c-3105-c5103fc206fa",
                                "children": {
                                    "count": 3,
                                    "edges": [
                                        {
                                            "node": {
                                                "children": {"count": 0},
                                                "name": None,
                                                "spine_count": {"count": 3},
                                                "leaf_count": {"count": 32},
                                                "tor_count": {"count": 0},
                                            }
                                        },
                                        {
                                            "node": {
                                                "children": {"count": 0},
                                                "name": None,
                                                "spine_count": {"count": 3},
                                                "leaf_count": {"count": 32},
                                                "tor_count": {"count": 0},
                                            }
                                        },
                                        {
                                            "node": {
                                                "children": {"count": 0},
                                                "name": None,
                                                "spine_count": {"count": 4},
                                                "leaf_count": {"count": 16},
                                                "tor_count": {"count": 0},
                                            }
                                        },
                                    ],
                                },
                                "name": {"value": "DC1"},
                                "amount_of_super_spines": {"value": 2},
                            }
                        }
                    ]
                }
            }
        }

        result = clean_data(data)

        # Verify top-level structure
        assert "data" in result
        assert "TopologyDataCenter" in result["data"]
        assert isinstance(result["data"]["TopologyDataCenter"], list)
        assert len(result["data"]["TopologyDataCenter"]) == 1

        # Extract datacenter node
        dc = result["data"]["TopologyDataCenter"][0]

        # Verify basic fields are extracted
        assert dc["id"] == "187a1cd7-11d2-663c-3105-c5103fc206fa"
        assert dc["name"] == "DC1"
        assert dc["amount_of_super_spines"] == 2

        # Verify children structure with mixed count and edges
        assert "children" in dc
        children = dc["children"]

        # When both count and edges are present, edges should take priority
        assert isinstance(children, list)
        assert len(children) == 3

        # Verify first pod
        pod1 = children[0]
        assert pod1["children"] == 0  # count extracted
        assert pod1["name"] is None  # None value preserved
        assert pod1["spine_count"] == 3  # count extracted
        assert pod1["leaf_count"] == 32  # count extracted
        assert pod1["tor_count"] == 0  # count extracted (0 is not treated as falsy)

    def test_complex_topology_pod_response(self) -> None:
        """Test clean_data with complex TopologyPod response structure."""
        data = {
            "TopologyPod": {
                "edges": [
                    {
                        "node": {
                            "id": "pod-uuid-1",
                            "amount_of_spines": {"value": 4},
                            "name": {"value": "Pod-A1"},
                            "checksum": {"value": "abc123"},
                            "index": {"value": 1},
                            "role": {"value": "cpu"},
                            "spine_switch_template": {
                                "node": {
                                    "id": "template-uuid-1",
                                    "interfaces": {
                                        "edges": [
                                            {"node": {"name": {"value": "Ethernet1/31"}}},
                                            {"node": {"name": {"value": "Ethernet1/32"}}},
                                        ]
                                    },
                                }
                            },
                            "parent": {"node": {"id": "dc-uuid-1", "name": {"value": "DC-1"}}},
                        }
                    }
                ]
            }
        }
        result = clean_data(data)

        # Verify structure is properly unwrapped
        assert isinstance(result, dict)
        assert "TopologyPod" in result
        assert isinstance(result["TopologyPod"], list)
        assert len(result["TopologyPod"]) == 1

        pod = result["TopologyPod"][0]
        assert pod["id"] == "pod-uuid-1"
        assert pod["name"] == "Pod-A1"
        assert pod["amount_of_spines"] == 4
        assert pod["checksum"] == "abc123"

        # Verify nested spine_switch_template
        template = pod["spine_switch_template"]
        assert template["id"] == "template-uuid-1"
        assert isinstance(template["interfaces"], list)
        assert len(template["interfaces"]) == 2
        assert template["interfaces"][0]["name"] == "Ethernet1/31"
        assert template["interfaces"][1]["name"] == "Ethernet1/32"

        # Verify parent
        parent = pod["parent"]
        assert parent["id"] == "dc-uuid-1"
        assert parent["name"] == "DC-1"

    def test_deeply_nested_graphql_structure(self) -> None:
        """Test deeply nested GraphQL response structure."""
        data = {
            "TopologyDataCenter": {
                "edges": [
                    {
                        "node": {
                            "name": {"value": "DC1"},
                            "children": {
                                "edges": [
                                    {
                                        "node": {
                                            "name": {"value": "pod-1"},
                                            "children": {
                                                "edges": [
                                                    {
                                                        "node": {
                                                            "name": {"value": "rack-1"},
                                                            "devices": {"count": 10},
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }

        result = clean_data(data)

        # Navigate through nested structure
        dc_list = result["TopologyDataCenter"]
        assert len(dc_list) == 1
        assert dc_list[0]["name"] == "DC1"

        pods = dc_list[0]["children"]
        assert len(pods) == 1
        assert pods[0]["name"] == "pod-1"

        racks = pods[0]["children"]
        assert len(racks) == 1
        assert racks[0]["name"] == "rack-1"
        assert racks[0]["devices"] == 10


class TestGetData:
    """Test cases for get_data function."""

    def test_extract_first_value_from_dict(self) -> None:
        """Test extraction of first value from cleaned dictionary."""
        data = {"DcimDevice": {"edges": [{"node": {"name": {"value": "leaf-01"}}}]}}
        result = get_data(data)
        assert result == {"name": "leaf-01"}

    def test_extract_first_item_from_list(self) -> None:
        """Test extraction when first value is a list."""
        data = {"TopologyDataCenter": [{"name": {"value": "DC-1"}}]}
        result = get_data(data)
        assert result == {"name": "DC-1"}

    def test_extract_single_value(self) -> None:
        """Test extraction of single non-list value."""
        data = {"Device": {"name": {"value": "spine-01"}}}
        result = get_data(data)
        assert result == {"name": "spine-01"}

    def test_raise_on_empty_dict(self) -> None:
        """Test that ValueError is raised on empty dictionary."""
        with pytest.raises(ValueError, match="did not return a non-empty dictionary"):
            get_data({})

    def test_raise_on_non_dict(self) -> None:
        """Test that ValueError is raised on non-dictionary input."""
        with pytest.raises(ValueError, match="did not return a non-empty dictionary"):
            get_data([1, 2, 3])

    def test_complex_query_response(self) -> None:
        """Test realistic GraphQL query response."""
        data = {
            "DcimDevice": {
                "edges": [
                    {
                        "node": {
                            "id": "device-123",
                            "name": {"value": "leaf-01"},
                            "platform": {"node": {"name": {"value": "arista_eos"}}},
                            "interfaces": {
                                "count": 48,
                                "edges": [
                                    {
                                        "node": {
                                            "name": {"value": "Ethernet1"},
                                            "enabled": {"value": True},
                                        }
                                    }
                                ],
                            },
                        }
                    }
                ]
            }
        }
        result = get_data(data)
        assert result["name"] == "leaf-01"
        assert result["platform"]["name"] == "arista_eos"
        assert result["interfaces"][0]["name"] == "Ethernet1"

    def test_datacenter_capacity_with_get_data(self) -> None:
        """Test get_data with DataCenterCapacityValidation response."""
        data = {
            "TopologyDataCenter": {
                "edges": [
                    {
                        "node": {
                            "id": "187a1cd7-11d2-663c-3105-c5103fc206fa",
                            "name": {"value": "DC1"},
                            "amount_of_super_spines": {"value": 2},
                            "children": {
                                "count": 3,
                                "edges": [
                                    {
                                        "node": {
                                            "spine_count": {"count": 3},
                                            "leaf_count": {"count": 32},
                                        }
                                    }
                                ],
                            },
                        }
                    }
                ]
            }
        }

        # get_data should extract the first datacenter
        result = get_data(data)

        assert result["id"] == "187a1cd7-11d2-663c-3105-c5103fc206fa"
        assert result["name"] == "DC1"
        assert result["amount_of_super_spines"] == 2

        # Verify children (edges takes priority over count)
        assert isinstance(result["children"], list)
        assert len(result["children"]) == 1
        assert result["children"][0]["spine_count"] == 3
        assert result["children"][0]["leaf_count"] == 32
