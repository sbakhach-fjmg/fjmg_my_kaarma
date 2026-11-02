# mykaarma

Small CLI for the myKaarma API (Basic auth). It lists the departments your credentials can reach, then pulls recommended work (multipoint-inspection recommendations) for a VIN across those departments.

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

Results land in `dbo.src_mykaarma__*` tables in the database that holds `Current_Inventory`. The stakeholder's view is `dbo.vw_mykaarma__vin_recommendation_summary`: one row per VIN, with the recommended, approved, declined and pending amounts and the severity counts from the VIN's latest inspection.

1. Add `MSSQL_CONNECTION` to `.env`, using the template in `.env.example` (Windows integrated auth, no password).
2. Run `sql/src_mykaarma.sql` once in SSMS. It's re-runnable, and the loader never creates tables.
3. Run the loader:

```
uv run mykaarma load --limit 25      # first N used VINs; nothing is pruned
uv run mykaarma load                 # all used inventory; prunes VINs that left inventory
```

Each VIN is checked against the 12 Service departments and committed in its own transaction. A department error keeps the VIN's previous rows and records `error`, and the next run retries it. VINs checked without error in the last 20 hours are skipped (`--skip-fresh 0` rechecks everything), so a stopped run picks up where it left off. Customer identifiers are never stored. Employee identifiers are.

## How credentials are kept out of output

- They go straight into `httpx.BasicAuth`. Nothing builds, stores or prints the header.
- Errors report the method, path and status only, never the request, headers or response body.
- The VIN goes in the request body, and the credentials are never in a URL or a CLI argument.
- `uv run pytest` checks that the password, username and Base64 token never show up in errors, `repr`, logs or CLI output.

## API references

- Auth and access check: https://docs.mykaarma.com/authentication-and-authorization
- MPI filter (by VIN, per department): https://docs.mykaarma.com/our-products/inspection/service-cart/how-to-filter-mpis
- MPI structure (sections → checks → recommendations): https://docs.mykaarma.com/our-products/inspection/service-cart/how-to-get-multipoint-inspection
