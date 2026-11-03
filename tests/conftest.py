"""Synthetic runtime fixtures; no SMTP or external map services."""
import importlib
import json
import os
from pathlib import Path

import pytest

GEO = {"type":"FeatureCollection","features":[
    {"type":"Feature","properties":{"id":"one","name":"Synthetic One","strassen":["Synthetic Street"],"max":1},
     "geometry":{"type":"MultiLineString","coordinates":[[[7.0,50.0],[7.01,50.01]]]}},
    {"type":"Feature","properties":{"id":"two","name":"Synthetic Two","strassen":["Synthetic Lane"],"max":4},
     "geometry":{"type":"MultiLineString","coordinates":[[[7.01,50.0],[7.02,50.01]]]}}
]}

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "synthetic-session-key-fixture-0123456789")
    monkeypatch.setenv("ELTERN_TOKEN", "synthetic-parent-token-fixture-0123456789")
    monkeypatch.setenv("ADMIN_USERNAME", "synthetic.operator")
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-password")
    monkeypatch.setenv("BASE_URL", "http://localhost")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path/"app.db"))
    monkeypatch.setenv("GEOJSON_PATH", str(tmp_path/"maps/bezirke.geojson"))
    (tmp_path/"maps").mkdir()
    (tmp_path/"maps/bezirke.geojson").write_text(json.dumps(GEO))
    import mail
    importlib.reload(mail)
    import app
    importlib.reload(app)
    import los_migrate
    importlib.reload(los_migrate)
    return app,los_migrate

@pytest.fixture
def installed(runtime):
    app, job = runtime
    job.migrate()
    app.app.config["TESTING"]=True
    return app

def csrf(client, path="/admin/login"):
    import re
    response=client.get(path)
    match=re.search(r'name="csrf-token" content="([^"]+)"',response.text)
    assert match
    return match[1]

def parent(app):
    client=app.app.test_client()
    assert client.get("/?t="+app.ELTERN_TOKEN).status_code==302
    token=csrf(client,"/")
    return client,token

def login(app):
    client=app.app.test_client()
    token=csrf(client)
    assert client.post("/admin/login",data={"username":"synthetic.operator","pw":"synthetic-password","csrf_token":token}).status_code==302
    return client,token

def registration(client, nonce, district="one", name="Synthetic"):
    return client.post("/api/anmeldung",json={"bezirk_id":district,"personen":[
        {"name":name,"klasse":"3a","telefon":"0123456789","email":"synthetic@example.invalid"}]},
        headers={"X-CSRF-Token":nonce})
