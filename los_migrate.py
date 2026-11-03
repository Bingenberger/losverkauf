"""Explicit SQLite job. Application writers must be paused by the caller."""
import fcntl
from pathlib import Path
import sqlite3
from app import DB_PATH, SCHEMA, migrieren
from los_runtime import SCHEMA_VERSION

OUTBOX = """CREATE TABLE IF NOT EXISTS mail_outbox (
id INTEGER PRIMARY KEY, anmeldung_id INTEGER NOT NULL REFERENCES anmeldungen(id) ON DELETE CASCADE,
art TEXT NOT NULL CHECK(art IN ('eingang','zuteilung')),
status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','failed')),
attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
updated_at REAL NOT NULL DEFAULT 0
)"""

def migrate():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DB_PATH.parent / "migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(DB_PATH, timeout=10) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise ValueError("Unsupported schema version")
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables - {"anmeldungen","personen","mail_log","bezirk_status","wuensche","mail_outbox","sqlite_sequence"}:
                    raise ValueError("Unexpected tables; reviewed adoption required")
                if "wuensche" in tables:
                    columns = {r[1] for r in con.execute("PRAGMA table_info(wuensche)")}
                    required = {"id","bezirk_id","edit_token","created_at","name","klasse"}
                    allowed = required | {"team","kontakt","telefon","email","von_admin"}
                    if not required <= columns or not columns <= allowed:
                        raise ValueError("Unknown legacy fields; refusing lossy conversion")
                # A partial/unknown shape is never silently adopted by CREATE IF NOT EXISTS.
                expected = {
                    "anmeldungen":{"id","bezirk_id","status","edit_token","created_at","bestaetigt_at","von_admin"},
                    "personen":{"id","anmeldung_id","pos","name","klasse","telefon","email"},
                    "mail_log":{"id","anmeldung_id","art","empfaenger","ok","fehler","zeit"},
                    "bezirk_status":{"bezirk_id","gesperrt","max_override"},
                    "mail_outbox":{"id","anmeldung_id","art","status","attempts","available_at","updated_at"},
                }
                for table, columns in expected.items():
                    if table in tables and {r[1] for r in con.execute("PRAGMA table_info(" + table + ")")} != columns:
                        raise ValueError("Unexpected table shape")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        con.execute(statement)
                migrieren(con)
                con.execute(OUTBOX)
                if con.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Foreign key violation")
                con.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        DB_PATH.chmod(0o600)

if __name__ == "__main__":
    try:
        migrate()
        print("Explicit migration verified.")
    except Exception:
        raise SystemExit("Migration refused or failed; inspect the private data and release compatibility.")
