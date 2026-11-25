"""Production entrypoint — reads PORT from env (Railway-safe, no shell expansion)."""

import os
import sys

import uvicorn


def init_db() -> None:
    """Create all tables in the target PostgreSQL DB.
    If a local SQLite file (data/fishtrade.db) is present, also migrate its data.
    """
    from pathlib import Path
    from app import config, models
    from sqlalchemy import create_engine

    # Create tables (no-op if they already exist)
    pg_engine = create_engine(config.settings.validated_database_url)
    models.Base.metadata.create_all(pg_engine)
    print("✅ PostgreSQL tables ensured.")

    # If a local SQLite DB is bundled (e.g., for fresh dev deployments), migrate its data.
    sqlite_path = Path(__file__).parents[2] / "data" / "fishtrade.db"
    if sqlite_path.is_file():
        print(f"🔄 Detected local SQLite DB at {sqlite_path}, running data migration…")
        try:
            # Import the migration helper (it will copy data if the DB exists)
            from migrate_sqlite_to_postgres import copy_model, models_to_migrate
            # Ensure migration script knows the correct SQLite URL (it already points to ./data/fishtrade.db)
            for model in models_to_migrate:
                copy_model(model)
            print("✅ Data migration from SQLite completed.")
        except Exception as exc:
            print(f"⚠️ Migration failed: {exc}")
    else:
        print("ℹ️ No local SQLite DB found – only table creation performed.")

def main() -> None:
    port = int(os.environ.get("PORT", "8000"))

    # Fail fast with a readable message before SQLAlchemy import errors
    db_url = (
        os.environ.get("DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_PRIVATE_URL", "").strip()
        or os.environ.get("POSTGRES_URL", "").strip()
        or os.environ.get("POSTGRES_PRIVATE_URL", "").strip()
    )
    env = os.environ.get("ENVIRONMENT", "development").lower()
    on_railway = bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("RAILWAY_SERVICE_ID")
    )
    if on_railway and not db_url:
        print(
            "\n[FATAL] Running on Railway but no Postgres URL is set.\n"
            "The API would fall back to ephemeral SQLite — tenant signups would NOT appear in Railway Postgres.\n"
            "Fix: dukaplusbackend → Variables → delete bad DATABASE_URL → Add Reference →\n"
            "Postgres → DATABASE_PRIVATE_URL → name it DATABASE_URL. Set ENVIRONMENT=production, redeploy.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    if env in {"production", "prod"} and not db_url:
        print(
            "\n[FATAL] ENVIRONMENT=production but DATABASE_URL is missing.\n"
            "Fix: open dukaplusbackend service → Variables → Add Reference →\n"
            "Postgres service → DATABASE_PRIVATE_URL → variable name DATABASE_URL\n"
            "Delete any empty DATABASE_URL entry first, then redeploy.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # -------------------------------------------------
    # Run DB initialization / migration before starting the server
    # -------------------------------------------------
    init_db()

    uvicorn.run("app.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
