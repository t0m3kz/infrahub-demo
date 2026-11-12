"""The timeout and polling constants of the integration suite."""

REPO_SYNC_MAX_ATTEMPTS = 60

REPO_SYNC_POLL_INTERVAL = 5  # seconds


# Trigger-rule automations register asynchronously, 12-20s after `infrahubctl object load` returns.
TRIGGER_ACTIVATION_DELAY = 30  # seconds
