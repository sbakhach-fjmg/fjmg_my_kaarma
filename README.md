# mykaarma

Small CLI for the myKaarma API (Basic auth). It lists the departments your credentials can reach, pulls recommended work (multipoint-inspection recommendations) for a VIN, and loads that work for every used car in stock into SQL Server, so you can see per VIN how much is recommended, approved, declined or pending, and how severe it is.

## Setup

```
uv sync
copy .env.example .env
notepad .env
```

Fill in `MYKAARMA_USERNAME` (credential part 1) and `MYKAARMA_PASSWORD` (part 2) **in the editor**. Don't use `echo … >> .env`, because PowerShell keeps that in its history. Optionally restrict the file to your account:

```
icacls .env /inheritance:r /grant:r "%USERNAME%:F"
```

`.env` is git-ignored. Never paste the values into chat or a ticket. `.claude/settings.local.json` also denies Claude Code from reading `.env`.

## Use

```
uv run mykaarma check            # credentials set/missing, then each department's UUID and scopes
uv run mykaarma check --save     # also writes departments.json (git-ignored)
uv run mykaarma recommendations WDDZF4JB1HA123456
uv run mykaarma recommendations WDDZF4JB1HA123456 --dept newport
```

`recommendations` queries each store's Service department from `departments.json` (12 today). `--all` queries all 58, and `--dept` narrows to matching departments. It skips departments without the `multipoint.inspection.fetch` scope and reports a failure in one department without stopping the others.

## Load used inventory into SQL Server

Results land in `dbo.src_mykaarma__*` tables in the database that holds `Current_Inventory`. The stakeholder's view is `dbo.vw_mykaarma__vin_recommendation_summary`: one row per VIN ever checked (`where in_inventory = 1` for current stock), with the recommended, approved, declined and pending amounts and the severity counts from the VIN's latest inspection that has recommendations (or its latest inspection if none do). `last_inspection_date` shows when a newer, empty inspection exists.

1. Add `MSSQL_CONNECTION` to `.env`, using the template in `.env.example` (Windows integrated auth, no password).
2. Run `sql/src_mykaarma.sql` once in SSMS. It's re-runnable (it also adds or widens columns on tables created by an older version), and the loader never creates tables. The loader stops with a message naming anything missing.
3. Run the loader:

```
uv run mykaarma load --limit 25      # first N used VINs; doesn't update which VINs are in inventory
uv run mykaarma load                 # all used inventory; flags VINs that left inventory
uv run mykaarma load --workers 8     # parallel API workers (default 6)
uv run mykaarma load --skip-fresh 0  # recheck everything (default skips VINs checked in the last 20 hours)
```

A full run covers about 1,600 VINs in under 5 minutes.

**Which VINs.** `sql/current_inventory_used.sql` returns every used car in stock in `Current_Inventory`: `[Veh Status] = 'P'`, `[New/Used] = 'U'`, a 17-character VIN, and not at FRMBN, FRPOR, AUDFR or WCPOR. A VIN with two stock records keeps the most recently received one.

**Which store.** Each VIN is checked only at its current store's Service department. `stores.json` (committed) maps the inventory `StoreID` to that department's myKaarma UUID. To add a store, run `uv run mykaarma check` to find its Service department UUID and add an entry. A VIN at a store missing from `stores.json` is recorded with an error and no API calls, and the loader lists unmapped store codes when it starts.

**Failures and resuming.** Each VIN is written in its own transaction. If a department call fails (after 3 retries on 429, 5xx and timeouts) or the database rejects a row, the VIN's previous rows stay, its `error` column is set, and the next run retries it. A stopped run picks up where it left off, because VINs checked without error recently are skipped. The run exits with status 1 if any VIN errored.

**History.** Nothing is deleted. myKaarma returns every inspection a VIN has had, so past ROs stay as each check replaces the VIN's inspection rows. When a full run no longer finds a VIN in used inventory, the VIN keeps its last known state with `in_inventory = 0` and isn't rechecked. If it comes back into stock, it's checked again. `first_seen` is the first check, and `last_seen` is the last full run that found it in stock. What isn't kept is how a line changed over time: if a line goes from pending to approved, only "approved" is stored.

**Tables:**

| Table | One row per |
|---|---|
| `src_mykaarma__vin_lookups` | VIN ever checked: when, at which store, how many inspections, `error` (null = OK), and `in_inventory`, `first_seen`, `last_seen` |
| `src_mykaarma__inspections` | inspection (one per RO), with myKaarma's own totals (`mk_*`) and the raw JSON minus customer identifiers |
| `src_mykaarma__recommendations` | recommended line: check, description, severity, status, decision, amount, op code |
| `src_mykaarma__inspection_associates` | employee on an inspection: advisor, technician or assignee |

**How the summary decides things.**
- *Decision:* `approvalState` first, then `status`. Anything starting `approv` is approved, `declin` is declined, and everything else is pending (`incomplete`, `complete`, `requested`, `not needed`).
- *Severity:* the worst colour on the line's check (red > yellow > green).
- *Amounts:* sums of the line items. Unpriced lines count as 0 and are counted in `unpriced_count`. myKaarma's `mk_recommended_total` and `mk_approved_total` are kept for comparison, but don't always match the lines. Approved in particular often includes other work on the RO.
- *Pending on old ROs* usually means nobody recorded a decision, not work still waiting. Use `order_date` to tell.

Customer identifiers are never stored. Employee identifiers are.

## How credentials are kept out of output

- They go straight into `httpx.BasicAuth`. Nothing builds, stores or prints the header.
- Errors report the method, path and status only, never the request, headers or response body.
- The VIN goes in the request body, and the credentials are never in a URL or a CLI argument.
- The loader's progress lines carry counts only. Stored errors carry store names, HTTP status codes or the database driver's message, and never credentials.
- `uv run pytest` checks that the password, username and Base64 token never show up in errors, `repr`, logs or CLI output.

## API references

- Auth and access check: https://docs.mykaarma.com/authentication-and-authorization
- MPI filter (by VIN, per department): https://docs.mykaarma.com/our-products/inspection/service-cart/how-to-filter-mpis
- MPI structure (sections → checks → recommendations): https://docs.mykaarma.com/our-products/inspection/service-cart/how-to-get-multipoint-inspection
