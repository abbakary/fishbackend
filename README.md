# FishTrade FastAPI backend

This service is the first shared persistence/authentication boundary for the React and Flutter clients.
It provides tenant-isolated accounts, branch access control, optimistic-versioned branch snapshots,
and generic versioned CRUD for the branch collections used by the clients.
SQLite is the selected development database; set `DATABASE_URL` to a managed PostgreSQL URL for a
multi-process/production deployment. Do not expose the development database file over a network share.

## Local setup

1. Create a Python virtual environment in this folder and install `requirements.txt`.
2. Copy `.env.example` to `.env`; replace `JWT_SECRET` with a random secret.
3. Run `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` from this folder.
4. Open `/docs` for the development OpenAPI interface and `/health` for the health check.

`docker compose up --build` runs the API with a persistent SQLite volume. Registration creates a fresh
tenant with an empty branch; the server supplies the structural starter state (warehouse list,
sequences, one active season, two empty fuel tanks, empty collections) so both clients render a usable
branch immediately and never fall back to built-in sample data.

## API outline

- `POST /api/v1/auth/register` creates an owner, tenant, and branches (starter state auto-applied).
- `POST /api/v1/auth/login` issues a signed bearer token.
- `GET /api/v1/auth/me` returns tenant, user, and authorized branch metadata.
- `GET /api/v1/bootstrap` returns the signed-in user's accessible branch snapshots and staff list.
- `POST /api/v1/branches` and `GET/POST/PATCH /api/v1/staff` provide manager-only tenant administration
  with plan limits, including profile fields, permissions, module keys, and password resets.
- `POST /api/v1/branches/{branch_id}/initialize` applies the starter template to a legacy branch and is
  idempotent once a branch has `seq` and `seasons`.
- `GET/PUT /api/v1/branches/{branch_id}/state` loads/saves a branch snapshot. PUT requires
  `expected_version`; a stale write returns HTTP 409 instead of silently overwriting another device.
- `GET/POST /api/v1/branches/{branch_id}/collections/{name}` and `PATCH/DELETE` on an item path
  provide version-checked storage for the allow-listed web/mobile collection names. Writes generate audit events.
- `GET /api/v1/branches/{branch_id}/audit` lists server-side audit events.
- Transactional domain commands: `POST .../commands/purchases`, `.../commands/batches/{id}/inspect`,
  `.../commands/sales`, `.../commands/supplier-payments`, `.../commands/fuel-issues`,
  `.../commands/gear-issues` (role-checked, stock-checked, version-checked).
- Purchases support paid, partial, and unpaid settlements. `paid_now` is validated against
  `kg * price_per_kg` minus `fuel_deducted`/`maint_deducted`; each batch stores `ded`, `paid`,
  `bal`, and `payStatus`, purchase deductions post fisherman-ledger credits and a settlement record,
  and purchase payments carry the `pur` reference. Supplier payments are rejected when they exceed
  the supplier's outstanding balance.

**Integration status:** both frontends now authenticate against this API. The web app restores its
session from the bearer token (sessionStorage), hydrates branch snapshots via `/bootstrap`, syncs edits
with versioned PUTs, and routes staff creation/updates through `/api/v1/staff` when signed in remotely;
the opt-in local demo tab remains for offline exploration. Flutter logs in only against the API,
provisions branches through server starter state, and keeps an offline dirty-flag queue for branch
writes. Local-only staff PIN authentication and seeded sample branches have been removed from both
clients; `seed_data.dart` is no longer referenced. Before production, add Alembic migrations
(a SQLite profile-column auto-patch runs at startup in the meantime), rate limiting, backups, TLS,
secure client token storage, and production secrets.