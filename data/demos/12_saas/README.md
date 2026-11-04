# 12 - Zscaler Secure Internet Breakout Demo

A quick sample that models Zscaler POPs, underlay tunnels, web policies and where they sit in the topology via SaaS regions. Think of it as a polite way to send your traffic through somebody else's data center before it reaches the internet it was going to visit anyway.

## What it shows

- Cloud security locations (Zscaler) pinned to real metros/buildings, because even "the cloud" has a postal address
- Optional IPSec underlay objects you can attach to edge devices (optional, in the sense that tunnels are optional until the first audit)
- Cloud policy/rules with explicit destinations, so nobody has to guess where "any" was meant to go

## Files

- `01_cloud_locations.yml` — defines Zscaler POPs as `SecurityCloudSecurityLocation` objects
- `02_cloud_policy.yml` — creates a cloud security policy and a few rules with explicit destinations
- `03_topology_saas.yml` — maps Zscaler as `TopologySaas` with regions tied to metros
  *(removed)* `04_external_connections.yml` — connectivity mapping dropped; topology-only mapping stays (it did not survive review, which is the polite way of saying it was deleted)

## How to load

```bash
# create a working branch (example)
uv run infrahubctl branch create zscaler_demo

# load the demo objects
uv run infrahubctl object load data/demos/12_saas/ --branch zscaler_demo
```

## Next steps

- Attach `VPNConnection` objects to your edge devices pointing at the Zscaler endpoints (yes, you still have to do the tunnels yourself)
- Give a network segment its egress rules toward the SaaS endpoints in the segment's own `security_policy` (one per segment, not one per mood)
- Extend rules with `application_categories` if you also populate application data

For the application and proxy acceptance workflow, run `uv run invoke test-integration-all-demo`. Use `uv run invoke test-integration` when the SaaS changes are part of a broader topology update, which they will be, eventually.
