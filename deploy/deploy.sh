#!/usr/bin/env bash
# Deploy des Losverkaufs per rsync + ssh. Aufruf im Projektordner:
#
#   deploy/deploy.sh einrichten   Ersteinrichtung des Servers (Pakete, venv, systemd, nginx, TLS)
#   deploy/deploy.sh              Neue Version übertragen: Sicherung, rsync, Abhängigkeiten, Neustart, Prüfung
#   deploy/deploy.sh zertifikat   TLS-Zertifikat (Let's Encrypt) holen, z. B. wenn der DNS-Eintrag erst später gesetzt wurde
#   deploy/deploy.sh backup       Datenbank auf dem Server sichern und nach ./backups/ herunterladen
#   deploy/deploy.sh status       Status des Dienstes
#   deploy/deploy.sh logs         Log des Dienstes live verfolgen (Strg+C beendet)
#
# Einstellungen stehen in deploy/deploy.conf (Vorlage: deploy/deploy.conf.example).
set -euo pipefail
cd "$(dirname "$0")/.."

KONF=deploy/deploy.conf
if [[ ! -f $KONF ]]; then
  echo "deploy/deploy.conf fehlt. Bitte anlegen:"
  echo "  cp deploy/deploy.conf.example deploy/deploy.conf && nano deploy/deploy.conf"
  exit 1
fi
# shellcheck source=/dev/null
source "$KONF"
: "${SERVER:?SERVER in deploy/deploy.conf setzen}" "${DOMAIN:?DOMAIN in deploy/deploy.conf setzen}"
ZIEL=${ZIEL:-/opt/losverkauf}
PORT=${PORT:-5010}
DIENST=${DIENST:-losverkauf}
TLS=${TLS:-certbot}
CERTBOT_EMAIL=${CERTBOT_EMAIL:-}
SUDO_OHNE_PASSWORT=${SUDO_OHNE_PASSWORT:-nein}
[[ $TLS == keine ]] && SCHEMA=http || SCHEMA=https

schritt() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fehler() { printf '\033[31mFehler:\033[0m %s\n' "$*" >&2; exit 1; }
remote() { ssh "$SERVER" "$@"; }
remote_tty() { ssh -t "$SERVER" "$@"; }  # mit Terminal, damit sudo nach dem Passwort fragen kann

lokal_pruefen() {
  schritt "Lokale Prüfung"
  [[ -s static/bezirke.geojson ]] || fehler "static/bezirke.geojson fehlt – zuerst: python3 import_streets.py"
  local py=python3
  [[ -x venv/bin/python ]] && py=venv/bin/python
  "$py" -m py_compile app.py mail.py import_streets.py || fehler "Python-Dateien enthalten Syntaxfehler"
  "$py" -c 'import json; json.load(open("static/bezirke.geojson", encoding="utf-8"))' || fehler "bezirke.geojson ist kein gültiges JSON"
  echo "ok"
}

uebertragen() {
  schritt "Dateien übertragen → $SERVER:$ZIEL"
  # --delete entfernt auf dem Server, was lokal gelöscht wurde; alles aus rsync-exclude
  # (venv, data, losverkauf.env …) bleibt auf dem Server unangetastet.
  rsync -az --delete --chmod=D755,F644 --exclude-from=deploy/rsync-exclude \
        --info=name1,stats1 ./ "$SERVER:$ZIEL/"
}

sichern() {
  schritt "Datenbank auf dem Server sichern"
  # sqlite-Backup-API: konsistente Kopie auch während der Dienst läuft; die letzten 30 Sicherungen bleiben
  remote "cd '$ZIEL' && if [ -f data/losverkauf.db ]; then
      f=data/backups/losverkauf-\$(date +%Y%m%d-%H%M%S).db
      venv/bin/python -c 'import sqlite3, sys; q = sqlite3.connect(sys.argv[1]); z = sqlite3.connect(sys.argv[2]); q.backup(z); z.close(); q.close()' data/losverkauf.db \"\$f\"
      echo \"\$f\"
      ls -1t data/backups/*.db | tail -n +31 | xargs -r rm --
    else
      echo 'noch keine Datenbank vorhanden'
    fi"
}

pruefen() {
  schritt "Erreichbarkeit prüfen"
  if ! remote "for i in \$(seq 30); do curl -fsS http://127.0.0.1:$PORT/healthz >/dev/null 2>&1 && exit 0; sleep 0.5; done; exit 1"; then
    echo "Der Dienst antwortet nicht. Letzte Log-Zeilen:"
    remote_tty "sudo journalctl -u '$DIENST' -n 60 --no-pager"
    exit 1
  fi
  echo "Dienst: ok (127.0.0.1:$PORT)"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$SCHEMA://$DOMAIN/healthz" || true)
  if [[ $code == 200 ]]; then
    echo "Öffentlich: ok ($SCHEMA://$DOMAIN)"
  else
    echo "Öffentlich: $SCHEMA://$DOMAIN/healthz antwortet mit '$code' – DNS, Firewall oder nginx prüfen."
  fi
}

einrichten() {
  [[ $TLS == certbot && -z $CERTBOT_EMAIL ]] && fehler "Für TLS=certbot bitte CERTBOT_EMAIL in deploy/deploy.conf setzen"
  lokal_pruefen
  schritt "Zielverzeichnis $ZIEL anlegen (sudo)"
  remote_tty "sudo mkdir -p '$ZIEL' && sudo chown \"\$(id -un):\$(id -gn)\" '$ZIEL'"
  uebertragen
  schritt "Server einrichten (sudo)"
  remote_tty "sudo bash '$ZIEL/deploy/server-einrichten.sh' '$ZIEL' '$DOMAIN' '$PORT' '$DIENST' '$TLS' '$CERTBOT_EMAIL' '$SUDO_OHNE_PASSWORT'"
  pruefen
}

zertifikat() {
  [[ -n $CERTBOT_EMAIL ]] || fehler "Bitte CERTBOT_EMAIL in deploy/deploy.conf setzen"
  schritt "DNS prüfen"
  local ip
  ip=$(remote "getent ahosts '$DOMAIN' | awk 'NR==1 {print \$1}'" || true)
  [[ -n $ip ]] || fehler "Für $DOMAIN gibt es noch keinen DNS-Eintrag. Bitte beim DNS-Anbieter einen A-Eintrag auf den Server anlegen und einige Minuten warten."
  echo "$DOMAIN → $ip"
  schritt "TLS-Zertifikat holen (sudo)"
  remote_tty "sudo certbot --nginx -d '$DOMAIN' --non-interactive --agree-tos -m '$CERTBOT_EMAIL' --redirect && sudo systemctl reload nginx"
  pruefen
}

aktualisieren() {
  lokal_pruefen
  remote "test -x '$ZIEL/venv/bin/gunicorn'" || fehler "Server ist noch nicht eingerichtet – zuerst: deploy/deploy.sh einrichten"
  sichern
  uebertragen
  schritt "Abhängigkeiten aktualisieren"
  remote "'$ZIEL/venv/bin/pip' install -q -r '$ZIEL/requirements.txt'"
  echo "ok"
  schritt "Dienst neu starten"
  remote_tty "sudo systemctl restart '$DIENST'"
  pruefen
}

backup() {
  sichern
  schritt "Sicherungen herunterladen → ./backups/"
  mkdir -p backups
  chmod 700 backups
  rsync -az --chmod=D700,F600 --info=name1 "$SERVER:$ZIEL/data/backups/" backups/
  echo "Hinweis: Die Sicherungen enthalten personenbezogene Daten – nach dem Martinszug löschen."
}

case "${1:-}" in
  "")         aktualisieren ;;
  einrichten) einrichten ;;
  zertifikat) zertifikat ;;
  backup)     backup ;;
  status)     remote_tty "sudo systemctl status '$DIENST' --no-pager" ;;
  logs)       remote_tty "sudo journalctl -u '$DIENST' -f --no-pager" ;;
  *)          sed -n '2,11p' "$0"; exit 1 ;;
esac
