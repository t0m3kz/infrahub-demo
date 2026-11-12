"""Unwrap GraphQL response wrappers (``value``, ``count``, ``id``, ``node``, ``edges``) for checks, generators and
transforms.
"""

from typing import Any

# (predicate, extractor) rules for dict wrappers; the first match wins.
_EXTRACTION_RULES: list[tuple] = [
    (lambda v: "value" in v and len(v) == 1, lambda v: v["value"]),
    (lambda v: "id" in v and len(v) == 1, lambda v: v["id"]),
    (lambda v: "count" in v and len(v) == 1, lambda v: v["count"]),
    # "value" wins alongside other keys
    (lambda v: "value" in v, lambda v: v["value"]),
    # "edges" wins over "count"
    (lambda v: "edges" in v and v.get("edges") is not None, lambda v: clean_data(v["edges"])),
    (lambda v: "count" in v and v.get("count") is not None, lambda v: v["count"]),
]


def _extract_from_dict(value: dict) -> tuple[bool, Any]:
    """Try the extraction rules; returns ``(matched, result)``, the caller recurses when not matched."""
    for predicate, extractor in _EXTRACTION_RULES:
        if predicate(value):
            return True, extractor(value)

    if "node" in value:
        node = value.get("node")
        return True, clean_data(node) if node is not None else None

    return False, None


def clean_data(data: Any) -> Any:
    """Recursively unwrap ``{"value"}``, ``{"count"}``, ``{"id"}``, ``{"node"}`` and ``{"edges"}``; ``__`` is dropped
    from keys.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                matched, extracted = _extract_from_dict(value)
                result[key] = extracted if matched else clean_data(value)
            elif "__" in key:
                result[key.replace("__", "")] = value
            else:
                result[key] = clean_data(value)
        return result

    if isinstance(data, list):
        return [
            clean_data(item["node"]) if isinstance(item, dict) and item.get("node") is not None else clean_data(item)
            for item in data
        ]

    return data


def get_data(data: Any) -> Any:
    """The first value of the cleaned data (its first item when that is a list); ValueError if not a non-empty dict."""
    cleaned_data = clean_data(data)

    if isinstance(cleaned_data, dict) and cleaned_data:
        first_key = next(iter(cleaned_data))
        first_value = cleaned_data[first_key]

        if isinstance(first_value, list) and first_value:
            return first_value[0]

        return first_value

    raise ValueError("clean_data() did not return a non-empty dictionary")
