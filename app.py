"""
Losverkauf St. Martin – Bezirkswahl auf der Karte.
Flask + SQLite, Frontend mit Leaflet/OpenStreetMap.

Umgebungsvariablen (siehe deploy/losverkauf.env.example):
  SECRET_KEY      Flask-Session-Schlüssel
  ELTERN_TOKEN    Token im Elternlink:  https://…/?t=<ELTERN_TOKEN>
  ADMIN_PASSWORD  Passwort für /admin
  DB_PATH         Pfad zur SQLite-Datei (Standard: data/losverkauf.db)
  TITEL           Anzeigename, z. B. "Losverkauf St. Martin 2026"
  SESSION_COOKIE_SECURE  1 = Session-Cookie nur über https (im Betrieb hinter nginx mit TLS)
  SMTP_…, MAIL_…  Mailversand, siehe mail.py
"""
import csv
from html import escape
import io
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from urllib.parse import quote
import los_runtime

import mail

BASE = Path(__file__).parent
app = Flask(__name__)
app.secret_key = los_runtime.required("SECRET_KEY")
BASE_URL = los_runtime.origin()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=BASE_URL.startswith("https://") or os.environ.get("SESSION_COOKIE_SECURE") == "1")
# Hinter nginx: Schema und Host aus den X-Forwarded-Headern übernehmen (Gunicorn lauscht nur auf 127.0.0.1)
_original_wsgi = app.wsgi_app
_trusted = los_runtime.trusted_ips()
_proxy_wsgi = ProxyFix(_original_wsgi, x_for=1, x_proto=1, x_host=0)
def trusted_wsgi(environ, start_response):
    target = _proxy_wsgi if environ.get("REMOTE_ADDR") in _trusted else _original_wsgi
    return target(environ, start_response)
app.wsgi_app = trusted_wsgi
ELTERN_TOKEN = los_runtime.required("ELTERN_TOKEN")
ADMIN_PASSWORD = los_runtime.value("ADMIN_PASSWORD")
ADMIN_PASSWORD_HASH = los_runtime.value("ADMIN_PASSWORD_HASH")
ADMIN_USERNAME = los_runtime.value("ADMIN_USERNAME")
DB_PATH = Path(os.environ.get("DB_PATH", BASE / "data" / "losverkauf.db"))
TITEL = os.environ.get("TITEL", "Losverkauf St. Martin")
GEOJSON = Path(os.environ.get("GEOJSON_PATH", BASE / "static" / "bezirke.geojson"))
TILE_URL = os.environ.get("TILE_URL", "")
if TILE_URL and (not TILE_URL.startswith("/") or TILE_URL.startswith("//") or "\\" in TILE_URL):
    raise RuntimeError("TILE_URL must use a configured same-origin tile service")
_pdf_slot = threading.BoundedSemaphore(1)
MAX_PERSONEN = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS anmeldungen (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bezirk_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'offen',   -- offen | bestaetigt
  edit_token TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  bestaetigt_at TEXT,
  von_admin INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS personen (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anmeldung_id INTEGER NOT NULL REFERENCES anmeldungen(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  name TEXT NOT NULL,
  klasse TEXT NOT NULL DEFAULT '',
  telefon TEXT DEFAULT '',
  email TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mail_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anmeldung_id INTEGER NOT NULL,
  art TEXT NOT NULL,                      -- eingang | zuteilung
  empfaenger TEXT NOT NULL,
  ok INTEGER NOT NULL,
  fehler TEXT DEFAULT '',
  zeit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bezirk_status (
  bezirk_id TEXT PRIMARY KEY,
  gesperrt INTEGER DEFAULT 0,
  max_override INTEGER
);
"""
TELEFON_RE = re.compile(r"^\+?[0-9][0-9 ()/.\-]{4,28}[0-9]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FELDER = (("name", 80), ("klasse", 10), ("telefon", 30), ("email", 120))


def jetzt() -> str:
    return datetime.now().isoformat(timespec="minutes")


@app.template_filter("datum")
def datum(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M") if iso else ""


# ---------- DB ----------
def migrieren(con):
    """Known legacy conversion, only inside the explicit migration transaction."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wuensche'").fetchone():
        return
    for r in map(dict, con.execute("SELECT * FROM wuensche").fetchall()):
        kontakt = r.get("kontakt") or ""
        telefon = r.get("telefon") or ("" if "@" in kontakt or kontakt == "–" else kontakt)
        email = r.get("email") or (kontakt if "@" in kontakt else "")
        aid = r["id"]
        con.execute("INSERT INTO anmeldungen (id,bezirk_id,edit_token,created_at,von_admin) VALUES (?,?,?,?,?)",
                    (aid,r["bezirk_id"],r["edit_token"],r["created_at"],r.get("von_admin") or 0))
        con.execute("INSERT INTO personen (anmeldung_id,pos,name,klasse,telefon,email) VALUES (?,1,?,?,?,?)",
                    (aid,r["name"],r["klasse"],telefon,email))
        if r.get("team"):
            con.execute("INSERT INTO personen (anmeldung_id,pos,name) VALUES (?,2,?)",(aid,r["team"]))
    con.execute("DROP TABLE wuensche")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def load_geojson():
    return los_runtime.geojson(GEOJSON)


def bezirke_mit_status():
    """Bezirke aus GeoJSON + Zähler/Status aus DB. Eine Anmeldung (1–3 Personen) belegt einen Platz."""
    gj = load_geojson()
    counts = {r["bezirk_id"]: r for r in db().execute(
        "SELECT bezirk_id, COUNT(*) n, SUM(status='bestaetigt') b FROM anmeldungen GROUP BY bezirk_id")}
    status = {r["bezirk_id"]: r for r in db().execute("SELECT * FROM bezirk_status")}
    for f in gj["features"]:
        p = f["properties"]
        st = status.get(p["id"])
        p["max_effektiv"] = (st["max_override"] if st and st["max_override"] is not None else p.get("max", 2))
        p["gesperrt"] = bool(st and st["gesperrt"])
        p["anzahl"] = counts[p["id"]]["n"] if p["id"] in counts else 0
        p["bestaetigt"] = counts[p["id"]]["b"] if p["id"] in counts else 0
        p["frei"] = max(0, p["max_effektiv"] - p["anzahl"])
        p["waehlbar"] = (not p["gesperrt"]) and p["frei"] > 0
    return gj


def bezirk_props(bid: str) -> dict:
    return next((f["properties"] for f in load_geojson()["features"] if f["properties"]["id"] == bid),
                {"id": bid, "name": bid, "strassen": []})


def anmeldungen_laden(where: str = "", args: tuple = ()) -> list[dict]:
    """Anmeldungen mit Personen und letztem Mailstatus je Art."""
    rows = [dict(r) for r in db().execute(f"SELECT * FROM anmeldungen {where} ORDER BY created_at, id", args)]
    if not rows:
        return []
    ids = {r["id"]: r for r in rows}
    marken = ",".join("?" * len(ids))
    for r in rows:
        r["personen"], r["mails"] = [], {}
    for p in db().execute(f"SELECT * FROM personen WHERE anmeldung_id IN ({marken}) ORDER BY pos", tuple(ids)):
        ids[p["anmeldung_id"]]["personen"].append(dict(p))
    for m in db().execute(f"SELECT * FROM mail_log WHERE anmeldung_id IN ({marken}) ORDER BY id", tuple(ids)):
        ids[m["anmeldung_id"]]["mails"][m["art"]] = dict(m)
    return rows


# ---------- Mail ----------
def mail_senden(aid: int, art: str) -> str | None:
    """Eingangs- oder Zuteilungsmail an alle Personen der Anmeldung; wird in mail_log protokolliert."""
    a = anmeldungen_laden("WHERE id=?", (aid,))
    if not a:
        return "Anmeldung nicht gefunden."
    a = a[0]
    empfaenger = list(dict.fromkeys(p["email"] for p in a["personen"] if p["email"]))
    if empfaenger:
        text = render_template(f"mail/{art}.txt", titel=TITEL, bezirk=bezirk_props(a["bezirk_id"]),
                               personen=a["personen"], anmeldung=a).lstrip()
        betreff, _, rumpf = text.partition("\n")
        fehler = mail.senden(empfaenger, betreff.removeprefix("Betreff:").strip(), rumpf.strip() + "\n",
                             DB_PATH.parent / "mails")
    else:
        fehler = "Keine E-Mail-Adresse hinterlegt."
    db().execute("INSERT INTO mail_log (anmeldung_id,art,empfaenger,ok,fehler,zeit) VALUES (?,?,?,?,?,?)",
                 (aid, art, ", ".join(empfaenger), 0 if fehler else 1, fehler or "", jetzt()))
    db().commit()
    return fehler


def mail_absprache(bid: str, auch_offene: bool) -> tuple[str | None, int]:
    """Absprache-Mail an alle Angemeldeten eines Bezirks – nur Namen und E-Mail-Adressen, keine Telefonnummern.

    Rückgabe: (Fehler oder None, Anzahl Empfänger)."""
    anmeldungen = [a for a in anmeldungen_laden("WHERE bezirk_id=?", (bid,))
                   if auch_offene or a["status"] == "bestaetigt"]
    empfaenger = list(dict.fromkeys(p["email"] for a in anmeldungen for p in a["personen"] if p["email"]))
    if not empfaenger:
        return "Keine E-Mail-Adresse vorhanden.", 0
    text = render_template("mail/absprache.txt", titel=TITEL, bezirk=bezirk_props(bid),
                           anmeldungen=anmeldungen).lstrip()
    betreff, _, rumpf = text.partition("\n")
    fehler = mail.senden(empfaenger, betreff.removeprefix("Betreff:").strip(), rumpf.strip() + "\n",
                         DB_PATH.parent / "mails")
    for a in anmeldungen:
        db().execute("INSERT INTO mail_log (anmeldung_id,art,empfaenger,ok,fehler,zeit) VALUES (?,?,?,?,?,?)",
                     (a["id"], "absprache", ", ".join(empfaenger), 0 if fehler else 1, fehler or "", jetzt()))
    db().commit()
    return fehler, len(empfaenger)


# ---------- Zugang ----------
def eltern_zugang():
    t = request.args.get("t")
    if t and secrets.compare_digest(t, ELTERN_TOKEN):
        session["eltern"] = True
    return session.get("eltern", False)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not ADMIN_USERNAME or session.get("admin_identity") != ADMIN_USERNAME:
            return redirect(url_for("admin_login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.route("/healthz")
def healthz():
    healthy = los_runtime.ready(DB_PATH, GEOJSON)
    return jsonify({"status": "ready" if healthy else "migration-or-map-required"}), 200 if healthy else 503



def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

@app.context_processor
def runtime_context():
    return {"csrf_token": csrf_token, "tile_url": TILE_URL, "parent_link": BASE_URL + "/?t=" + quote(ELTERN_TOKEN, safe="")}

@app.before_request
def contract_guard():
    if request.endpoint in ("healthz", "liveness"):
        return
    if not los_runtime.ready(DB_PATH, GEOJSON):
        return jsonify({"status": "migration-or-map-required"}), 503
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("_csrf")
        if not expected or not submitted or not secrets.compare_digest(expected, submitted):
            abort(403)
        if request.headers.get("Origin") and request.headers["Origin"] != BASE_URL:
            abort(403)

@app.after_request
def private_responses(response):
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.route("/health/live")
def liveness():
    return jsonify({"status": "live"})

app.add_url_rule("/health/ready", view_func=healthz)

def bounded_pdf(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _pdf_slot.acquire(blocking=False):
            return "PDF-Export belegt", 503, {"Retry-After": "5"}
        try:
            return fn(*args, **kwargs)
        finally:
            _pdf_slot.release()
    return wrapped



@app.errorhandler(Exception)
def safe_failure(error):
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error
    app.logger.error("Application operation failed (%s)", type(error).__name__)
    return jsonify({"fehler": "Die Anfrage konnte nicht verarbeitet werden. Bitte später erneut versuchen."}), 503


# ---------- Eltern ----------
@app.route("/")
def index():
    if not eltern_zugang():
        return render_template("gesperrt.html", titel=TITEL), 403
    edit = request.args.get("edit", "")
    if edit and len(edit) <= 128 and db().execute("SELECT 1 FROM anmeldungen WHERE edit_token=?", (edit,)).fetchone():
        mine = session.get("meine", [])
        if edit not in mine:
            session["meine"] = (mine + [edit])[-40:]
    if request.args.get("t") or edit:
        return redirect(url_for("index"))  # Capabilities leave the address bar immediately
    return render_template("index.html", titel=TITEL, max_personen=MAX_PERSONEN)


@app.route("/api/bezirke")
def api_bezirke():
    if not eltern_zugang():
        abort(403)
    gj = bezirke_mit_status()
    meine = session.get("meine", [])
    eigene = [{"bezirk_id": a["bezirk_id"], "status": a["status"], "edit_token": a["edit_token"],
               "personen": [{"name": p["name"], "klasse": p["klasse"]} for p in a["personen"]]}
              for a in anmeldungen_laden(f"WHERE edit_token IN ({','.join('?' * len(meine))})", tuple(meine))] if meine else []
    return jsonify({"bezirke": gj, "eigene": eigene})


def personen_pruefen(roh) -> tuple[list[dict], str | None]:
    if not isinstance(roh, list) or not 1 <= len(roh) <= MAX_PERSONEN:
        return [], f"Bitte 1 bis {MAX_PERSONEN} Personen angeben."
    personen = []
    for i, eingabe in enumerate(roh, 1):
        eingabe = eingabe if isinstance(eingabe, dict) else {}
        p = {k: str(eingabe.get(k) or "").strip() for k, _ in FELDER}
        bei = f" (Person {i})" if len(roh) > 1 else ""
        if not all(p.values()):
            return [], f"Bitte Name, Klasse, Telefonnummer und E-Mail-Adresse angeben{bei}."
        if any(len(p[k]) > n for k, n in FELDER):
            return [], f"Eine Eingabe ist zu lang{bei}."
        if not TELEFON_RE.match(p["telefon"]):
            return [], f"Bitte eine gültige Telefonnummer angeben{bei}."
        if not EMAIL_RE.match(p["email"]):
            return [], f"Bitte eine gültige E-Mail-Adresse angeben{bei}."
        personen.append(p)
    return personen, None


@app.route("/api/anmeldung", methods=["POST"])
def api_anmeldung():
    if not eltern_zugang():
        abort(403)
    d = request.get_json(force=True, silent=True) or {}
    bid = str(d.get("bezirk_id") or "").strip()
    personen, fehler = personen_pruefen(d.get("personen"))
    if fehler:
        return jsonify({"fehler": fehler}), 400

    con = db()
    con.execute("BEGIN IMMEDIATE")
    feat = next((f for f in bezirke_mit_status()["features"] if f["properties"]["id"] == bid), None)
    if not feat:
        con.rollback()
        return jsonify({"fehler": "Bezirk unbekannt."}), 404
    if not feat["properties"]["waehlbar"]:
        con.rollback()
        return jsonify({"fehler": "In diesem Bezirk sind inzwischen alle Plätze belegt. Bitte wählen Sie einen anderen Bezirk."}), 409
    token = secrets.token_urlsafe(16)
    aid = con.execute("INSERT INTO anmeldungen (bezirk_id,edit_token,created_at) VALUES (?,?,?)",
                      (bid, token, jetzt())).lastrowid
    for pos, p in enumerate(personen, 1):
        con.execute("INSERT INTO personen (anmeldung_id,pos,name,klasse,telefon,email) VALUES (?,?,?,?,?,?)",
                    (aid, pos, p["name"], p["klasse"], p["telefon"], p["email"]))
    from los_worker import enqueue
    enqueue(con, aid, "eingang")
    con.commit()
    session["meine"] = session.get("meine", []) + [token]
    return jsonify({"ok": True, "edit_token": token})


@app.route("/api/anmeldung/<token>", methods=["DELETE"])
def api_anmeldung_loeschen(token):
    if not eltern_zugang():
        abort(403)
    if token not in session.get("meine", []):
        abort(403)
    a = db().execute("SELECT status FROM anmeldungen WHERE edit_token=?", (token,)).fetchone()
    if a and a["status"] == "bestaetigt":
        return jsonify({"fehler": "Ihre Anmeldung ist bereits fest zugeteilt. Bitte antworten Sie auf die "
                                  "Zuteilungs-E-Mail, wenn Sie absagen möchten."}), 409
    db().execute("DELETE FROM mail_log WHERE anmeldung_id IN (SELECT id FROM anmeldungen WHERE edit_token=?)", (token,))
    db().execute("DELETE FROM anmeldungen WHERE edit_token=?", (token,))
    db().commit()
    return jsonify({"ok": True})


# ---------- Admin ----------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    fehler = None
    if request.method == "POST":
        password_ok = (check_password_hash(ADMIN_PASSWORD_HASH, request.form.get("pw", ""))
                       if ADMIN_PASSWORD_HASH else ADMIN_PASSWORD and secrets.compare_digest(request.form.get("pw", ""), ADMIN_PASSWORD))
        if (ADMIN_USERNAME and password_ok
                and secrets.compare_digest(request.form.get("username", ""), ADMIN_USERNAME)
                ):
            session["admin"] = True
            session["admin_identity"] = ADMIN_USERNAME
            ziel = request.args.get("next", "")
            return redirect(ziel if ziel.startswith("/") and not ziel.startswith("//") else url_for("admin"))
        fehler = "Anmeldedaten falsch."
    return render_template("login.html", titel=TITEL, fehler=fehler)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_identity", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin():
    gj = bezirke_mit_status()
    alle = anmeldungen_laden()
    by_b = {}
    for a in alle:
        by_b.setdefault(a["bezirk_id"], []).append(a)
    bezirke = []
    for f in gj["features"]:
        p = f["properties"]
        p["anmeldungen"] = by_b.get(p["id"], [])
        bezirke.append(p)
    namen = {b["id"]: b["name"] for b in bezirke}
    return render_template("admin.html", titel=TITEL, bezirke=bezirke, namen=namen,
                           unbestaetigt=[a for a in alle if a["status"] == "offen"],
                           ohne=[b for b in bezirke if b["anzahl"] == 0],
                           gesamt=len(alle), max_personen=MAX_PERSONEN, mail_test=mail.testbetrieb(),
                           mail_einstellungen=mail.einstellungen(),
                           eltern_link=BASE_URL + "/?t=" + quote(ELTERN_TOKEN, safe=""))


def zurueck(bid: str | None = None):
    return redirect(url_for("admin") + (f"#{bid}" if bid else ""))


@app.route("/admin/anmeldung", methods=["POST"])
@admin_required
def admin_anmeldung_neu():
    f = request.form
    personen = []
    for i in range(1, MAX_PERSONEN + 1):
        p = {k: f.get(f"{k}{i}", "").strip()[:n] for k, n in FELDER}
        if p["name"]:
            personen.append(p)
    if not personen:
        flash("Bitte mindestens einen Namen angeben.", "fehler")
        return zurueck(f["bezirk_id"])
    aid = db().execute("INSERT INTO anmeldungen (bezirk_id,edit_token,created_at,von_admin) VALUES (?,?,?,1)",
                       (f["bezirk_id"], secrets.token_urlsafe(16), jetzt())).lastrowid
    for pos, p in enumerate(personen, 1):
        db().execute("INSERT INTO personen (anmeldung_id,pos,name,klasse,telefon,email) VALUES (?,?,?,?,?,?)",
                     (aid, pos, p["name"], p["klasse"], p["telefon"], p["email"]))
    db().commit()
    flash(f"Anmeldung für {personen[0]['name']} angelegt (noch nicht bestätigt).", "ok")
    return zurueck(f["bezirk_id"])


def anmeldung_oder_404(aid: int):
    a = db().execute("SELECT * FROM anmeldungen WHERE id=?", (aid,)).fetchone()
    if not a:
        abort(404)
    return a


@app.route("/admin/anmeldung/<int:aid>/bestaetigen", methods=["POST"])
@admin_required
def admin_bestaetigen(aid):
    a = anmeldung_oder_404(aid)
    db().execute("UPDATE anmeldungen SET status='bestaetigt', bestaetigt_at=? WHERE id=?", (jetzt(), aid))
    db().commit()
    fehler = mail_senden(aid, "zuteilung")
    if fehler:
        flash(f"Bestätigt, aber die Zuteilungs-Mail konnte nicht verschickt werden: {fehler}", "fehler")
    else:
        flash("Bestätigt und Zuteilungs-Mail verschickt." + (" (Testbetrieb: nur abgelegt)" if mail.testbetrieb() else ""), "ok")
    return zurueck(a["bezirk_id"])


@app.route("/admin/anmeldung/<int:aid>/mail/<art>", methods=["POST"])
@admin_required
def admin_mail_erneut(aid, art):
    if art not in ("eingang", "zuteilung"):
        abort(404)
    a = anmeldung_oder_404(aid)
    fehler = mail_senden(aid, art)
    flash(f"Mail konnte nicht verschickt werden: {fehler}" if fehler else "Mail erneut verschickt.", "fehler" if fehler else "ok")
    return zurueck(a["bezirk_id"])


@app.route("/admin/testmail", methods=["POST"])
@admin_required
def admin_testmail():
    an = request.form.get("an", "").strip()
    if not EMAIL_RE.match(an):
        flash("Test-Mail: Bitte eine gültige E-Mail-Adresse angeben.", "fehler")
        return zurueck("mailversand")
    text = (f"Guten Tag,\n\ndies ist eine Test-Mail aus der Verwaltung von „{TITEL}“, verschickt am "
            f"{datetime.now():%d.%m.%Y um %H:%M} Uhr.\n\nWenn Sie diese Nachricht lesen, funktioniert der Mailversand.\n")
    fehler = mail.senden([an], f"{TITEL}: Test-Mail", text, DB_PATH.parent / "mails")
    if fehler:
        flash(f"Test-Mail an {an} fehlgeschlagen: {fehler}", "fehler")
    elif mail.testbetrieb():
        flash(f"Test-Mail an {an} wurde nur als Datei unter data/mails/ abgelegt (Testbetrieb, kein SMTP-Server eingerichtet).", "hinweis")
    else:
        flash(f"Test-Mail an {an} verschickt. Bitte Posteingang (und Spam-Ordner) prüfen.", "ok")
    return zurueck("mailversand")


@app.route("/admin/bezirk/<bid>/absprache", methods=["POST"])
@admin_required
def admin_absprache(bid):
    auch_offene = bool(request.form.get("auch_offene"))
    fehler, anzahl = mail_absprache(bid, auch_offene)
    name = bezirk_props(bid)["name"]
    if fehler and anzahl == 0:
        flash(f"{name}: keine Anmeldung mit E-Mail-Adresse{'' if auch_offene else ' (nur bestätigte berücksichtigt)'}.", "hinweis")
    elif fehler:
        flash(f"{name}: Absprache-Mail fehlgeschlagen: {fehler}", "fehler")
    else:
        flash(f"{name}: Absprache-Mail an {anzahl} Adresse{'n' if anzahl != 1 else ''} verschickt.", "ok")
    return zurueck(bid)


@app.route("/admin/anmeldung/<int:aid>/verschieben", methods=["POST"])
@admin_required
def admin_verschieben(aid):
    a = anmeldung_oder_404(aid)
    ziel = request.form["bezirk_id"]
    db().execute("UPDATE anmeldungen SET bezirk_id=?, status='offen', bestaetigt_at=NULL WHERE id=?", (ziel, aid))
    db().commit()
    if a["status"] == "bestaetigt":
        flash("Verschoben. Die Bestätigung wurde zurückgesetzt – bitte im neuen Bezirk erneut bestätigen, "
              "damit die neue Zuteilung per Mail verschickt wird.", "hinweis")
    return zurueck(ziel)


@app.route("/admin/anmeldung/<int:aid>/loeschen", methods=["POST"])
@admin_required
def admin_loeschen(aid):
    a = anmeldung_oder_404(aid)
    db().execute("DELETE FROM mail_log WHERE anmeldung_id=?", (aid,))
    db().execute("DELETE FROM anmeldungen WHERE id=?", (aid,))
    db().commit()
    flash("Anmeldung gelöscht.", "ok")
    return zurueck(a["bezirk_id"])


@app.route("/admin/bezirk/<bid>", methods=["POST"])
@admin_required
def admin_bezirk(bid):
    gesperrt = 1 if request.form.get("gesperrt") else 0
    mo = request.form.get("max_override", "").strip()
    max_override = int(mo) if mo.isdigit() else None
    db().execute("INSERT INTO bezirk_status (bezirk_id,gesperrt,max_override) VALUES (?,?,?) "
                 "ON CONFLICT(bezirk_id) DO UPDATE SET gesperrt=excluded.gesperrt, max_override=excluded.max_override",
                 (bid, gesperrt, max_override))
    db().commit()
    return zurueck(bid)


@app.route("/admin/export.csv")
@admin_required
def admin_export():
    names = {f["properties"]["id"]: f["properties"] for f in load_geojson()["features"]}
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Bezirk-ID", "Bezirk", "Straßen", "Anmeldung", "Status", "Person", "Name", "Klasse",
                "Telefon", "E-Mail", "Angemeldet am", "Bestätigt am"])
    for a in sorted(anmeldungen_laden(), key=lambda a: (a["bezirk_id"], a["created_at"])):
        b = names.get(a["bezirk_id"], {})
        for p in a["personen"]:
            w.writerow([a["bezirk_id"], b.get("name", ""), ", ".join(b.get("strassen", [])), a["id"],
                        "bestätigt" if a["status"] == "bestaetigt" else "offen", p["pos"], p["name"], p["klasse"],
                        p["telefon"], p["email"], datum(a["created_at"]), datum(a["bestaetigt_at"])])
    resp = make_response("﻿" + out.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=losverkauf-anmeldungen.csv"
    return resp


@app.route("/admin/liste.pdf")
@admin_required
@bounded_pdf
def admin_pdf():
    """Gesamtliste nach Bezirk sortiert, mit Kontaktdaten – zum Ausdrucken."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    nur_bestaetigt = request.args.get("nur") == "bestaetigt"
    gj = bezirke_mit_status()
    nach_bezirk = {}
    for a in anmeldungen_laden():
        if not (nur_bestaetigt and a["status"] != "bestaetigt"):
            nach_bezirk.setdefault(a["bezirk_id"], []).append(a)

    s = getSampleStyleSheet()
    stil_titel = ParagraphStyle("titel", parent=s["Title"], fontSize=16, spaceAfter=2 * mm, alignment=0)
    stil_klein = ParagraphStyle("klein", parent=s["Normal"], fontSize=8, textColor=colors.HexColor("#5B6270"))
    stil_bezirk = ParagraphStyle("bezirk", parent=s["Heading2"], fontSize=12, spaceBefore=4 * mm, spaceAfter=1 * mm)
    stil_zelle = ParagraphStyle("zelle", parent=s["Normal"], fontSize=9, leading=11)
    stil_leer = ParagraphStyle("leer", parent=stil_klein, spaceBefore=1 * mm)

    def kopfzeile(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#5B6270"))
        canvas.drawString(15 * mm, 10 * mm, f"{TITEL} – Stand {datetime.now():%d.%m.%Y %H:%M}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    inhalt = [Paragraph(f"{escape(TITEL)} – Verkäuferliste", stil_titel),
              Paragraph("nur fest zugeteilte Anmeldungen" if nur_bestaetigt
                        else "alle Anmeldungen, auch noch nicht bestätigte", stil_klein),
              Spacer(1, 4 * mm)]

    for f in gj["features"]:
        p = f["properties"]
        block = [Paragraph(f"{p['name']} &nbsp;<font size=9 color='#5B6270'>"
                           f"({len(nach_bezirk.get(p['id'], []))} von {p['max_effektiv']} Plätzen belegt)</font>", stil_bezirk),
                 Paragraph(", ".join(p["strassen"]), stil_klein)]
        zeilen = [["Name", "Klasse", "Telefon", "E-Mail", "Status"]]
        trenner = []
        for a in nach_bezirk.get(p["id"], []):
            trenner.append(len(zeilen))
            for i, person in enumerate(a["personen"]):
                zeilen.append([Paragraph(escape(person["name"]), stil_zelle), person["klasse"], person["telefon"],
                               Paragraph(escape(person["email"]), stil_zelle),
                               ("bestätigt" if a["status"] == "bestaetigt" else "offen") if i == 0 else ""])
        if len(zeilen) == 1:
            block.append(Paragraph("Noch keine Anmeldung.", stil_leer))
        else:
            t = Table(zeilen, colWidths=[48 * mm, 14 * mm, 32 * mm, 62 * mm, 20 * mm], repeatRows=1)
            t.setStyle(TableStyle([
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#5B6270")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#9AA3AD")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                # dünne Linie zwischen den Anmeldungen (eine Anmeldung = ein Team)
                *[("LINEABOVE", (0, r), (-1, r), 0.4, colors.HexColor("#DDE1E6")) for r in trenner[1:]],
            ]))
            block.append(t)
        inhalt.append(KeepTogether(block))

    puffer = io.BytesIO()
    SimpleDocTemplate(puffer, pagesize=A4, title=f"{TITEL} – Verkäuferliste", author=TITEL,
                      leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm
                      ).build(inhalt, onFirstPage=kopfzeile, onLaterPages=kopfzeile)

    resp = make_response(puffer.getvalue())
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=losverkauf-{'zuteilung' if nur_bestaetigt else 'anmeldungen'}-"
        f"{datetime.now():%Y-%m-%d}.pdf")
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", debug=False, port=5006)
