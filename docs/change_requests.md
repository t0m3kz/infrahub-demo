# Change requests

*Paperwork for the change advisory board, written by the one participant who actually read the diff.*

A **change request** (`ChangeRequest`) is the ServiceNow-style record of one Proposed Change. It describes the change
in the terms a change advisory board reads: what changes, how risky it is, how it is rolled out and rolled back, how it
is tested, and who is notified. Almost all of it is derived from the Proposed Change itself, so the record cannot
drift from the change it describes.

A change request is **opt-in**: a Proposed Change without one is not gated and merges as before.

## Create one

You do not. Infrahub does it. When the **last artifact validator** of an open Proposed Change completes, the generator
`open_change_request` (a rule on `CoreArtifactValidator`, group `artifact_validators`) acts once, in one of two ways:

- no change request exists: it creates `CHG-<proposed change name>` on the PC's source branch, in the group
  `change_requests`, which starts `add_change_request`;
- one exists: it re-runs `add_change_request` on it, so the artifact diff it describes is the complete one.

An earlier validator that completes while others still run does nothing. A change request derived before the
artifacts exist lists no devices and the gate warns that it is stale (CR5 below); creating it only after the last
artifact is how it avoids being confident and empty. No outside service is needed for any of this.

The change request lives on the Proposed Change's **source branch**, so reviewers see it in the PC's diff, and it
merges into `main` with the change as its history. Creating one by hand still works, if you enjoy that sort of thing:

| Field | Value |
| --- | --- |
| `name` | `CHG-<proposed change name>` (unique) |
| `proposed_change` | the Proposed Change (one change request per PC) |
| `member of groups` | `change_requests` |
| `justification` | why the change is needed (yours to write) |

Joining the `change_requests` group starts `add_change_request`, which fills in everything else (the "why" stays
yours: no generator has yet been brave enough to invent a business justification).

## Run it again

Nothing to do after the pipeline: the last artifact validator re-derives. To derive on demand anyway (for example after
a push that changed nothing the pipeline would notice), use **Run generator** on `add_change_request`, on the source
branch.

## The thread on the Proposed Change

The Infrahub UI shows a Proposed Change only through its fixed pages (the create form, the PC page and the edit
drawer), and none of them has room for a change request's fields. So `add_change_request` also keeps a conversation
thread on the PC, labelled "Change request", with one comment (`templates/change/pc_comment.j2`):

- a link to the change request;
- the change type, the effective risk (after the override) with its score, and the number of devices;
- the window, in Europe/Warsaw, or "not planned";
- the ticket (`external_id`), or "not filed yet".

The comment is rewritten when the derivation changes it and left alone when it does not, so the thread does not
collect a diary of identical updates.

## What is filled in

The generator reads only what the Proposed Change holds, plus the role, pair and BGP sessions of the affected devices:

- the PC's **data diff**: counts, and the values of the changed segments, firewall rules, cables, circuits, footprints
  and applications;
- the PC's **artifact diff**: every device configuration with its old and new checksum.

The text fields are rendered from the templates in `templates/change/`, so nobody types "low risk, tested in lab"
from memory ever again.

| Field | Derived from |
| --- | --- |
| `short_description`, `description` | A summary line, then the devices by site and role, the links, the firewall rules and the network segments the change touches, then the PC's own description |
| `affected_devices`, `affected_customers` | The devices whose configuration changes; the customers who own the changed segments and footprints |
| `risk`, `risk_score` | Scored from the device roles touched (spines, border leaves, edges, firewalls weigh most), removed objects, routing changes, production, the number of customers and devices: low below 20, moderate below 50, high above |
| `change_type` | `emergency` for a `hotfix/` branch; `standard` when only standard kinds change (segments, application components, rules, prefixes, addresses), nothing is removed and the risk is low; else `normal` |
| `impact` | 1 to 3, from production and the customers affected |
| `implementation_plan` | One step per device, least critical role first; for an MLAG or HA pair one member, then a check, then its peer; each step names the configuration and its checksums |
| `backout_plan` | Per device, the configuration it returns to, plus a warning when the change removes objects (a configuration rollback does not restore their allocations) |
| `test_plan` | Per device, the expected state: the BGP sessions the model expects, and that the running configuration's checksum equals the artifact's |
| `communication_plan` | The customers to notify |
| `planned_start`, `planned_end` | A normal change goes into the next maintenance window (Thursday 22:00, Europe/Warsaw), sized at 10 minutes per device, at least 30; a standard or emergency change starts now (a start already due is kept) |
| `conflicts` | The other open Proposed Changes whose change request lists the same devices |

The window is fully derived. Edit `planned_start` or `planned_end` by hand and the next derivation overwrites it; the
calendar is not a negotiation. To move a change, change what it is, or raise `risk_override` for the right reasons.

The generator never writes the fields that belong to people:

- `justification`;
- `risk_override` and `risk_override_reason`, to raise or lower the derived risk with a reason;
- `external_id`, the ticket number in ServiceNow;
- `requested_by`.

Somebody still has to sign it, after all.

## The gate

The check `validate_change_request` runs in the PC's pipeline. It judges a Proposed Change only when it has a change
request:

| Rule | Severity | Fails when |
| --- | --- | --- |
| CR1 | error | the PC has more than one change request |
| CR2 | error | `short_description`, `implementation_plan`, `backout_plan` or `test_plan` is empty, or there is no justification and the PC has no description |
| CR3 | error | a high risk (after the override) or an emergency change has no planned window |
| CR4 | error | `risk_override` is lower than the derived risk without a `risk_override_reason` |
| CR5 | warning | the change request was derived from another artifact diff than the current one: run `add_change_request` again |

A failing check refuses the merge, the same way any other check does. Escalating to a vice president does not change
its mind.

## ServiceNow

The transform `change_request_servicenow` renders a change request as the body of a ServiceNow `change_request`
record. It uses ServiceNow's field names, with the affected devices as `cmdb_ci`:

```bash
curl -H "X-INFRAHUB-KEY: $INFRAHUB_API_TOKEN" \
  "$INFRAHUB_ADDRESS/api/transform/python/change_request_servicenow?branch=<source branch>&name=CHG-<pc name>"
```

The transform is not an artifact: it is rendered on demand, so it always reflects the change request as it is,
which is more than most tickets can say six months in.

## What is not automated

The change request itself needs no help any more. Two things are still outside the repository: raising the ServiceNow
ticket (and writing its number to `external_id`) and deploying the change. A service that listens to Infrahub's events
does both, or a person does, which is the classic enterprise integration of a person and a calendar reminder.
