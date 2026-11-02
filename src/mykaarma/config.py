"""Settings. Reads .env in the project root (KEY=value lines) and the environment.

The credentials are only ever handed to the HTTP client; nothing here prints or formats them.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPARTMENTS_FILE = ROOT / "departments.json"


def load_env(path: Path = ROOT / ".env") -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def credentials() -> tuple[str, str]:
    return os.environ.get("MYKAARMA_USERNAME", ""), os.environ.get("MYKAARMA_PASSWORD", "")


def base_url() -> str:
    return os.environ.get("MYKAARMA_BASE_URL") or "https://api.mykaarma.com"


def departments(path: Path | None = None) -> list[dict]:
    """Departments saved by `mykaarma check --save`. Not secret, but git-ignored (internal dealer data)."""
    path = path or DEPARTMENTS_FILE
    return json.loads(path.read_text()) if path.exists() else []


def save_departments(rows: list[dict], path: Path | None = None) -> None:
    path = path or DEPARTMENTS_FILE
    path.write_text(json.dumps(rows, indent=2) + "\n")
