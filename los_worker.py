"""One bounded durable mail dispatcher, owned by the single web process.

At-least-once delivery: a crash after SMTP acceptance can cause a duplicate.
Only fixed message kinds and IDs are stored. No arbitrary commands or payloads.
"""
import fcntl
import sqlite3
import threading
import time
from flask import abort

_stop = threading.Event()
_thread = None
_lock = None

def enqueue(con, aid, art):
    if art not in ("eingang", "zuteilung"):
        raise ValueError("Unknown mail kind")
    if con.execute("SELECT count(*) FROM mail_outbox WHERE status IN ('pending','sending')").fetchone()[0] >= 200:
        abort(503, "Mail-Warteschlange belegt. Bitte später erneut versuchen.")
    con.execute("INSERT INTO mail_outbox(anmeldung_id,art) VALUES(?,?)", (aid, art))

def dispatch_once():
    from app import app, db, mail_senden
    with app.app_context():
        con = db()
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT id,anmeldung_id,art,attempts FROM mail_outbox WHERE status='pending' AND available_at<=? ORDER BY id LIMIT 1",
                          (time.time(),)).fetchone()
        if row is None:
            con.rollback()
            return False
        con.execute("UPDATE mail_outbox SET status='sending',updated_at=? WHERE id=?", (time.time(), row["id"]))
        con.commit()
        try:
            error = mail_senden(row["anmeldung_id"], row["art"])
        except Exception:
            error = "delivery-failed"  # never retain exception payloads containing message data
        attempts = row["attempts"] + 1
        state = "sent" if error is None else ("failed" if attempts >= 5 else "pending")
        con.execute("UPDATE mail_outbox SET status=?,attempts=?,available_at=?,updated_at=? WHERE id=?",
                    (state, attempts, time.time() + min(300, 2 ** attempts), time.time(), row["id"]))
        con.commit()
        return True

def _loop():
    from app import DB_PATH, GEOJSON
    from los_runtime import ready
    while not _stop.is_set():
        try:
            if ready(DB_PATH, GEOJSON) and dispatch_once():
                continue
        except Exception:
            pass  # bounded retry; no arbitrary app/SMTP payload in logs
        _stop.wait(.25)

def start():
    global _thread, _lock
    from app import DB_PATH
    if _thread is not None:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _lock = (DB_PATH.parent / "mail-worker.lock").open("a")
    fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Only this lock owner may recover an interrupted in-flight item.
    if DB_PATH.is_file():
        with sqlite3.connect(DB_PATH) as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='mail_outbox'").fetchone():
                con.execute("UPDATE mail_outbox SET status='pending' WHERE status='sending'")
                con.commit()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="fixed-mail-dispatcher", daemon=True)
    _thread.start()

def stop():
    global _thread, _lock
    _stop.set()
    if _thread:
        _thread.join(timeout=15)
        if _thread.is_alive():
            return  # process owns lock until exit; restart recovers pending/sending items
        _thread = None
    if _lock:
        _lock.close()
        _lock = None
