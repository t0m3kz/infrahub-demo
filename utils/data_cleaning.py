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

# Extraction rules for GraphQL dict wrappers, evaluated in priority order.
# Each rule is (predicate, extractor). First matching predicate wins.
_EXTRACTION_RULES: list[tuple] = [
    # Single-key wrappers: extract the only meaningful value
    (lambda v: "value" in v and len(v) == 1, lambda v: v["value"]),
    (lambda v: "id" in v and len(v) == 1, lambda v: v["id"]),
    (lambda v: "count" in v and len(v) == 1, lambda v: v["count"]),
    # Multi-key: "value" still wins when present alongside other keys
    (lambda v: "value" in v, lambda v: v["value"]),
    # Collections: "edges" takes priority over "count" when both present
    (lambda v: "edges" in v and v.get("edges") is not None, lambda v: clean_data(v["edges"])),
    (lambda v: "count" in v and v.get("count") is not None, lambda v: v["count"]),
]


def _extract_from_dict(value: dict) -> tuple[bool, Any]:
    """Try extraction rules on a dict value.

    Returns:
        (matched, result) — if matched is False, caller should recurse.
    """
    for predicate, extractor in _EXTRACTION_RULES:
        if predicate(value):
            return True, extractor(value)

    # Node relationships: extract if present, None if null
    if "node" in value:
        node = value.get("node")
        return True, clean_data(node) if node is not None else None

    return False, None


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
        result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                matched, extracted = _extract_from_dict(value)
                result[key] = extracted if matched else clean_data(value)
            elif "__" in key:
                # Handle flattened GraphQL field names (e.g., "device__name")
                result[key.replace("__", "")] = value
            else:
                result[key] = clean_data(value)
        return result

    if isinstance(data, list):
        return [
            clean_data(item["node"]) if isinstance(item, dict) and item.get("node") is not None else clean_data(item)
            for item in data
        ]

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
