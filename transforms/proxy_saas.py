import json
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from utils.data_cleaning import clean_data

from .common import get_data


def _flatten_url_patterns(url_categories: list[dict]) -> list[str]:
    """Flatten url_patterns from a list of ServiceProxyURLCategory objects.

    Each category carries a ``url_patterns`` field that is a newline-delimited
    string of patterns.  Split each entry, strip blanks, and return the merged
    flat list.
    """
    patterns: list[str] = []
    for category in url_categories:
        raw = category.get("url_patterns", "") or ""
        for line in raw.splitlines():
            line = line.strip()
            if line:
                patterns.append(line)
    return patterns


def _one_name(value: Any) -> str:
    """Return relation object name for cardinality-one relation."""
    if isinstance(value, dict):
        return value.get("name", "") or ""
    if isinstance(value, str):
        return value
    return ""


def _many_names(values: Any) -> list[str]:
    """Return relation object names for cardinality-many relation."""
    if not isinstance(values, list):
        return []
    names: list[str] = []
    for value in values:
        if isinstance(value, dict):
            name = value.get("name", "")
            if name:
                names.append(name)
        elif isinstance(value, str) and value:
            names.append(value)
    return names


def _build_policies(proxy_data: dict) -> list[dict]:
    """Build the ``policies`` list from cleaned proxy data."""
    policies_raw = proxy_data.get("policies", []) or []
    policies: list[dict] = []

    for policy_raw in policies_raw:
        rules_raw = policy_raw.get("rules", []) or []
        rules: list[dict] = []

        for rule_raw in rules_raw:
            url_categories = rule_raw.get("url_categories", []) or []
            url_patterns = _flatten_url_patterns(url_categories)

            # Source segments: list of network segment names
            source_segments = [seg.get("name", "") for seg in (rule_raw.get("source_segments", []) or [])]

            rules.append(
                {
                    "index": rule_raw.get("index", 0),
                    "name": rule_raw.get("name", ""),
                    "action": rule_raw.get("action", "forward"),
                    "destination_type": rule_raw.get("destination_type", "url_category"),
                    "destination_value": rule_raw.get("destination_value", ""),
                    "protocol": rule_raw.get("protocol", "any"),
                    "log": rule_raw.get("log", True),
                    "url_categories": url_patterns,
                    "source_segments": source_segments,
                }
            )

        policies.append(
            {
                "name": policy_raw.get("name", ""),
                "default_action": policy_raw.get("default_action", "forward"),
                "target_cloud_zone": _one_name(policy_raw.get("cloud_zone")),
                "target_colo_zone": _one_name(policy_raw.get("colo_zone")),
                "target_backend_pool": _one_name(policy_raw.get("backend_pool")),
                "target_cloud_instances": _many_names(policy_raw.get("cloud_instances")),
                "target_onprem_devices": _many_names(policy_raw.get("onprem_devices")),
                "auth_required": policy_raw.get("auth_required", False),
                "rules": rules,
            }
        )

    return policies


class ProxySaas(InfrahubTransform):
    """Transform proxy_saas_config query into a vendor-neutral JSON policy export.

    The output format is compatible with Zscaler ZIA / Prisma Access APIs and
    captures all forwarding policies, rules, and URL-category patterns stored in
    Infrahub.
    """

    query = "proxy_saas_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)

        # get_data() extracts the first node from edges in the cleaned response.
        # Fall back to direct dict traversal (same pattern as LoadBalancer).
        proxy_data = get_data(cleaned)

        if not proxy_data:
            # Fallback: look for any top-level key that contains proxy data
            for key, value in cleaned.items():
                if isinstance(value, list) and value:
                    proxy_data = value[0]
                    break
                elif isinstance(value, dict) and value:
                    proxy_data = value
                    break

        if not proxy_data:
            raise ValueError("No SaasProxy data found in query result")

        policies = _build_policies(proxy_data)

        output: dict[str, Any] = {
            "proxy_name": proxy_data.get("name", ""),
            "vendor": proxy_data.get("vendor", "zscaler"),
            "proxy_type": proxy_data.get("proxy_type", "zia"),
            "cloud_node": proxy_data.get("cloud_node", "zscaler.net"),
            "customer": proxy_data.get("customer", ""),
            "policies": policies,
        }

        return json.dumps(output, indent=2)
