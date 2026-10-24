"""Demo data paths, the DC6 expansion chain scenario and the timeout and polling constants of the integration suite."""


# ---------------------------------------------------------------------------
# Demo data paths  (single source of truth — the same files the demos ship)
# ---------------------------------------------------------------------------

DEMO_DC_DATA_ROOT = "data/demos/01_data_center"

DEMO_SWITCH_DATA = "data/demos/02_switch_dc6"

DEMO_RACK_DATA = "data/demos/03_rack_dc6"

DEMO_POD_DATA = "data/demos/04_pod_dc6"

DEMO_SERVERS_DATA = "data/demos/06_servers"


# ---------------------------------------------------------------------------
# DC6 expansion chain (test_12 - test_20) — grows the DC6 fabric deployed by
# test_10 with the DC6 expansion demos shipped in data/demos/.
# ---------------------------------------------------------------------------

SCENARIO_DC_NAME = "DC6"

SCENARIO_POD_1 = "DC6-1-POD-1"


# Test-local segment data: there is no standalone-loadable DC6 segment demo
# (data/demos/07_customers and 08_segments depend on the 30_all customer
# organizations and still use the pre-d4801531 [org_id, environment]
# customer-deployment HFID), so the scenario boards its own footprint.
SCENARIO_SEGMENT_DATA = "tests/integration/data/20_segments"

SCENARIO_SEGMENT_FOOTPRINT = "C001-P-DC6"


# Test-local application data for test_20: data/demos/06_servers declares no
# AppComponent, and a local segment's VLAN ID is realized only where a
# component's instance is cabled to a switch (add_app_component_segment), so
# the scenario declares one component on a SCENARIO_SEGMENT_DATA segment,
# instanced on one of the servers test_20 cables.
SCENARIO_ENDPOINT_APP_DATA = "tests/integration/data/20_endpoints"

SCENARIO_ENDPOINT_APPLICATION = "c001-dc6-endpoint-probe-p"

SCENARIO_ENDPOINT_COMPONENT_FQDN = "web.dc6-endpoint-probe.c001.test.local"

SCENARIO_ENDPOINT_SEGMENT = "c001-web-app-p"

SCENARIO_ENDPOINT_SERVER = "ktw-pod1-server-1"


# Every switching role a DC6 scenario can add. Counted per DC, so a role that
# never appears in DC6 just stays at 0 on both sides of the comparison.
SCENARIO_FABRIC_ROLES = ["super-spine", "spine", "leaf", "access-leaf", "l2-leaf", "tor", "border-leaf"]


# Roles whose existing devices must keep their underlay ASN while the chain
# grows DC6 (ebgp-ibgp). l2-leaf is L2-only (no BGP) so it is not listed;
# access-leaf only exists from test_14 on, so its baseline is empty before that.
SCENARIO_UNDERLAY_ROLES = ["super-spine", "spine", "leaf", "access-leaf", "tor"]


# Timeout and polling constants
REPO_SYNC_MAX_ATTEMPTS = 60

REPO_SYNC_POLL_INTERVAL = 5  # seconds

GENERATOR_TASK_TIMEOUT = 1800  # 30 minutes

DIFF_TASK_TIMEOUT = 600  # 10 minutes

MERGE_TASK_TIMEOUT = 600  # 10 minutes

# Consecutive empty polls (5s apart) of main's task queue before a merge's
# post-merge work counts as done — triggers schedule it a few seconds late.
POST_MERGE_STABLE_ZERO = 3

VALIDATION_MAX_ATTEMPTS = 30

VALIDATION_POLL_INTERVAL = 10  # seconds

DATA_PROPAGATION_DELAY = 3  # seconds


# CoreNodeTriggerRule nodes (loaded by test_03_load_events) exist in the graph
# as soon as `infrahubctl object load` returns, but the underlying Prefect
# automations that actually listen for created/updated events are registered
# separately and asynchronously by a background worker — confirmed live at a
# consistent 12-20s lag behind the object-load command completing. A DC bulk
# load that starts before that registration finishes has its pods'/racks'
# created events fired into a system with nothing listening yet, silently
# dropping them (no error — the objects just never get generated). This has
# only ever bitten the very first DC in the suite; by the time later DCs run,
# the gap has long closed on its own.
TRIGGER_ACTIVATION_DELAY = 30  # seconds
