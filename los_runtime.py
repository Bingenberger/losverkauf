"""Explicit configuration, schema readiness and trusted proxy addresses."""
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

os.umask(0o077)
SCHEMA_VERSION = 1

def value(name, default=""):
    path = os.environ.get(name + "_FILE", "")
    raw = os.environ.get(name, "")
    if path and raw:
        raise RuntimeError("Configure exactly one source for " + name)
    return Path(path).read_text().strip() if path else raw or default

def required(name):
    result = value(name)
    if len(result) < 32 or result.startswith(("<", "changeme")):
        raise RuntimeError(name + " requires a private value of at least 32 characters")
    return result

def origin():
    result = os.environ.get("BASE_URL", "http://localhost:5006").rstrip("/")
    parsed = urlsplit(result)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise RuntimeError("BASE_URL must be an HTTP(S) origin")
    _ = parsed.port
    return result

def trusted_ips():
    result = os.environ.get("PROXY_IPS", "").split(",") if os.environ.get("PROXY_IPS") else []
    for address in result:
        ipaddress.ip_address(address)
    return set(result)

def geojson(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("A bounded local GeoJSON file is required")
    result = json.loads(path.read_text())
    if result.get("type") != "FeatureCollection" or not isinstance(result.get("features"), list):
        raise ValueError("Invalid district GeoJSON")
    seen = set()
    for feature in result["features"]:
        props = feature["properties"]
        if (not isinstance(props["id"], str) or props["id"] in seen or not isinstance(props["name"], str)
                or not isinstance(props["strassen"], list)):
            raise ValueError("Invalid district properties")
        seen.add(props["id"])
    return result

def ready(path, map_path):
    if not Path(path).is_file():
        return False
    try:
        geojson(map_path)
        with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                return False
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"anmeldungen", "personen", "mail_log", "bezirk_status", "mail_outbox"} <= tables
    except (ValueError, KeyError, sqlite3.Error, OSError):
        return False
