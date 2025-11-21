"""Tests for data cleaning utilities using real Infrahub GraphQL responses."""

from utils.data_cleaning import clean_data, get_data


class TestRealGraphQLResponses:
    """Test clean_data with actual Infrahub GraphQL responses."""

    def test_datacenter_capacity_validation_response(self) -> None:
        """Test clean_data with real DataCenterCapacityValidation query response."""
        # This is the actual response from Infrahub for DataCenterCapacityValidation query
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
                                "design_pattern": {
                                    "node": {
                                        "id": "187a1c22-ea67-42d9-3100-c51744dc5c08",
                                        "maximum_super_spines": {"value": 2},
                                        "maximum_pods": {"value": 4},
                                        "maximum_spines": {"value": 4},
                                        "maximum_leafs": {"value": 32},
                                        "maximum_tors": {"value": 96},
                                    }
                                },
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

        # Verify second pod
        pod2 = children[1]
        assert pod2["spine_count"] == 3
        assert pod2["leaf_count"] == 32
        assert pod2["tor_count"] == 0

        # Verify third pod (different counts)
        pod3 = children[2]
        assert pod3["spine_count"] == 4
        assert pod3["leaf_count"] == 16
        assert pod3["tor_count"] == 0

        # Verify design_pattern nested node
        design = dc["design_pattern"]
        assert design["id"] == "187a1c22-ea67-42d9-3100-c51744dc5c08"
        assert design["maximum_super_spines"] == 2
        assert design["maximum_pods"] == 4
        assert design["maximum_spines"] == 4
        assert design["maximum_leafs"] == 32
        assert design["maximum_tors"] == 96

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
                            "design_pattern": {
                                "node": {
                                    "maximum_pods": {"value": 4},
                                    "maximum_spines": {"value": 4},
                                }
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

        # Verify nested design_pattern
        assert result["design_pattern"]["maximum_pods"] == 4
        assert result["design_pattern"]["maximum_spines"] == 4

    def test_edges_priority_over_count(self) -> None:
        """Test that edges takes priority when both count and edges are present."""
        data = {
            "children": {
                "count": 3,
                "edges": [
                    {"node": {"name": {"value": "child1"}}},
                    {"node": {"name": {"value": "child2"}}},
                ],
            }
        }

        result = clean_data(data)

        # edges should take priority, returning list of 2 items (not count of 3)
        assert isinstance(result["children"], list)
        assert len(result["children"]) == 2
        assert result["children"][0]["name"] == "child1"
        assert result["children"][1]["name"] == "child2"

    def test_count_extraction_when_no_edges(self) -> None:
        """Test that count is extracted when edges is not present."""
        data = {
            "pod": {
                "spine_count": {"count": 4},
                "leaf_count": {"count": 16},
            }
        }

        result = clean_data(data)

        assert result["pod"]["spine_count"] == 4
        assert result["pod"]["leaf_count"] == 16

    def test_zero_count_not_treated_as_falsy(self) -> None:
        """Test that count of 0 is properly extracted (not treated as falsy)."""
        data = {
            "pod": {
                "tor_count": {"count": 0},
                "children": {"count": 0},
            }
        }

        result = clean_data(data)

        assert result["pod"]["tor_count"] == 0
        assert result["pod"]["children"] == 0

    def test_null_values_preserved(self) -> None:
        """Test that null/None values are preserved in extraction."""
        data = {
            "pod": {
                "name": None,
                "description": {"value": None},
            }
        }

        result = clean_data(data)

        # None value should be extracted from wrapper
        assert result["pod"]["name"] is None
        assert result["pod"]["description"] is None

    def test_id_extraction_from_nodes(self) -> None:
        """Test that id fields are properly extracted."""
        data = {
            "datacenter": {
                "id": "187a1cd7-11d2-663c-3105-c5103fc206fa",
                "design_pattern": {
                    "node": {
                        "id": "187a1c22-ea67-42d9-3100-c51744dc5c08",
                        "name": {"value": "L-Clos"},
                    }
                },
            }
        }

        result = clean_data(data)

        assert result["datacenter"]["id"] == "187a1cd7-11d2-663c-3105-c5103fc206fa"
        assert result["datacenter"]["design_pattern"]["id"] == "187a1c22-ea67-42d9-3100-c51744dc5c08"
        assert result["datacenter"]["design_pattern"]["name"] == "L-Clos"

    def test_mixed_count_and_edges_in_same_response(self) -> None:
        """Test response with both count-only and count+edges patterns."""
        data = {
            "datacenter": {
                "pods": {
                    "count": 3,
                    "edges": [
                        {
                            "node": {
                                "name": {"value": "pod-1"},
                                "devices": {
                                    "count": 5  # count without edges
                                },
                            }
                        }
                    ],
                }
            }
        }

        result = clean_data(data)

        # pods should be list (edges takes priority)
        assert isinstance(result["datacenter"]["pods"], list)
        assert len(result["datacenter"]["pods"]) == 1
        
        # devices should be count value (no edges present)
        assert result["datacenter"]["pods"][0]["name"] == "pod-1"
        assert result["datacenter"]["pods"][0]["devices"] == 5

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
                                                            "devices": {
                                                                "count": 10
                                                            },
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
