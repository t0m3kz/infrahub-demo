"""Pod layouts for unit-test fixtures that think in deployment types."""

from __future__ import annotations

# One real POD_LAYOUTS entry per deployment type: generators derive the type
# from the layout (generators.pod_config.pod_deployment_type).
LAYOUT_FOR_DEPLOYMENT_TYPE = {"middle_rack": "S_MIDDLE", "tor": "S_TOR", "mixed": "S_MIXED"}
