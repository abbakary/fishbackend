"""Utility to list tables/columns from the local SQLite DB (fishtrade.db).
Run with: python inspect_sqlite.py
"""
import sqlite3, json, sys

DB_PATH = r"C:/Users/Administrator/acquarefish/backend/data/fishtrade.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Get list of tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
tables = [row[0] for row in cur.fetchall()]

schema = {}
for tbl in tables:
    cur.execute(f"PRAGMA table_info('{tbl}')")
    cols = [{"cid": c[0], "name": c[1], "type": c[2], "notnull": bool(c[3]), "dflt_value": c[4], "pk": bool(c[5])} for c in cur.fetchall()]
    schema[tbl] = cols

print(json.dumps(schema, indent=2))

conn.close()
