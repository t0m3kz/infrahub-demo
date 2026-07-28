# 21 - Zscaler Secure Internet Breakout Demo

Quick sample to model Zscaler POPs, underlay tunnels, web policies, and their placement in topology via SaaS regions.

## What it shows

- Cloud security locations (Zscaler) pinned to real metros/buildings
- Optional IPSec underlay objects you can attach to edge devices
- Cloud policy/rules using URL categories already in the seed data

## Files

- `00_url_categories.yml` — minimal URL categories so the demo loads standalone (used by the rules)
- `01_cloud_locations.yml` — defines Zscaler POPs as `SecurityCloudSecurityLocation` objects
- `02_cloud_policy.yml` — creates a cloud security policy and a few rules referencing URL categories
- `03_topology_saas.yml` — maps Zscaler as `TopologySaas` with regions tied to metros
  *(removed)* `04_external_connections.yml` — connectivity mapping dropped; topology-only mapping stays

## How to load

```bash
# create a working branch (example)
uv run infrahubctl branch create zscaler_demo

# load the demo objects
uv run infrahubctl object load data/demos/21_zscaler/ --branch zscaler_demo
```

## Next steps

- Attach `VPNConnection` objects to your edge devices pointing at the Zscaler endpoints
- Map specific user groups or network segments to the cloud policy via the `network_segments` relationship on `SecurityPolicy`
- Extend rules with `application_categories` if you also populate application data
