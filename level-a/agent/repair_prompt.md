You repair the adapter between a panel recall agent and a distributor's partner order system. The order system changed its API, and the agent's last call to it failed. Your proposal is checked before anything uses it: the new adapter must read, from the live order system, a shipped count the agent's ledger already holds for one account. A wrong mapping fails that check and is discarded, and the order system stays paused until a correct one arrives. So map what the documents say, not what seems likely.

The agent's code speaks canonical fields, defined in `<canonical_fields>`. An adapter tells it where each canonical field lives in the order system's JSON, as a dot path ("inventory.available" for a nested field). You get:

- `<failure>`: the one-line failure the agent's code detected (a 410, a 422, or a missing field).
- `<error_body>`: the order system's error response, verbatim. It may name the new API version.
- `<old_adapter>`: the adapter that worked before: its endpoints, mapping, hold states and fixed request fields.
- `<openapi>`: the order system's openapi.json as it is now. Operations marked `deprecated` are the old version.

Propose the adapter for the current API:

- `paths`: the endpoint for each role, written `METHOD /path` exactly as openapi.json lists it. `place_hold` creates a hold, `list_holds` lists the holds on a batch, `list_accounts` lists the accounts that received a batch. If a new hold has no effect until a second call confirms it, give that call as `confirm_hold` (keep its path parameter, such as `{hold_id}`); otherwise `confirm_hold` is null.
- `mapping`: every canonical field, mapped to the field of the new schemas that carries the same meaning. `batch` and `account_scope` are the field names in the `place_hold` request body, and `batch` is also the query parameter of both lists. `hold_id`, `batch`, `account_scope` and `status` come from the `list_holds` items; `account`, `name`, `phone`, `qty_shipped` and `qty_in_stock` from the `list_accounts` items. Use only fields that appear in openapi.json. Two canonical fields never share an order system field. Quantities are integers: `qty_in_stock` is stock still available for sale, not stock set aside.
- `status_in_force`: the hold states that mean the hold is in force. `status_pending`: states that mean it exists but is not yet in force (waiting for confirmation). Use the exact enum values from openapi.json.
- `defaults`: request fields of `place_hold` that the schema requires but no canonical field covers, each with the fixed value to send. For a reason or purpose field, choose the value that means a product recall.
- `rationale`: one or two sentences on what changed.

If openapi.json does not let you determine a field, still give your best-supported reading and say in `rationale` which field is uncertain; the ledger check will decide. Respond with the JSON object only.
