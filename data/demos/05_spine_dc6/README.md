# 05 - Spine Added to a DC6 Pod

POD-1 of DC6 grows from 2 to 3 spines.

## What's Inside

- `00_design_element.yml`: a new `["3", "spine", "N9K-C9336C-FX2_SPINE"]` element (the 2-spine one is shared with the
  other DC6 pods, so it is not edited)
- `01_pod.yml`: `DC6-1-POD-1` upserted with that element as its `fabric_templates`, replacing the 2-spine entry

The `M_MIDDLE` design caps a pod at 3 spines, so this is the one spine POD-1 can take. The change of the pod's
`fabric_templates` runs `pod_rack_cascade`, which re-runs the pod and its racks: the new spine is created, cabled to
the super-spines and the pod's leafs, and every existing device keeps its underlay ASN. The other pods do not change.

## Deployment

Needs DC6 deployed (`data/demos/01_data_center/dc6`). As a reviewed change, after the earlier DC6 growth steps:

```bash
uv run invoke kick dc6-growth --from-stage spine
```

Or by hand:

```bash
uv run infrahubctl branch create your_branch
uv run infrahubctl object load data/demos/05_spine_dc6/ --branch your_branch
```

## Validation

The `spine` stage of the integration suite's `dc6-suite` kick (`tests/integration/kicks/dc6.yml`) asserts the
spine counts per pod and the unchanged ASNs.
