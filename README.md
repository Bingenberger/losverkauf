# Losverkauf St. Martin – Bezirkswahl auf der Karte

Eltern sehen die Verkaufsbezirke auf einer OpenStreetMap-Karte mit der aktuellen Belegung
(z. B. 1/2). Erstes Antippen zeigt einen Bezirk an, „Anmelden“ öffnet das Formular.
Eine Anmeldung umfasst 1–3 Personen (Name, Klasse, Telefon, E-Mail), die gemeinsam verkaufen,
und belegt einen Platz im Bezirk. Bezirke ohne Anmeldung sind orange, teilweise belegte gelb,
volle grau. Offene Anmeldungen lassen sich im selben Browser wieder zurückziehen.

Ablauf: Anmeldung → Eingangsbestätigung per Mail an alle Personen → Bestätigung in `/admin`
→ Mail mit der festen Zuteilung.

## Aufbau
- `config/bezirke.json` – Bezirke: ID, Name, Straßen, Obergrenze (`max`)
- `import_streets.py` – holt die Straßengeometrien per Overpass-API und schreibt `static/bezirke.geojson`
- `app.py` – Flask-App (Eltern-Seite, JSON-API, Admin)
- `mail.py` – Mailversand per SMTP (Testbetrieb ohne SMTP: Mails als Datei unter `data/mails/`)
- `templates/mail/` – Mailtexte (`eingang.txt`, `zuteilung.txt`; erste Zeile = Betreff), frei anpassbar
- `templates/`, `static/` – Frontend (Leaflet 1.9)
- `deploy/` – Deploy-Skript (`deploy.sh`), Server-Einrichtung, Vorlagen für systemd, nginx und `losverkauf.env`

## Lokal entwickeln
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
nano config/bezirke.json          # Bezirke pflegen, Straßennamen exakt wie in OSM
python3 import_streets.py         # → static/bezirke.geojson; meldet nicht gefundene Straßen
venv/bin/python app.py            # → http://127.0.0.1:5006 (ohne ELTERN_TOKEN ist die Seite offen)
```

## Server-Deploy (rsync + ssh)

Alles läuft über `deploy/deploy.sh` vom eigenen Rechner aus, im Projektordner.

### Voraussetzungen
- Server mit Ubuntu oder Debian, SSH-Zugang mit einem Benutzer mit sudo-Rechten (am besten per SSH-Schlüssel)
- DNS-Eintrag der Domain (z. B. `lose.gsndk.de`) zeigt auf den Server, Ports 80 und 443 sind erreichbar
- lokal: `rsync`, `ssh` und eine erzeugte `static/bezirke.geojson`

### Ersteinrichtung
```bash
cp deploy/deploy.conf.example deploy/deploy.conf
nano deploy/deploy.conf           # SERVER, DOMAIN, CERTBOT_EMAIL (ggf. PORT, TLS, SUDO_OHNE_PASSWORT)
deploy/deploy.sh einrichten
```
Das Skript
1. legt `/opt/losverkauf` an und überträgt den Code per rsync,
2. installiert Python, nginx und certbot,
3. legt die Python-Umgebung an und installiert die Abhängigkeiten,
4. erzeugt `/opt/losverkauf/losverkauf.env` mit zufälligem `SECRET_KEY`, Elterntoken und Admin-Passwort
   (nur root lesbar; das Passwort wird **einmal** angezeigt),
5. richtet den systemd-Dienst `losverkauf` ein (Gunicorn als `www-data`, nur auf `127.0.0.1:5010`),
6. richtet die nginx-Site ein und holt per certbot ein Let's-Encrypt-Zertifikat (inkl. Weiterleitung http → https),
7. zeigt am Ende Elternlink und Verwaltungsadresse.

Die Einrichtung kann erneut ausgeführt werden. Eine vorhandene `losverkauf.env` und eine vorhandene
nginx-Site werden dabei nicht überschrieben.

Danach den Mailversand eintragen (sonst Testbetrieb, Mails nur unter `data/mails/`):
```bash
ssh server
sudo nano /opt/losverkauf/losverkauf.env    # SMTP_HOST, SMTP_PORT, SMTP_SICHERHEIT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_REPLY_TO
sudo systemctl restart losverkauf
```
In `/admin` verschwindet dann der Hinweis „Testbetrieb“.

### Updates
```bash
python3 import_streets.py         # nur wenn sich Bezirke geändert haben
deploy/deploy.sh
```
Ablauf: lokale Prüfung → Sicherung der Datenbank auf dem Server (`data/backups/`, die letzten 30 bleiben) →
rsync (auf dem Server gelöschte Dateien werden entfernt; `venv/`, `data/`, `losverkauf.env` bleiben unangetastet,
siehe `deploy/rsync-exclude`) → Abhängigkeiten → Neustart → Prüfung über `/healthz`.

### Weitere Befehle
```bash
deploy/deploy.sh backup           # Datenbank sichern und nach ./backups/ herunterladen
deploy/deploy.sh status           # Dienststatus
deploy/deploy.sh logs             # Log live verfolgen (Strg+C)
```
Neustart, Status und Log brauchen sudo. Mit `SUDO_OHNE_PASSWORT=ja` legt die Einrichtung eine Regel in
`/etc/sudoers.d/losverkauf` an, die genau diese Befehle ohne Passwort erlaubt – sonst fragt sudo jeweils nach.

### nginx als Reverse-Proxy
```
Browser ──https──▶ nginx :443 ──http──▶ Gunicorn 127.0.0.1:5010 (Benutzer www-data) ──▶ data/losverkauf.db
                   │ TLS-Zertifikat (certbot)
                   │ /static/ direkt aus /opt/losverkauf/static
                   │ Rate-Limit für /api/anmeldung und /admin/login
```
- Gunicorn ist nur lokal erreichbar; von außen geht jede Anfrage über nginx.
  nginx gibt Schema und Host per `X-Forwarded-*` weiter, die App übernimmt sie (`ProxyFix`), damit Links
  und der Elternlink in `/admin` mit `https://…` erzeugt werden.
- Dateien auf dem Server:

  | Datei | Inhalt |
  |---|---|
  | `/etc/nginx/sites-available/losverkauf` (+ Link in `sites-enabled`) | nginx-Site, Vorlage `deploy/nginx-losverkauf.conf` |
  | `/etc/systemd/system/losverkauf.service` | Dienst, Vorlage `deploy/losverkauf.service` (wird bei jeder Einrichtung aktualisiert) |
  | `/opt/losverkauf/losverkauf.env` | Geheimnisse und Einstellungen (root, 600) |
  | `/opt/losverkauf/data/` | Datenbank, Sicherungen, abgelegte Mails (www-data, Gruppe des Deploy-Benutzers) |
  | `/var/log/nginx/losverkauf.access.log`, `.error.log` | nginx-Logs; App-Log über `journalctl -u losverkauf` |

- nginx-Site ändern: `sudo nano /etc/nginx/sites-available/losverkauf`, dann
  `sudo nginx -t && sudo systemctl reload nginx`. Andere Sites auf dem Server bleiben unberührt.
- Zertifikat: certbot verlängert automatisch (`certbot.timer`); Test mit `sudo certbot renew --dry-run`.
- Gibt es auf dem Server schon ein Zertifikat (z. B. Wildcard für `*.gsndk.de`): `TLS=keine` setzen, nach der
  Einrichtung in der Site `listen 443 ssl;` plus `ssl_certificate`/`ssl_certificate_key` ergänzen, eine
  Weiterleitung von Port 80 einrichten und in `losverkauf.env` `SESSION_COOKIE_SECURE=1` setzen.
- Port 5010 schon belegt? `PORT` in `deploy/deploy.conf` ändern – die Einrichtung prüft das vorher.

## Betrieb
- Elternlink: `https://lose.gsndk.de/?t=<ELTERN_TOKEN>` – steht auch oben in `/admin`.
  Nach dem ersten Aufruf wird das Token in einem Cookie gehalten, die URL wird bereinigt.
- Admin unter `/admin` (Passwort `ADMIN_PASSWORD`): oben die noch zu bestätigenden Anmeldungen,
  „Bestätigen & Zuteilung mailen“ verschickt die feste Zuteilung. Pro Anmeldung ist sichtbar, ob
  Eingangs- und Zuteilungsmail angekommen sind (bei Fehlern mit Meldung und „erneut mailen“).
  Außerdem: manuell anmelden, verschieben (setzt die Bestätigung zurück), löschen,
  Obergrenze je Bezirk ändern, Bezirk sperren, Test-Mail verschicken.
- Listen: **PDF** („PDF-Liste“, nach Bezirk sortiert, mit Straßen und Kontaktdaten – zum Ausdrucken;
  wahlweise alle oder nur die bestätigten Anmeldungen) und **CSV** (eine Zeile pro Person, Semikolon,
  öffnet direkt in Excel/LibreOffice).
- Bestätigte Anmeldungen können Eltern nicht mehr selbst zurückziehen (Hinweis: auf die Mail antworten).
- Neues Jahr: auf dem Server `data/losverkauf.db` löschen (vorher `deploy/deploy.sh backup`), in
  `losverkauf.env` ein neues `ELTERN_TOKEN` setzen, `sudo systemctl restart losverkauf`.

## Hinweise
- Straßen, die in OSM anders heißen (z. B. „Str.“ vs. „Straße“), werden beim Import gemeldet.
- `ortsteil` im Bezirk: Es werden nur Straßen innerhalb dieser Ortsteilgrenze aus OSM verwendet
  (wichtig, wenn ein Straßenname in mehreren Ortsteilen vorkommt).
- Feldwege (`track`, `path` …) einer Straße werden weggelassen, solange die Straße auch normale Abschnitte hat.
- Lange Straßen lassen sich aufteilen: statt des Namens einen Abschnitt zwischen zwei Querstraßen angeben,
  z. B. `{"name": "Hauptstraße", "von": "Weidenstraße", "bis": "Pastor-Grimm-Straße"}`.
  Auf der Karte erscheint nur dieser Abschnitt, in der Liste „Hauptstraße (Weidenstraße – Pastor-Grimm-Straße)“.
  Statt einer Querstraße geht auch `"Bahn"` (Bahnübergang). Optional `"zusatz"` für die Anzeige,
  z. B. `{"name": "Niederkasseler Straße", "zusatz": "südliche Seite"}`.
  Querstraßen, die die Straße nicht treffen, meldet der Import.
- Bezirke sind als Straßenzüge (Linien) dargestellt, nicht als Flächen – das passt zur Straßenliste
  und lässt sich ohne Zeichnen pflegen.
- Leaflet wird von unpkg.com geladen. Für einen komplett externen-frei betriebenen Dienst
  `leaflet.js`/`leaflet.css` nach `static/` kopieren und die beiden Links in `templates/base.html`
  und `templates/index.html` anpassen. Die Kartenkacheln kommen von openstreetmap.org
  (Nutzungsbedingungen: für eine Schule in diesem Umfang unproblematisch; Datenschutzhinweis ergänzen).
- Daten: Name, Klasse, Telefon, E-Mail liegen nur in der lokalen SQLite. Nach dem Martinszug löschen.
