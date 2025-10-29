"""Unit tests for clean_data method in CommonGenerator."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

# Add parent directory to path so we can import generators
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from generators.common import CommonGenerator


def create_test_generator() -> CommonGenerator:
    """Create a mocked CommonGenerator instance for testing clean_data."""
    # Create instance using __new__ to bypass __init__ requirements
    gen = CommonGenerator.__new__(CommonGenerator)
    gen.client = Mock()
    gen.logger = Mock()
    gen.query = Mock()  # type: ignore
    gen.infrahub_node = Mock()  # type: ignore

    return gen


class TestCleanDataBasic:
    """Test clean_data with basic data types and structures."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        return create_test_generator()

    def test_clean_data_scalar_string(self, generator: CommonGenerator) -> None:
        """Test clean_data with scalar string value."""
        result = generator.clean_data("test_value")
        assert result == "test_value"

    def test_clean_data_scalar_int(self, generator: CommonGenerator) -> None:
        """Test clean_data with scalar integer value."""
        result = generator.clean_data(42)
        assert result == 42

    def test_clean_data_scalar_float(self, generator: CommonGenerator) -> None:
        """Test clean_data with scalar float value."""
        result = generator.clean_data(3.14)
        assert result == 3.14

    def test_clean_data_scalar_bool(self, generator: CommonGenerator) -> None:
        """Test clean_data with scalar boolean value."""
        result = generator.clean_data(True)
        assert result is True

    def test_clean_data_scalar_none(self, generator: CommonGenerator) -> None:
        """Test clean_data with None value."""
        result = generator.clean_data(None)
        assert result is None

    def test_clean_data_empty_dict(self, generator: CommonGenerator) -> None:
        """Test clean_data with empty dictionary."""
        result = generator.clean_data({})
        assert result == {}

    def test_clean_data_empty_list(self, generator: CommonGenerator) -> None:
        """Test clean_data with empty list."""
        result = generator.clean_data([])
        assert result == []

    def test_clean_data_simple_dict(self, generator: CommonGenerator) -> None:
        """Test clean_data with simple dictionary (no wrappers)."""
        data = {"name": "test", "age": 30, "active": True}
        result = generator.clean_data(data)
        assert result == data

    def test_clean_data_simple_list(self, generator: CommonGenerator) -> None:
        """Test clean_data with simple list of scalars."""
        data = [1, 2, 3, 4, 5]
        result = generator.clean_data(data)
        assert result == data


class TestCleanDataSingleKeyWrappers:
    """Test clean_data with single-key wrapper unwrapping."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_value_wrapper(self, generator: CommonGenerator) -> None:
        """Test clean_data unwraps single 'value' key."""
        data = {"name": {"value": "Test Name"}}
        result = generator.clean_data(data)
        assert result == {"name": "Test Name"}

    def test_clean_data_node_wrapper(self, generator: CommonGenerator) -> None:
        """Test clean_data unwraps single 'node' key."""
        data = {"user": {"node": {"id": "123", "name": "John"}}}
        result = generator.clean_data(data)
        assert result == {"user": {"id": "123", "name": "John"}}

    def test_clean_data_parent_wrapper(self, generator: CommonGenerator) -> None:
        """Test clean_data unwraps single 'parent' key."""
        data = {"location": {"parent": {"id": "parent-id", "name": "HQ"}}}
        result = generator.clean_data(data)
        assert result == {"location": {"id": "parent-id", "name": "HQ"}}

    def test_clean_data_edges_wrapper_single_node(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data unwraps single 'edges' key with node inside."""
        data = {"items": {"edges": [{"node": {"id": "1", "name": "Item 1"}}]}}
        result = generator.clean_data(data)
        assert result == {"items": [{"id": "1", "name": "Item 1"}]}

    def test_clean_data_nested_value_wrappers(self, generator: CommonGenerator) -> None:
        """Test clean_data unwraps nested value wrappers."""
        data = {
            "pod": {
                "name": {"value": "Pod-A1"},
                "index": {"value": 1},
                "role": {"value": "cpu"},
            }
        }
        result = generator.clean_data(data)
        expected = {"pod": {"name": "Pod-A1", "index": 1, "role": "cpu"}}
        assert result == expected

    def test_clean_data_deeply_nested_value_wrappers(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data unwraps deeply nested value wrappers."""
        data = {"level1": {"level2": {"level3": {"value": "deep_value"}}}}
        result = generator.clean_data(data)
        assert result == {"level1": {"level2": {"level3": "deep_value"}}}


class TestCleanDataMultiKeyDictionaries:
    """Test clean_data with multi-key dictionaries (no single-key unwrapping)."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_multi_key_dict_preserved(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data preserves multi-key dictionaries (not a wrapper)."""
        data = {"field": {"value": "test", "extra": "data"}}
        result = generator.clean_data(data)
        # Multi-key dict is not a wrapper, so recurse on both keys
        assert result == {"field": {"value": "test", "extra": "data"}}

    def test_clean_data_node_with_multiple_fields(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with node containing multiple fields."""
        data = {
            "device": {
                "node": {
                    "id": "device-1",
                    "name": {"value": "Router-1"},
                    "type": {"value": "Arista"},
                }
            }
        }
        result = generator.clean_data(data)
        expected = {"device": {"id": "device-1", "name": "Router-1", "type": "Arista"}}
        assert result == expected


class TestCleanDataEdgesAndLists:
    """Test clean_data with edges and list structures."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_edges_with_multiple_nodes(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data unwraps edges with multiple nodes."""
        data = {
            "devices": {
                "edges": [
                    {"node": {"id": "1", "name": "Device-1"}},
                    {"node": {"id": "2", "name": "Device-2"}},
                    {"node": {"id": "3", "name": "Device-3"}},
                ]
            }
        }
        result = generator.clean_data(data)
        expected = {
            "devices": [
                {"id": "1", "name": "Device-1"},
                {"id": "2", "name": "Device-2"},
                {"id": "3", "name": "Device-3"},
            ]
        }
        assert result == expected

    def test_clean_data_nested_edges_with_nested_value_wrappers(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with edges containing value wrappers."""
        data = {
            "interfaces": {
                "edges": [
                    {"node": {"name": {"value": "Ethernet1/1"}}},
                    {"node": {"name": {"value": "Ethernet1/2"}}},
                ]
            }
        }
        result = generator.clean_data(data)
        expected = {
            "interfaces": [
                {"name": "Ethernet1/1"},
                {"name": "Ethernet1/2"},
            ]
        }
        assert result == expected

    def test_clean_data_list_without_node_wrapper(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with list items without node wrapper."""
        data = {
            "tags": [
                {"name": "tag1"},
                {"name": "tag2"},
            ]
        }
        result = generator.clean_data(data)
        expected = {
            "tags": [
                {"name": "tag1"},
                {"name": "tag2"},
            ]
        }
        assert result == expected


class TestCleanDataGraphQLTypes:
    """Test clean_data with GraphQL typename fields."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_typename_normalized(self, generator: CommonGenerator) -> None:
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
        result = generator.clean_data(data)
        expected = {
            "device": {"typename": "DcimPhysicalDevice", "id": "1", "name": "Device-1"}
        }
        assert result == expected

    def test_clean_data_multiple_double_underscores_normalized(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data normalizes __typename but not other __ keys in multi-key dict."""
        data = {"__typename": "Query", "__schema": {"node": {"__field": "value"}}}
        result = generator.clean_data(data)
        # Multi-key dict doesn't trigger pure wrapper unwrapping,
        # so __typename gets normalized but __schema stays as-is
        # because it has a "node" wrapper
        assert "typename" in result  # __typename becomes typename
        assert result["__schema"]["field"] == "value"  # __field in node becomes field


class TestCleanDataComplexGraphQLResponse:
    """Test clean_data with complex real-world GraphQL responses."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_complex_topology_pod_response(
        self, generator: CommonGenerator
    ) -> None:
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
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/31"}
                                                }
                                            },
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/32"}
                                                }
                                            },
                                        ]
                                    },
                                }
                            },
                            "parent": {
                                "node": {"id": "dc-uuid-1", "name": {"value": "DC-1"}}
                            },
                        }
                    }
                ]
            }
        }
        result = generator.clean_data(data)

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

    def test_clean_data_multi_query_response(self, generator: CommonGenerator) -> None:
        """Test clean_data with response containing multiple top-level queries."""
        data = {
            "TopologyDeployment": {
                "edges": [
                    {"node": {"id": "deploy-1"}},
                    {"node": {"id": "deploy-2"}},
                ]
            },
            "TopologyPod": {
                "edges": [
                    {"node": {"id": "pod-1"}},
                    {"node": {"id": "pod-2"}},
                ]
            },
        }
        result = generator.clean_data(data)

        assert isinstance(result["TopologyDeployment"], list)
        assert len(result["TopologyDeployment"]) == 2
        assert result["TopologyDeployment"][0]["id"] == "deploy-1"

        assert isinstance(result["TopologyPod"], list)
        assert len(result["TopologyPod"]) == 2
        assert result["TopologyPod"][0]["id"] == "pod-1"

    def test_clean_data_wrapped_response(self, generator: CommonGenerator) -> None:
        """Test clean_data with data wrapper around queries."""
        data = {
            "data": {
                "TopologyDeployment": {
                    "edges": [{"node": {"id": "deploy-1", "name": {"value": "DC-1"}}}]
                }
            }
        }
        result = generator.clean_data(data)

        # Data wrapper should be preserved since it's not a pure single-key wrapper
        # when considering the overall structure
        assert "data" in result
        assert isinstance(result["data"]["TopologyDeployment"], list)
        assert result["data"]["TopologyDeployment"][0]["id"] == "deploy-1"
        assert result["data"]["TopologyDeployment"][0]["name"] == "DC-1"

    def test_clean_data_comprehensive_pod_generation_query(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with comprehensive pod generation query response."""
        # This is a realistic pod query response with all nested structures
        data = {
            "TopologyPod": {
                "edges": [
                    {
                        "node": {
                            "id": "1871d83c-3313-5b7d-3029-c51b776edc7e",
                            "amount_of_spines": {"value": 4},
                            "name": {"value": "Pod-A1"},
                            "checksum": {
                                "value": "251b527b42bbf34e0dbbdc5e6577aa132bde9a6a1e72fffecd7c1225482d1936"
                            },
                            "index": {"value": 1},
                            "role": {"value": "cpu"},
                            "spine_switch_template": {
                                "node": {
                                    "id": "1871d302-6519-7020-3022-c51818a0c08b",
                                    "interfaces": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/31"}
                                                }
                                            },
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/32"}
                                                }
                                            },
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/33"}
                                                }
                                            },
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/34"}
                                                }
                                            },
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/35"}
                                                }
                                            },
                                            {
                                                "node": {
                                                    "name": {"value": "Ethernet1/36"}
                                                }
                                            },
                                        ]
                                    },
                                }
                            },
                            "parent": {
                                "node": {
                                    "id": "1871d5de-d149-c98d-3025-c515100cb750",
                                    "name": {"value": "DC-1"},
                                    "amount_of_super_spines": {"value": 6},
                                    "fabric_interface_sorting_method": {
                                        "value": "top_down"
                                    },
                                    "spine_interface_sorting_method": {
                                        "value": "bottom_up"
                                    },
                                    "super_spine_switch_template": {
                                        "node": {
                                            "interfaces": {
                                                "edges": [
                                                    {
                                                        "node": {
                                                            "name": {
                                                                "value": "Ethernet1/1"
                                                            }
                                                        }
                                                    },
                                                    {
                                                        "node": {
                                                            "name": {
                                                                "value": "Ethernet1/2"
                                                            }
                                                        }
                                                    },
                                                    {
                                                        "node": {
                                                            "name": {
                                                                "value": "Ethernet1/3"
                                                            }
                                                        }
                                                    },
                                                ]
                                            }
                                        }
                                    },
                                }
                            },
                        }
                    }
                ]
            }
        }

        result = generator.clean_data(data)

        # Verify top-level structure
        assert isinstance(result["TopologyPod"], list)
        assert len(result["TopologyPod"]) == 1

        pod = result["TopologyPod"][0]

        # Verify pod-level fields
        assert pod["id"] == "1871d83c-3313-5b7d-3029-c51b776edc7e"
        assert pod["name"] == "Pod-A1"
        assert pod["amount_of_spines"] == 4
        assert pod["index"] == 1
        assert pod["role"] == "cpu"

        # Verify spine template and interfaces
        template = pod["spine_switch_template"]
        assert template["id"] == "1871d302-6519-7020-3022-c51818a0c08b"
        assert isinstance(template["interfaces"], list)
        assert len(template["interfaces"]) == 6
        assert template["interfaces"][0]["name"] == "Ethernet1/31"
        assert template["interfaces"][5]["name"] == "Ethernet1/36"

        # Verify parent datacenter
        parent = pod["parent"]
        assert parent["id"] == "1871d5de-d149-c98d-3025-c515100cb750"
        assert parent["name"] == "DC-1"
        assert parent["amount_of_super_spines"] == 6
        assert parent["fabric_interface_sorting_method"] == "top_down"
        assert parent["spine_interface_sorting_method"] == "bottom_up"

        # Verify super-spine template interfaces
        super_spine_template = parent["super_spine_switch_template"]
        assert isinstance(super_spine_template["interfaces"], list)
        assert len(super_spine_template["interfaces"]) == 3
        assert super_spine_template["interfaces"][0]["name"] == "Ethernet1/1"
        assert super_spine_template["interfaces"][1]["name"] == "Ethernet1/2"
        assert super_spine_template["interfaces"][2]["name"] == "Ethernet1/3"


class TestCleanDataEdgeCases:
    """Test clean_data with edge cases and special scenarios."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_empty_edges_list(self, generator: CommonGenerator) -> None:
        """Test clean_data with empty edges list (not a pure wrapper)."""
        data: dict[str, dict[str, list]] = {"items": {"edges": []}}
        result = generator.clean_data(data)
        # Empty edges list doesn't trigger unwrapping
        # because len(value) != 1 (it's 0), so not a pure wrapper
        assert result == {"items": {"edges": []}}

    def test_clean_data_mixed_list_items(self, generator: CommonGenerator) -> None:
        """Test clean_data with list containing both node-wrapped and non-wrapped items."""
        data = {
            "mixed": [
                {"node": {"id": "1"}},
                {"id": "2"},
            ]
        }
        result = generator.clean_data(data)
        expected = {
            "mixed": [
                {"id": "1"},
                {"id": "2"},
            ]
        }
        assert result == expected

    def test_clean_data_wrapper_key_order(self, generator: CommonGenerator) -> None:
        """Test clean_data respects wrapper key priority order."""
        # When multiple wrapper keys could apply, the first in WRAPPER_KEYS should win
        # For single-key dict with 'value', should unwrap to the value
        data = {"field": {"value": "test"}}
        result = generator.clean_data(data)
        assert result == {"field": "test"}

    def test_clean_data_nested_list_of_dicts(self, generator: CommonGenerator) -> None:
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
        result = generator.clean_data(data)

        assert len(result["devices"]) == 2
        assert result["devices"][0]["name"] == "Device-1"
        assert result["devices"][0]["interfaces"][0]["name"] == "Eth0"
        assert result["devices"][1]["name"] == "Device-2"
        assert result["devices"][1]["interfaces"][0]["name"] == "Eth2"

    def test_clean_data_single_item_edges_preserved_as_list(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data preserves single-item edges as a list."""
        data = {"item": {"edges": [{"node": {"id": "single-item"}}]}}
        result = generator.clean_data(data)
        # edges wrapper should be unwrapped to a list
        assert isinstance(result["item"], list)
        assert len(result["item"]) == 1
        assert result["item"][0]["id"] == "single-item"

    def test_clean_data_value_wrapper_with_null(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with value wrapper containing None (falsy, not unwrapped)."""
        data = {"field": {"value": None}}
        result = generator.clean_data(data)
        # value.get("value") returns None, which is falsy
        # so the condition `if value.get(wrapper_key) and len(value) == 1` fails
        # None is falsy, so it doesn't unwrap
        assert result == {"field": {"value": None}}

    def test_clean_data_value_wrapper_with_empty_string(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with value wrapper containing empty string (falsy, not unwrapped)."""
        data = {"field": {"value": ""}}
        result = generator.clean_data(data)
        # Empty string is falsy, so the condition `if value.get(wrapper_key) and len(value) == 1`
        # fails because "" is falsy, so it doesn't unwrap
        assert result == {"field": {"value": ""}}

    def test_clean_data_deeply_nested_mixed_wrappers(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data with deeply nested mixed wrapper types."""
        data = {
            "level1": {
                "edges": [
                    {
                        "node": {
                            "level2": {"value": "deeply_nested"},
                            "level3": {"parent": {"id": "parent-id"}},
                        }
                    }
                ]
            }
        }
        result = generator.clean_data(data)

        assert isinstance(result["level1"], list)
        assert result["level1"][0]["level2"] == "deeply_nested"
        assert result["level1"][0]["level3"]["id"] == "parent-id"


class TestCleanDataIdempotency:
    """Test clean_data idempotency and consistency."""

    @pytest.fixture
    def generator(self) -> CommonGenerator:
        """Create CommonGenerator instance for testing."""
        mock_client = Mock()
        mock_logger = Mock()
        return create_test_generator()

    def test_clean_data_idempotent_on_cleaned_data(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data applied twice produces same result as once."""
        data = {
            "pod": {
                "name": {"value": "Pod-A1"},
                "interfaces": {
                    "edges": [
                        {"node": {"name": {"value": "Eth0"}}},
                    ]
                },
            }
        }

        result1 = generator.clean_data(data)
        result2 = generator.clean_data(result1)

        # Second clean should not change anything
        assert result1 == result2

    def test_clean_data_consistent_across_calls(
        self, generator: CommonGenerator
    ) -> None:
        """Test clean_data produces consistent results across multiple calls."""
        data = {
            "TopologyPod": {
                "edges": [{"node": {"id": "pod-1", "name": {"value": "Pod-A1"}}}]
            }
        }

        result1 = generator.clean_data(data)
        result2 = generator.clean_data(data)

        assert result1 == result2
