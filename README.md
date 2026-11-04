# Losverkauf – eigenständige Schul-App

Eltern wählen einen Verkaufsbezirk auf einer lokalen Karte und melden 1–3
Personen an. Eine Anmeldung belegt einen Platz. Der persönliche Verwaltungszugang
bestätigt Zuordnungen und bietet Mail, CSV und PDF an.

Der geprüfte Stand mit Migration, Secrets, Mailqueue, Backup und Grenzen steht
in [PLATFORM.md](PLATFORM.md).

<!-- schulplattform:installation -->
## Installation

### Im Rahmen der Schulplattform

Empfohlen für den Schulserver: Die [Schulplattform](https://github.com/Bingenberger/schulplattform) installiert diese App zusammen mit den
anderen Schul-Apps auf einem leeren Ubuntu-24.04-Server, mit eigener Subdomain, TLS-Zertifikat, Mailversand,
Sicherung und Dashboard. Sie verwendet das signierte Release-Image `ghcr.io/bingenberger/losverkauf`
und die unveränderte `docker-compose.yml` dieses Repositorys plus eine Override-Datei.

1. In der Plattformkonfiguration (`./install.sh configure` bzw. `/etc/schulplattform/platform.toml`)
   unter `[apps.losverkauf]` `enabled = true` und den Hostnamen setzen, z. B. `lose.ihre-schule.de`,
   und beim DNS-Provider einen A-Eintrag auf den Server anlegen.
2. `./install.sh apply …` installiert die App. Migration, Proxy, Zertifikat und Sicherung übernimmt die Plattform.
3. `./install.sh app-setup losverkauf` legt den persönlichen Verwaltungszugang an (Name und Passwort am Terminal; gespeichert wird nur ein Werkzeug-Hash).
4. Daten vom bisherigen Server übernehmen: `./install.sh import losverkauf …` nach dem
   [Umzugs-Runbook](https://github.com/Bingenberger/schulplattform/blob/main/docs/betrieb/umzug.md).

### Standalone mit Docker Compose

Ohne Plattform, z. B. zum Ausprobieren oder für einen eigenen Server mit eigenem Proxy.
Benötigt Docker Engine mit Compose v2 (amd64). Die App lauscht danach nur auf `http://127.0.0.1:5006`.

Siehe Abschnitt **„Standalone“** unten (`prepare_secrets.py`, Build, `python -m los_migrate`). Die Bezirkskarte gehört nach `/data/maps/bezirke.geojson`.

Statt selbst zu bauen, kann das veröffentlichte Image verwendet werden (dann `docker compose build` weglassen):

```sh
docker pull ghcr.io/bingenberger/losverkauf:0.1.0 && docker tag ghcr.io/bingenberger/losverkauf:0.1.0 losverkauf:0.1.0-dev
```
<!-- /schulplattform:installation -->

## Standalone

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/python prepare_secrets.py .private-secrets --admin-user PERSOENLICHER_NAME
export LOSVERKAUF_SECRETS_DIR="$PWD/.private-secrets"
docker compose build
docker compose up -d --no-build
docker compose run --rm --no-deps app python -m los_migrate
```

Das Passwort wird ausschließlich am lokalen Terminal abgefragt.
Ohne explizite Migration bleibt Readiness 503. Danach unter
http://localhost:5006/admin anmelden und eigene Bezirksdaten bereitstellen.
Die Session-/Elternschlüssel werden weder ausgegeben noch bei Wiederholung ersetzt.
Der Elternlink steht in der geschützten Verwaltung.

BASE_URL passend zum tatsächlichen Proxy beziehungsweise Port setzen;
bei HTTPS setzt die App sichere Cookies automatisch.
Nur Host-Loopback wird veröffentlicht. Mehr zum optionalen Proxybetrieb und
den bestehenden Env-Namen in PLATFORM.md.

## Bezirksdaten und Browserassets

Das Image enthält eine leere GeoJSON-Datei unter /data/maps/bezirke.geojson.
Keine realen schulischen Bezirke, PDF-Vorlagen oder Kontaktdaten werden eingebaut.
Eigene Daten kontrolliert in diesen persistenten Pfad übernehmen.

import_streets.py kann auf ausdrücklichen Aufruf Geometrien aus Overpass holen.
BEZIRKE_CONFIG bestimmt die eigene Eingabekonfiguration, GEOJSON_PATH das Ausgabeziel.
Kein Netzwerkimport beim Start oder Image-Build.

Leaflet 1.9.4 liegt lokal samt Lizenz und Hashinventar unter static/vendor/leaflet.
TILE_URL bleibt standardmäßig leer; es gibt keine automatische Anfrage an einen
Kachelserver. Ein eigener Dienst kann über einen Same-Origin-Pfad ergänzt werden.
Die Rechte und Bedingungen eigener Karten-/OSM-Daten gesondert prüfen.

## Betrieb

- Die Anmeldung und ihre Eingangsbestätigung werden atomar gespeichert.
  Die begrenzte Mailqueue übersteht Neustarts; SMTP kann bei einer Absturzrace
  doppelt zustellen. Admin-Mailaktionen sind teilweise weiterhin synchron.
- Ohne SMTP_HOST werden Mails privat unter /data/mails abgelegt.
  Im Integrationstest ausschließlich lokaler Sink, kein externer Upstream.
- Elternlinks bleiben tokengebunden. Individuelle Maillinks nehmen mit dem
  bestehenden edit_token den gleichen Besitz auf; Tokens werden nicht rotiert.
- Verwaltungsformulare und JSON-Schreibaufrufe benötigen CSRF; Logout ist POST.
  Schreibende API-Clients müssen die Nonce aus der Elternseite übernehmen.
- DB-Migration ausschließlich mit pausierten Writern über los_migrate.
  Unbekannte Bestandsfelder werden nicht still verworfen.
- Sicherung über SQLite-.backup-API plus Maps-/Mailverzeichnisse und private
  Konfiguration/Schlüssel. Kein Kopieren der laufenden DBdatei.
- Stop und Deaktivierung behalten Volumes. Ein Jahreswechsel oder Löschen aller
  Daten ist eine gesonderte Aktion, kein automatischer Deployment-Schritt.

## Prüfung und native Altinstallation

Die Plattform liefert reproduzierbare Laufzeit-, Standalone-, Restore- und
Domainprüfungen mit synthetischen Daten. Browser-, Last-, ARM64- und öffentliche
DNS-/ACME-Abnahme stehen aus. Keine Produktionsfreigabe oder zentrale Sperrung.

Die Vorlagen unter deploy/ sind Herkunftsmaterial des nativen Betriebs. Die
automatische Servereinrichtung ist für den neuen Migrations-/Secretvertrag nicht
abgenommen. Bestehende produktive Dienste nicht blind mit ihr überschreiben.
Ein nativer Betrieb benötigt ebenfalls feste Schlüssel, persönliche Adminidentität,
BASE_URL, explizite Proxyadressen, Migration und die Gunicorn-Konfiguration mit
genau einem Dispatcher. Keine Produktivumstellung wurde ausgeführt.
