from __future__ import annotations

import os
import shutil
from pathlib import Path


def postgres_bin_dirs() -> list[Path]:
    roots = [
        Path("/Library/PostgreSQL"),
        Path("/usr/local/pgsql"),
        Path("/opt/homebrew/opt"),
        Path("/usr/lib/postgresql"),
    ]
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for candidate in root.rglob("bin"):
            if (candidate / "postgres").exists() and (candidate / "initdb").exists():
                found.append(candidate)
    return found


def ensure_postgres_on_path() -> bool:
    if shutil.which("postgres") and shutil.which("initdb"):
        return True
    for bin_dir in postgres_bin_dirs():
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        if shutil.which("postgres") and shutil.which("initdb"):
            return True
    return False


def resolve_test_database_url() -> str | None:
    env_url = os.getenv("TEST_DATABASE_URL")
    if env_url:
        return env_url

    if not ensure_postgres_on_path():
        return None

    try:
        import testing.postgresql  # noqa: F401
    except ImportError:
        return None

    return "__testing_postgresql__"
