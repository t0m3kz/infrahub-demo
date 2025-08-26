from typing import Any


def clean_data(data: Any) -> Any:
    """
    Recursively transforms the input data
    by extracting 'value', 'node', or 'edges' from dictionaries.
    """
    if isinstance(data, dict):
        dict_result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                if value.get("value"):
                    dict_result[key] = value["value"]
                elif value.get("node"):
                    dict_result[key] = clean_data(value["node"])
                elif value.get("edges"):
                    dict_result[key] = clean_data(value["edges"])
                elif not value.get("value"):
                    dict_result[key] = None
                else:
                    dict_result[key] = clean_data(value)
            elif "__" in key:
                dict_result[key.replace("__", "")] = value
            else:
                dict_result[key] = clean_data(value)
        return dict_result
    if isinstance(data, list):
        list_result = []
        for item in data:
            if isinstance(item, dict) and item.get("node", None) is not None:
                list_result.append(clean_data(item["node"]))
                continue
            list_result.append(clean_data(item))
        return list_result
    return data


def get_data(data: Any) -> Any:
    """
    Extracts the relevant data from the input.
    Returns the first value from the cleaned data dictionary.
    """
    cleaned_data = clean_data(data)
    if isinstance(cleaned_data, dict) and cleaned_data:
        first_key = next(iter(cleaned_data))
        first_value = cleaned_data[first_key]
        if isinstance(first_value, list) and first_value:
            return first_value[0]
        return first_value
    else:
        raise ValueError("clean_data() did not return a non-empty dictionary")


def validate_interfaces(data: dict[str, Any]) -> list[str]:
    """
    Validates that the device has interfaces and that loopback interfaces have IP addresses.
    """
    errors: list[str] = []
    if len(data.get("interfaces", [])) == 0:
        errors.append("You're MORON !!! You removed all interfaces.")

    for interface in data.get("interfaces", []):
        if interface.get("role") == "loopback" and not interface.get("ip_addresses"):
            errors.append("You're MORON !!! You removed ip from loopback.")

    return errors
