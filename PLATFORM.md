# Losverkauf: eigenständiger Containervertrag

Python 3.12.12/amd64, Flask 3.1.3, Gunicorn 26.2.0 und ReportLab 5.1.0,
aus Hash-Lockfiles installiert. Basisimage mit Versionsnummer und Digest.
Die lokale Anmeldung, Elternzugänge und bestehenden edit_token-Werte bleiben erhalten.

## Erster Standalone-Start

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/python prepare_secrets.py .private-secrets --admin-user PERSOENLICHER_NAME
export LOSVERKAUF_SECRETS_DIR="$PWD/.private-secrets"
docker compose build
docker compose up -d --no-build
docker compose run --rm --no-deps app python -m los_migrate
```

Die lokale Einrichtung verlangt ein kontrollierendes Terminal und eine
Passwortbestätigung. Kein Standardpasswort, kein öffentlicher Erstregistrierungsweg.
Session- und Elterntoken sind zufällig; das persönliche Passwort liegt als
Werkzeug-Scrypt-Hash vor. Dateien sind einzelne 0444-Projektionen unter einem
privaten 0700-Elternverzeichnis, damit UID 10001 sie lesen kann. Keine Ausgabe
der Werte und keine Ersetzung bestehender Schlüssel bei Wiederholung.
.private-secrets niemals committen.

Ohne SECRET_KEY oder ELTERN_TOKEN von jeweils mindestens 32 Zeichen scheitert
der Start. *_FILE ist als Alternative zu bestehenden Env-Namen möglich.
Die Quelle darf nur einmal konfiguriert sein. ADMIN_USERNAME bleibt eine
persönliche Identität; ADMIN_PASSWORD wird für bestehende native Konfigurationen
weiter unterstützt. Bevorzugt ADMIN_USERNAME_FILE und ADMIN_PASSWORD_HASH_FILE.
Es ist weiterhin ein einzelner persönlicher Verwaltungszugang, kein neues
Fachbenutzerverzeichnis.

## Migration, Daten und Karte

Kein Schemaaufbau beim Import oder während einer Anfrage.
Liveness ist /health/live; /healthz und /health/ready prüfen DBversion 1,
erwartete Tabellen und eine lesbare lokale GeoJSON-Datei. Vor Migration 503.

Der explizite Job sperrt exklusiv und verwendet BEGIN IMMEDIATE.
Die bekannte wuensche-Umwandlung erhält IDs und edit_tokens. Nicht bekannte
Tabellen, Spalten und neuere Versionen werden ohne verlustbehaftete Umwandlung
abgewiesen. Fremdschlüssel werden geprüft; keine Fehlerunterdrückung als
vermeintlicher Erfolg.

Persistenz:
- /data/losverkauf.db samt WAL, UID:GID 10001:10001, Datei 0600.
- /data/maps/bezirke.geojson und /data/mails, Verzeichnisse 0700.
- Schlüssel und persönliche Identität liegen gesondert in privaten Projektionen.

Das Image enthält nur eine leere Karten-Konfiguration und keine schulischen
Bezirke, PDFs oder Kontaktdaten. Eigene geprüfte GeoJSON-Daten in das Datenvolume
übernehmen. import_streets.py bleibt ein separat und ausdrücklich aufgerufener
Overpass-Import; BEZIRKE_CONFIG und GEOJSON_PATH können seine Pfade festlegen.
Er läuft weder im Image-Build noch beim Webstart. Im lokalen Nachweis werden
ausschließlich synthetische Straßenzüge benutzt.

Leaflet 1.9.4 ist mit BSD-2-Clause-Lizenz und Hashinventar unter
static/vendor/leaflet lokal vorhanden. Keine CDN-Abhängigkeit.
TILE_URL ist standardmäßig leer; die Bezirksgeometrien lassen sich ohne externe
Hintergrundkacheln anzeigen. Ein optionaler eigener Kacheldienst wird nur über
einen Same-Origin-Pfad konfiguriert. OSM-Daten und Kacheln sind eigenständige
Bestände und werden nicht mitgeliefert; Freigabe und Nutzungsbedingungen vor
einer produktiven Übernahme gesondert prüfen.

## Anmeldung, Links, CSRF und Export

BASE_URL ist die vollständige Laufzeit-Origin. Bei HTTPS entstehen Secure-Cookies.
PROXY_IPS akzeptiert nur explizite IP-Adressen; ohne Konfiguration kein Vertrauen
in Forwarded-Header. Absolute Eltern- und Mail-Links stammen aus BASE_URL.
Eingehender Forwarded-Host beeinflusst sie nicht.

Eltern betreten die App weiterhin über ?t=. Token werden nach Einstieg aus der
Adresszeile entfernt. Individuelle Eingangs-/Zuteilungsmails enthalten zusätzlich
&edit=<bestehender edit_token>; damit kann ein neuer Browser oder die neue Domain
denselben Anmeldungsbesitz wieder aufnehmen. Die Token werden nicht neu erzeugt.
Ohne Elternzugang genügt der edit_token allein nicht.
Hostgebundene Cookies werden beim Domainwechsel nicht automatisch übertragen.

Alle schreibenden Formulare und JSON-/DELETE-Aufrufe verlangen eine sitzungsgebundene
CSRF-Nonce. Admin-Logout ist POST. Export bleibt auf einen PDF-Slot im deklarierten
einzigen Webprozess begrenzt; Konkurrenz erhält 503 mit Retry-After.
Personenwerte in PDF-Paragraphen werden maskiert.
Keine Accesslogs für tokenisierte URLs; no-referrer und private,no-store.
Kein zentraler Logout und keine zentrale Cookie-Revokation.

## Mailwarteschlange und Stop

Eingangsbestätigungen werden zusammen mit der Anmeldung atomar in mail_outbox
gespeichert. Ein exklusiver Dispatcher verarbeitet genau einen Auftrag zugleich.
Höchstens 200 wartende/laufende Aufträge; bei Erschöpfung 503 mit Rollback der
Anmeldung. Fehler werden höchstens fünfmal mit begrenztem Backoff versucht.
Bei Neustart werden unter exklusivem Workerlock unterbrochene sending-Aufträge
wieder vorgemerkt.

At-least-once-Zustellung: Nach SMTP-Annahme und Absturz vor dem lokalen Abschluss
kann eine Mail doppelt zugestellt werden. Keine Exactly-once-Zusage.
Andere vorhandene Admin-Mailaktionen bleiben synchron mit Socket-Zeitlimit;
eine appübergreifende persistente Relayqueue ist noch offen.

Standalone standardmäßig ohne SMTP_HOST: private .eml-Dateien statt Zustellung.
Tests verwenden ausschließlich den lokalen Mail-Sink ohne Upstream.
SMTP_PASSWORD_FILE ist möglich; Zusatzdatei ausdrücklich schreibgeschützt mounten.
SMTP-Ausfälle machen die eigene DB-Readiness nicht ungesund.

Gunicorn: 1 Prozess, 4 HTTP-Threads, ein Maildispatcher; Init, Read-only-Root,
tmpfs 64 MiB, 384 MiB/1 CPU/128 Prozesse als gesetzte Grenzen.
Stop behält Daten und Schlüssel; pending/sending-Aufträge sind wiederaufnehmbar.
Keine Kapazitätsmessung aus diesen Limits ableiten.

## Backup und Grenzen

DB über SQLite-.backup-API und Maps-/Mailverzeichnisse bei koordiniertem
Schreibstopp sichern. Restore nur in ein leeres Ziel, danach passende Konfiguration
und Schlüssel bereitstellen und ohne erneute Migration starten.
Image-Rollback setzt keine Datenbank zurück. Verschlüsseltes Offsitebackup,
Secretstore-Gesamtbackup, Retention und Alarmierung sind noch offen.

Geprüft: 14 isolierte Laufzeit-/Negativtests, eigenständiger Compose-Start,
8 gleichzeitige Anmeldungen auf 1 Platz, Mail-Sink, Queue-Erhalt bei Neustart,
kombinierter Restore mit gleichen Tokens/IDs, lokales TLS, Domainwechsel mit
erzwungenem Rückfall und zwei No-op-Läufe. Keine Browserautomation,
öffentliche DNS-/ACME-Abnahme, ARM64- oder Zielservermessung.
Projektlizenzfreigabe, Gesamt-SBOM und Registry-Veröffentlichung fehlen.
Die historischen nativen Automationsskripte sind für diesen geänderten Vertrag
noch nicht abgenommen und wurden nicht gegen einen Server ausgeführt.
