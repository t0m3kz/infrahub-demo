"""Tests for shared data cleaning utilities."""

import pytest

from utils.data_cleaning import clean_data, get_data


class TestCleanData:
    """Test cases for clean_data function."""

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

    def test_flatten_double_underscore_keys(self) -> None:
        """Test flattening of double underscore keys."""
        data = {"device__name": "leaf-01"}
        result = clean_data(data)
        assert result == {"devicename": "leaf-01"}

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

    def test_list_without_nodes(self) -> None:
        """Test list of plain objects without node wrappers."""
        data = [{"name": "item1"}, {"name": "item2"}]
        result = clean_data(data)
        assert result == [{"name": "item1"}, {"name": "item2"}]

    def test_primitive_value(self) -> None:
        """Test that primitive values pass through unchanged."""
        assert clean_data("string") == "string"
        assert clean_data(42) == 42
        assert clean_data(3.14) == 3.14
        assert clean_data(True) is True
        assert clean_data(None) is None

    def test_empty_dict(self) -> None:
        """Test empty dictionary."""
        assert clean_data({}) == {}

    def test_empty_list(self) -> None:
        """Test empty list."""
        assert clean_data([]) == []


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
