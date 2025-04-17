"""Common functions for the generators."""

def clean_data(data):
    """
    Recursively transforms the input data
    by extracting 'value', 'node', or 'edges' from dictionaries.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                if value.get("value"):
                    result[key] = value["value"]
                elif value.get("node"):
                    result[key] = clean_data(value["node"])
                elif value.get("edges"):
                    result[key] = clean_data(value["edges"])
                elif not value.get("value"):
                    result[key] = None
                else:
                    result[key] = clean_data(value)
            else:
                result[key] = clean_data(value)
        return result
    if isinstance(data, list):
        result = []
        for item in data:
            if isinstance(item, dict) and item.get("node", None) is not None:
                result.append(clean_data(item["node"]))
                continue
            result.append(clean_data(item))
        return result
    return data


