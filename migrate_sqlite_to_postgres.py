"""Utility script to migrate the local SQLite database (fishtrade.db) into the Railway PostgreSQL instance.

Run it inside the container (or locally with proper env vars) after the tables have been created:
    python migrate_sqlite_to_postgres.py
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import the shared ORM models and settings
from app import config, models

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Path to the local SQLite file (relative to project root)
SQLITE_URL = "sqlite:///./data/fishtrade.db"

# PostgreSQL URL is provided by Railway via env var DATABASE_URL (or DATABASE_PRIVATE_URL)
# The config module already validates and normalises it.
POSTGRES_URL = config.settings.validated_database_url

if not POSTGRES_URL:
    raise RuntimeError("Validated PostgreSQL URL is empty – ensure DATABASE_URL is set in Railway variables.")

# ---------------------------------------------------------------------------
# Engines & Sessions
# ---------------------------------------------------------------------------
sqlite_engine = create_engine(SQLITE_URL)
postgres_engine = create_engine(POSTGRES_URL)

SqliteSession = sessionmaker(bind=sqlite_engine)
PostgresSession = sessionmaker(bind=postgres_engine)

# ---------------------------------------------------------------------------
# Create tables in PostgreSQL (if they do not exist)
# ---------------------------------------------------------------------------
print("Creating tables in PostgreSQL (if missing)...")
models.Base.metadata.create_all(postgres_engine)

# ---------------------------------------------------------------------------
# Helper: copy rows from SQLite to Postgres for a given model
# ---------------------------------------------------------------------------
def copy_model(model):
    src_session = SqliteSession()
    dst_session = PostgresSession()
    try:
        rows = src_session.query(model).all()
        print(f"Migrating {len(rows)} rows of {model.__name__}...")
        for row in rows:
            # Detach the instance from the SQLite session so it can be added to Postgres
            src_session.expunge(row)
            # Reset identity map for the target session
            dst_session.merge(row)
        dst_session.commit()
    except Exception as e:
        dst_session.rollback()
        raise
    finally:
        src_session.close()
        dst_session.close()

# ---------------------------------------------------------------------------
# List of models to migrate (order matters for foreign‑key constraints)
# ---------------------------------------------------------------------------
models_to_migrate = [
    models.Tenant,
    models.Branch,
    models.User,
    models.AuditEvent,
    # Add any additional tables that exist in your SQLite DB but are not listed here.
]

for m in models_to_migrate:
    copy_model(m)

print("✅ Migration completed.")
