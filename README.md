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

`recommendations` queries every department saved in `departments.json`. It skips departments without the `multipoint.inspection.fetch` scope and reports a failure in one department without stopping the others.

## How credentials are kept out of output

- They go straight into `httpx.BasicAuth`. Nothing builds, stores or prints the header.
- Errors report the method, path and status only, never the request, headers or response body.
- The VIN goes in the request body, and the credentials are never in a URL or a CLI argument.
- `uv run pytest` checks that the password, username and Base64 token never show up in errors, `repr`, logs or CLI output.

## API references

- Auth and access check: https://docs.mykaarma.com/authentication-and-authorization
- MPI filter (by VIN, per department): https://docs.mykaarma.com/our-products/inspection/service-cart/how-to-filter-mpis
- MPI structure (sections → checks → recommendations): https://docs.mykaarma.com/our-products/inspection/service-cart/how-to-get-multipoint-inspection
