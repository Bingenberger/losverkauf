import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import csrf,parent,login,registration


@pytest.mark.parametrize("name",["SECRET_KEY","ELTERN_TOKEN"])
def test_missing_required_secret_refuses_start(name):
    env=os.environ.copy()
    env["SECRET_KEY"]="synthetic-session-key-fixture-0123456789"
    env["ELTERN_TOKEN"]="synthetic-parent-token-fixture-0123456789"
    env[name]=""
    result=subprocess.run([sys.executable,"-c","import app"],env=env,capture_output=True,timeout=10)
    assert result.returncode!=0


def test_import_never_migrates_live_and_ready_are_distinct(runtime):
    app,job=runtime
    client=app.app.test_client()
    assert not app.DB_PATH.exists()
    assert client.get("/health/live").status_code==200
    assert client.get("/healthz").status_code==503
    assert client.get("/").status_code==503
    assert not app.DB_PATH.exists()
    job.migrate()
    job.migrate()
    assert client.get("/health/ready").status_code==200
    assert app.DB_PATH.stat().st_mode&0o777==0o600


def test_legacy_conversion_preserves_ids_and_capabilities(runtime):
    app,job=runtime
    with sqlite3.connect(app.DB_PATH) as db:
        db.execute("CREATE TABLE wuensche(id INTEGER PRIMARY KEY,bezirk_id TEXT,edit_token TEXT,created_at TEXT,name TEXT,klasse TEXT,team TEXT,kontakt TEXT)")
        db.execute("INSERT INTO wuensche VALUES(41,'one','synthetic-existing-capability','2050-01-01T10:00','Synthetic','3a','Synthetic Team','synthetic@example.invalid')")
    job.migrate()
    job.migrate()
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT id,edit_token FROM anmeldungen").fetchall()==[(41,"synthetic-existing-capability")]
        assert db.execute("SELECT anmeldung_id FROM personen").fetchall()==[(41,),(41,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]


def test_unknown_schema_rolls_back_without_data_loss(runtime):
    app,job=runtime
    with sqlite3.connect(app.DB_PATH) as db:
        db.executescript("CREATE TABLE foreign_table(id INTEGER);INSERT INTO foreign_table VALUES(7);")
    with pytest.raises(ValueError):
        job.migrate()
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT * FROM foreign_table").fetchall()==[(7,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='anmeldungen'").fetchall()==[]


def test_admin_identity_all_unsafe_routes_csrf_and_post_logout(installed):
    app=installed
    client=app.app.test_client()
    with client.session_transaction() as session:
        session["admin"]=True
    assert client.get("/admin").status_code==302
    nonce=csrf(client)
    assert client.post("/admin/login",data={"username":"synthetic.operator","pw":"synthetic-password"}).status_code==403
    assert client.post("/admin/login",data={"username":"wrong","pw":"synthetic-password","csrf_token":nonce}).status_code==200
    client,nonce=login(app)
    assert client.get("/admin").status_code==200
    for rule in app.app.url_map.iter_rules():
        if rule.rule.startswith("/admin") and "POST" in rule.methods:
            path=rule.rule.replace("<int:aid>","1").replace("<bid>","one").replace("<art>","eingang")
            assert client.post(path).status_code==403,path
    assert client.get("/admin/logout").status_code==405
    assert client.post("/admin/logout",data={"csrf_token":nonce}).status_code==302
    assert client.get("/admin").status_code==302


def test_parent_csrf_origin_and_other_owner_deletion(installed):
    app=installed
    assert app.app.test_client().get("/").status_code==403
    owner,nonce=parent(app)
    assert owner.post("/api/anmeldung",json={}).status_code==403
    assert owner.post("/api/anmeldung",json={},headers={"X-CSRF-Token":nonce,"Origin":"https://spoof.example.invalid"}).status_code==403
    response=registration(owner,nonce)
    assert response.status_code==200
    capability=response.json["edit_token"]
    other,other_nonce=parent(app)
    assert other.delete("/api/anmeldung/"+capability,headers={"X-CSRF-Token":other_nonce}).status_code==403
    assert owner.delete("/api/anmeldung/"+capability,headers={"X-CSRF-Token":nonce}).status_code==200
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT count(*) FROM personen").fetchone()==(0,)
        assert db.execute("SELECT count(*) FROM mail_outbox").fetchone()==(0,)


def test_parallel_capacity_is_transactional(installed):
    app=installed
    def book(index):
        client,nonce=parent(app)
        return registration(client,nonce,name="Synthetic "+str(index)).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes=list(pool.map(book,range(8)))
    assert codes.count(200)==1 and codes.count(409)==7
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT count(*) FROM anmeldungen").fetchone()==(1,)
        assert db.execute("SELECT count(*) FROM mail_outbox").fetchone()==(1,)


def test_queue_full_returns_503_and_rolls_back_registration(installed):
    app=installed
    client,nonce=parent(app)
    assert registration(client,nonce).status_code==200
    with sqlite3.connect(app.DB_PATH) as db:
        db.executemany("INSERT INTO mail_outbox(anmeldung_id,art) VALUES(1,'eingang')",[()]*199)
    assert registration(client,nonce,district="two").status_code==503
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT count(*) FROM anmeldungen").fetchone()==(1,)


def test_durable_retry_and_inflight_recovery(installed,monkeypatch):
    app=installed
    client,nonce=parent(app)
    assert registration(client,nonce).status_code==200
    import los_worker
    monkeypatch.setattr(app.mail,"senden",lambda *args:"Synthetic failure")
    assert los_worker.dispatch_once()
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT status,attempts FROM mail_outbox").fetchone()==("pending",1)
        db.execute("UPDATE mail_outbox SET status='sending',available_at=0")
    monkeypatch.setattr(app.mail,"senden",lambda *args:None)
    los_worker.start()
    try:
        for _ in range(100):
            with sqlite3.connect(app.DB_PATH) as db:
                state=db.execute("SELECT status,attempts FROM mail_outbox").fetchone()
            if state==("sent",2): break
            time.sleep(.05)
        assert state==("sent",2)
    finally:
        los_worker.stop()
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT ok FROM mail_log ORDER BY id").fetchall()==[(0,),(1,)]


def test_pdf_slot_and_markup_escaping(installed):
    app=installed
    owner,nonce=parent(app)
    assert registration(owner,nonce,name='<img src="http://forbidden.invalid/x">').status_code==200
    admin,nonce=login(app)
    app._pdf_slot.acquire()
    try:
        assert admin.get("/admin/liste.pdf").status_code==503
    finally:
        app._pdf_slot.release()
    response=admin.get("/admin/liste.pdf")
    assert response.status_code==200 and response.data.startswith(b"%PDF")


def test_all_browser_assets_local_and_private_headers(installed):
    app=installed
    client,nonce=parent(app)
    response=client.get("/")
    assert "https://unpkg.com" not in response.text
    assert 'name="tile-url" content=""' in response.text
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code==200
    assert response.headers["Referrer-Policy"]=="no-referrer"
    assert response.headers["Cache-Control"]=="private, no-store"


def test_mail_resume_link_preserves_existing_capability_ownership(installed):
    app=installed
    owner,nonce=parent(app)
    capability=registration(owner,nonce).json["edit_token"]
    anonymous=app.app.test_client()
    assert anonymous.get("/?edit="+capability).status_code==403
    restored=app.app.test_client()
    assert restored.get("/?t="+app.ELTERN_TOKEN+"&edit="+capability).status_code==302
    assert restored.get("/api/bezirke").json["eigene"][0]["edit_token"]==capability
    nonce=csrf(restored,"/")
    assert restored.delete("/api/anmeldung/"+capability,headers={"X-CSRF-Token":nonce}).status_code==200


def test_unknown_legacy_columns_refuse_conversion_without_data_loss(runtime):
    app,job=runtime
    with sqlite3.connect(app.DB_PATH) as db:
        db.execute("CREATE TABLE wuensche(id INTEGER PRIMARY KEY,bezirk_id TEXT,edit_token TEXT,created_at TEXT,name TEXT,klasse TEXT,future_value TEXT)")
        db.execute("INSERT INTO wuensche VALUES(7,'one','synthetic-retained','2050-01-01','Synthetic','3a','keep me')")
    with pytest.raises(ValueError):
        job.migrate()
    with sqlite3.connect(app.DB_PATH) as db:
        assert db.execute("SELECT id,future_value FROM wuensche").fetchall()==[(7,'keep me')]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='anmeldungen'").fetchall()==[]
