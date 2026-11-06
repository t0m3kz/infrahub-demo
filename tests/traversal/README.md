# Fast model test (traversal)

Instead of an hour of integration tests: the schema plus a small model on a throwaway branch, then assertions on a walk
over the graph (L1: cables, ports, patch panels, carrier circuits, a transport with its ports and bundle; L3: SVI/VRF,
BGP peerings, session circuits, the cloud peering interface, gateway, hub, virtual network, cloud segment) in both
directions. Goal: a few minutes.

```bash
uv run invoke check-model                      # every case of cases.yml
uv run invoke check-model --case full_topology
uv run invoke check-model --update-golden      # write the golden files tests/traversal/golden/*.txt
uv run invoke check-model --keep-branch        # keep the tv-* branch for inspection
uv run invoke check-model --rebuild            # clean the test containers and build the stack from scratch
uv run invoke check-model --reach              # only the far-end matrix: ONE shortest path per pair, a minute or two
uv run invoke check-model --pair "lis|office"  # only the pairs whose 'src dst golden' matches the regular expression
uv run invoke check-model --jobs 1             # pairs traced one at a time (default 2, the rows keep the order of cases.yml)
```

Steps: (1) the stack on `:8100` (if it does not answer on `/api/info`, the runner starts it like the integration test
`test_01`; about 5 minutes once, the containers stay); (2) a branch `tv-<time>`; (3) `infrahubctl schema load` from the
working tree onto that branch (server refusals are printed verbatim); (4) `infrahubctl object load` of the data
directories, in order; (5) for every segment pair: read the graph, walk L1 and L3, assert, compare with the golden file;
(6) delete the branch (also after a failure) and print the total time. Nothing is written to `main`.

The paths are the server's (`utils/path_trace.py`, `traverse_paths`): a pair in `cases.yml` gives its ends as
`kind:name` (`src`, `dst`) and, when the target must be reachable through a specific transport, `through` (one `--via`
trace per transport). Assertions per pair: L1 and L3 reach the target, the equal-cost set of paths (ECMP, minimal depth)
is complete (`max_paths` limit 100 above its size, the server budget not truncated), every chain crosses a transport of
an allowed kind (`far_end_kinds`), the circuit types on the transport chains (`through`: carriers in L1, session
circuits in L3; a carrier is named by its `circuit_id`), the reverse direction gives the same chains, the transport
holds the required interfaces (`transport_kinds`), agreement with the golden file (sorted `kind:hfid` chains). A
negative pair (`expect_unreachable`): a complete empty result in both layers with the exact message of the server
search. On failure the PASS/FAIL table shows the tracer result or the difference from the golden file.

## Data directories, pod, internet, pairs

`data:` is the directory of the sample (`data/demos/00_full_topology`: the numbered folders are the load order, one
`infrahubctl object load`); `tests/traversal/load_topology.py` loads it (`async apply(client, branch)`; command:
`uv run python tests/traversal/load_topology.py --branch main`): the load is idempotent, so a branch that inherited the data from
`main` after `test_03` loads again without a skip. `tests/integration/test_03_full_topology.py` runs the same assertions on the `main` of the
stack (`assert_case` from `check_model.py`, part of `invoke test-integration-fast`). Case keys: `pods` (one entry per DC: the
MLAG, the dual-homed bond, the deployment of the leaves, spine and server to the pod and of the border leaves to the DC,
VLAN rows from a data read; the number of distinct L1 device paths server -> edge, the overlay sessions (`leaves_to_edge`:
the leaves with a direct session to the edge) and the L3 overlay from both leaves, from the server's chains; the bond is
followed through the patch panels of the racks), `internet` (INTERNET: one object under the label
Internet with one interface, two ISPs and the public sides of a SaaS and of a cloud attached to it; L1 from the edge
port through each ISP and INTERNET to the port of the public side: one chain, three carriers by `circuit_id`; golden
file `full_topology__internet.txt`; L1 to the ISP router, L3 through the INTERNET space, no tunnel),
`transport_sub_interfaces` (a transport port and bundle see their peerings in `sub_interfaces`); the data reads are in
`tests/traversal/walks.py` (they read `d` from `graph_read.py`, no path search), the judging of chains in
`tests/traversal/chain_rows.py` (with unit tests). `inventory_golden` is what the chains do not show: the peering
interfaces (scope, peer AS, announced prefixes), the gateways with their AS, the allowed prefixes of the attachments,
the interfaces of the transports. Pair keys: `src`, `dst`, `through`, `far_end_kinds`, `transport_kinds`,
`expect_unreachable`, `layers`, `golden`, `max_paths` (the path limit of one call, 30 by default, the server accepts up
to 100; a pair from a tenant segment crosses the whole pod, so it asks for 100) and `circuit_types` (the circuit types
on the shortest chains of a pair without a waypoint: carriers in L1, session circuits in L3; an empty list means none).

The three sites of the sample (DC, colocation, office): the pairs between them and to INTERNET are in `pairs` (the ends
are the segment `c900-web-p`, the cage edge `par-colo1-edge-01`, the office edge `bcn-office-edge-01`,
`TopologyInternet:INTERNET`; to INTERNET only `layers: [l1]`, because INTERNET does not route). Two more case keys:
`sites` (every site reaches INTERNET in L1 through its own access carrier and the uplink of an ISP router:
`chain_rows.site_internet_rows`) and `layout` (a data read: the cloud and SaaS carriers start in the cage, the dark
fibres join the DC and the cage, the tunnels end on the office edge, the cage edge reaches the Fabric port through a
panel jack: `walks.layout_rows`). The server returns the equal-cost path set up to 100: a pair whose set is larger has
no complete result, so the data keeps the set small (for example a DCI session is only an EVPN peering between the
overlay processes).

## A new case

Add an entry to `cases.yml`: the schema directory, the data directories (in order), the `src`/`dst` pairs with their
expectations (`through`, `far_end_kinds`). The same mechanism can test the main topology of the project.

## Limits

- Sample data already present on `main` (for example loaded by hand) is inherited by the branch: the load is skipped
  (a second load would duplicate the nested `segment_deployments`), and the test says so plainly. A full test of loading
  the data files needs a stack without that data: `--rebuild`.
- A schema refusal caused by instances in other branches (a removed kind) needs a rebuilt stack: `--rebuild`.
- It does not run generators, transforms or checks: it verifies the model and the hand-written data.
- The schema is loaded onto the branch, so the `main` of the instance may hold an older schema; the assertions concern
  the branch.
