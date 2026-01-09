"""
Shared data cleaning utilities for Infrahub GraphQL responses.

This module provides unified data cleaning functions used across checks, generators,
and transforms to normalize GraphQL responses by extracting values from nested structures.

GraphQL Response Patterns:
- {"value": X} - Standard attribute values
- {"count": N} - Aggregation counts
- {"id": "uuid"} - Object identifiers
- {"node": {...}} - Single relationship
- {"edges": [{...}]} - Multiple relationships
"""

from typing import Any


def clean_data(data: Any) -> Any:
    """
    Recursively transforms GraphQL response data by extracting values from nested structures.

    This function handles common GraphQL response patterns including:
    - value: Standard attribute values
    - count: Aggregation results
    - id: Object identifiers
    - node: Single relationship objects
    - edges: Collections/lists of related objects

    Args:
        data: The input data to clean (dict, list, or primitive)

    Returns:
        Cleaned data with extracted values:
        - Dict values extracted from {"value": X}, {"count": N}, {"id": X}
        - Nested objects unwrapped from {"node": {...}}
        - Lists extracted from {"edges": [{...}]}
        - Keys with "__" replaced (e.g., "name__value" -> "name")

    Examples:
        >>> clean_data({"name": {"value": "DC-1"}})
        {"name": "DC-1"}

        >>> clean_data({"spine_count": {"count": 4}})
        {"spine_count": 4}

        >>> clean_data({"device": {"node": {"name": {"value": "leaf-01"}}}})
        {"device": {"name": "leaf-01"}}

        >>> clean_data({"interfaces": {"edges": [{"node": {"name": {"value": "eth0"}}}]}})
        {"interfaces": [{"name": "eth0"}]}
    """
    if isinstance(data, dict):
        dict_result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                # Extract common GraphQL wrapper fields in priority order
                # Use 'in' to check for key presence, then check if value is not None

                # Single-key wrappers: extract the wrapped value
                if "value" in value and len(value) == 1:
                    dict_result[key] = value["value"]
                elif "id" in value and len(value) == 1:
                    dict_result[key] = value["id"]
                elif "count" in value and len(value) == 1:
                    dict_result[key] = value["count"]
                # Multi-key or nested structures
                elif "value" in value:
                    # value key present but other keys too - extract value
                    dict_result[key] = value["value"]
                elif "edges" in value and value.get("edges") is not None:
                    # edges takes priority over count when both present
                    dict_result[key] = clean_data(value["edges"])
                elif "count" in value and value.get("count") is not None:
                    dict_result[key] = value["count"]
                elif "node" in value and value.get("node") is not None:
                    dict_result[key] = clean_data(value["node"])
                else:
                    # Recursively clean nested objects
                    dict_result[key] = clean_data(value)
            elif "__" in key:
                # Handle flattened GraphQL field names (e.g., "device__name")
                dict_result[key.replace("__", "")] = value
            else:
                dict_result[key] = clean_data(value)
        return dict_result

    if isinstance(data, list):
        list_result = []
        for item in data:
            # Unwrap nodes from edge collections
            if isinstance(item, dict) and item.get("node") is not None:
                list_result.append(clean_data(item["node"]))
            else:
                list_result.append(clean_data(item))
        return list_result

    # Return primitive values as-is
    return data


def get_data(data: Any) -> Any:
    """
    Extracts the first relevant value from cleaned GraphQL data.

    This helper is commonly used in transforms and checks to extract the primary
    object from a GraphQL response that may be wrapped in query structure.

    Args:
        data: Input data (typically from GraphQL query response)

    Returns:
        The first value from the cleaned data dictionary, or the first item
        if that value is a list.

    Raises:
        ValueError: If cleaned data is not a non-empty dictionary

    Examples:
        >>> get_data({"DcimDevice": {"edges": [{"node": {"name": {"value": "leaf-01"}}}]}})
        {"name": "leaf-01"}

        >>> get_data({"TopologyDataCenter": [{"name": {"value": "DC-1"}}]})
        {"name": "DC-1"}
    """
    cleaned_data = clean_data(data)

    if isinstance(cleaned_data, dict) and cleaned_data:
        first_key = next(iter(cleaned_data))
        first_value = cleaned_data[first_key]

        # If the first value is a list, return the first item
        if isinstance(first_value, list) and first_value:
            return first_value[0]

        return first_value

    raise ValueError("clean_data() did not return a non-empty dictionary")
