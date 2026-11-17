"""Consolidated tests for data cleaning utilities."""

import pytest

from utils.data_cleaning import clean_data, get_data


class TestValueExtraction:
    """Test extraction of various GraphQL wrapper patterns."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            pytest.param({"name": {"value": "DC-1"}}, {"name": "DC-1"}, id="value"),
            pytest.param({"spine_count": {"count": 4}}, {"spine_count": 4}, id="count"),
            pytest.param(
                {"device": {"id": "123e4567-e89b-12d3-a456-426614174000"}},
                {"device": "123e4567-e89b-12d3-a456-426614174000"},
                id="id",
            ),
            pytest.param(
                {"device": {"node": {"name": {"value": "leaf-01"}}}}, {"device": {"name": "leaf-01"}}, id="node"
            ),
            pytest.param(
                {
                    "interfaces": {
                        "edges": [{"node": {"name": {"value": "eth0"}}}, {"node": {"name": {"value": "eth1"}}}]
                    }
                },
                {"interfaces": [{"name": "eth0"}, {"name": "eth1"}]},
                id="edges",
            ),
        ],
    )
    def test_wrapper_is_unwrapped(self, data: dict, expected: dict) -> None:
        """value, count, id, node and edges wrappers are each unwrapped to their content."""
        assert clean_data(data) == expected


class TestEdgeCaseValues:
    """Test handling of special values (None, 0, empty string)."""

    @pytest.mark.parametrize("value", [0, None, ""], ids=["zero", "none", "empty-string"])
    def test_falsy_value_is_preserved(self, value: object) -> None:
        """A falsy ``value`` is kept as it is, never treated as missing."""
        assert clean_data({"field": {"value": value}}) == {"field": value}


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

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            pytest.param({"field": {"value": 5, "count": 10}}, {"field": 5}, id="value-over-count"),
            pytest.param(
                {"field": {"value": "name-value", "id": "some-id"}}, {"field": "name-value"}, id="value-over-id"
            ),
            pytest.param(
                {"interfaces": {"count": 48, "edges": [{"node": {"name": {"value": "eth0"}}}]}},
                {"interfaces": [{"name": "eth0"}]},
                id="edges-over-count",
            ),
        ],
    )
    def test_priority(self, data: dict, expected: dict) -> None:
        """value wins over count and id, edges over count, when both are present."""
        assert clean_data(data) == expected


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

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            pytest.param(
                {"DcimDevice": {"edges": [{"node": {"name": {"value": "leaf-01"}}}]}}, {"name": "leaf-01"}, id="edges"
            ),
            pytest.param({"TopologyDataCenter": [{"name": {"value": "DC-1"}}]}, {"name": "DC-1"}, id="list"),
            pytest.param({"Device": {"name": {"value": "spine-01"}}}, {"name": "spine-01"}, id="single-value"),
        ],
    )
    def test_extract_first_item(self, data: dict, expected: dict) -> None:
        """The first value of the cleaned response, or the first item when it is a list."""
        assert get_data(data) == expected

    @pytest.mark.parametrize("data", [{}, [1, 2, 3]], ids=["empty-dict", "non-dict"])
    def test_raise_on_no_non_empty_dict(self, data: object) -> None:
        """An empty dictionary or a non-dictionary input raises ValueError."""
        with pytest.raises(ValueError, match="did not return a non-empty dictionary"):
            get_data(data)

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
