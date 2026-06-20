"""Shared constants for integration tests."""

# ---------------------------------------------------------------------------
# Demo data paths  (single source of truth — same files as invoke demo.run-demo)
# ---------------------------------------------------------------------------

DEMO_DC_DATA_ROOT = "data/demos/01_data_center"
DEMO_SWITCH_DATA = "data/demos/02_switch"
DEMO_RACK_DATA = "data/demos/03_rack"
DEMO_POD_DATA = "data/demos/04_pod"
DEMO_SERVERS_DATA = "data/demos/06_servers"

# Timeout and polling constants
REPO_SYNC_MAX_ATTEMPTS = 60
REPO_SYNC_POLL_INTERVAL = 5  # seconds
GENERATOR_DEFINITION_MAX_ATTEMPTS = 10
GENERATOR_DEFINITION_POLL_INTERVAL = 5  # seconds
GENERATOR_TASK_TIMEOUT = 1800  # 30 minutes
DIFF_TASK_TIMEOUT = 600  # 10 minutes
MERGE_TASK_TIMEOUT = 600  # 10 minutes
VALIDATION_MAX_ATTEMPTS = 30
VALIDATION_POLL_INTERVAL = 10  # seconds
DATA_PROPAGATION_DELAY = 3  # seconds
MERGE_PROPAGATION_DELAY = 5  # seconds
BRANCH_ENDPOINT_TIMEOUT = 120  # seconds
