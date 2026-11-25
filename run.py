"""Production entrypoint — reads PORT from env (Railway-safe, no shell expansion)."""

import os
import sys
from pathlib import Path

import uvicorn


def _find_sqlite_file() -> Path | None:
    """Look for a bundled SQLite file without assuming how deep this file sits."""
    explicit = os.environ.get("SQLITE_PATH", "").strip()
    candidates = [Path(explicit)] if explicit else []
    here = Path(__file__).resolve().parent
    candidates += [here / "data" / "fishtrade.db", here.parent / "data" / "fishtrade.db"]
    return next((p for p in candidates if p.is_file()), None)


def init_db() -> None:
    """Create all tables in the configured database (Postgres on Railway).

    The legacy SQLite -> Postgres import is OPT-IN: set RUN_SQLITE_IMPORT=1.
    """
    from sqlalchemy import create_engine

    from app import config, models

    url = config.settings.validated_database_url
    if url.startswith("sqlite"):
        print("[WARN] Using SQLite (development only).")
    engine = create_engine(url, pool_pre_ping=True)
    models.Base.metadata.create_all(engine)
    print("✅ Database tables ensured.")

    if os.environ.get("RUN_SQLITE_IMPORT") != "1":
        return

    sqlite_path = _find_sqlite_file()
    if sqlite_path is None:
        print("ℹ️ RUN_SQLITE_IMPORT=1 but no SQLite file found; skipping.")
        return

    print(f"🔄 Importing legacy SQLite data from {sqlite_path}…")
    try:
        from migrate_sqlite_to_postgres import copy_model, models_to_migrate

        for model in models_to_migrate:
            copy_model(model)
        print("✅ SQLite import completed.")
    except Exception as exc:  # never block startup because of the legacy import
        print(f"⚠️ SQLite import failed: {exc}")


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))

    try:
        init_db()  # importing app.config also validates DATABASE_URL
    except RuntimeError as exc:  # missing / unresolved DATABASE_URL, SQLite in prod
        print(f"\n[FATAL] {exc}\n", file=sys.stderr)
        sys.exit(1)

    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()