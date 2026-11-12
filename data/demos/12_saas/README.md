# 12 - Zscaler Secure Internet Breakout Demo

A quick sample that models Zscaler POPs, underlay tunnels, web policies and where they sit in the topology via SaaS regions.

## What it shows

- Cloud security locations (Zscaler) pinned to real metros/buildings
- Optional IPSec underlay objects you can attach to edge devices
- Cloud policy/rules with explicit destinations

## Files

- `01_cloud_locations.yml` — defines Zscaler POPs as `SecurityCloudSecurityLocation` objects
- `02_cloud_policy.yml` — creates a cloud security policy and a few rules with explicit destinations
- `03_topology_saas.yml` — maps Zscaler as `TopologySaas` with regions tied to metros
  *(removed)* `04_external_connections.yml` — file removed; connectivity mapping dropped, topology-only mapping stays

## How to load

```bash
# create a working branch (example)
uv run infrahubctl branch create zscaler_demo

# load the demo objects
uv run infrahubctl object load data/demos/12_saas/ --branch zscaler_demo
```

## Next steps

- Attach `VPNConnection` objects to your edge devices pointing at the Zscaler endpoints
- Give a network segment its egress rules toward the SaaS endpoints in the segment's own `security_policy`
- Extend rules with `application_categories` if you also populate application data

For the application and proxy acceptance workflow, run `uv run invoke test-integration-all-demo`. Use `uv run invoke test-integration` when the SaaS changes are part of a broader topology update.
