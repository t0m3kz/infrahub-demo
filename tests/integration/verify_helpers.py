"""Domain verification helpers for integration tests.

Re-exports from split modules for backwards compatibility:
- verify_devices: device counts and snapshots
- verify_routing: DC topology, routing sessions, ASN stability
- verify_segments: segment deployment verification
- verify_proposed_change: diff tree and artifact verification
"""

from .verify_devices import (
    snapshot_device_counts_by_role,
    verify_device_counts_growth,
    verify_devices_created,
)
from .verify_proposed_change import (
    verify_artifacts_generated,
    verify_proposed_change_diff,
)
from .verify_routing import (
    snapshot_dc_routing_state,
    snapshot_underlay_asn_by_role,
    verify_dc_deployment,
    verify_dc_roles_exact,
    verify_dc_topology,
    verify_routing_sessions,
    verify_underlay_asn_unchanged,
)
from .verify_segments import verify_segment_deployments

__all__ = [
    "snapshot_dc_routing_state",
    "snapshot_device_counts_by_role",
    "snapshot_underlay_asn_by_role",
    "verify_artifacts_generated",
    "verify_dc_deployment",
    "verify_dc_roles_exact",
    "verify_dc_topology",
    "verify_device_counts_growth",
    "verify_devices_created",
    "verify_proposed_change_diff",
    "verify_routing_sessions",
    "verify_segment_deployments",
    "verify_underlay_asn_unchanged",
]
