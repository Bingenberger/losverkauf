#!/usr/bin/env bash
# Ersteinrichtung auf dem Server (Ubuntu/Debian). Wird von "deploy/deploy.sh einrichten" per sudo aufgerufen,
# nachdem der Code nach ZIEL übertragen wurde. Kann gefahrlos erneut ausgeführt werden:
# eine vorhandene losverkauf.env und eine vorhandene nginx-Site bleiben unverändert.
#
#   sudo bash server-einrichten.sh ZIEL DOMAIN PORT DIENST TLS CERTBOT_EMAIL SUDO_OHNE_PASSWORT
set -euo pipefail
# Per SSH mitgeschickte Locale-Einstellungen (z. B. de_DE) sind auf dem Server oft nicht installiert → Perl-Warnungen
export LC_ALL=C.UTF-8

ZIEL=$1 DOMAIN=$2 PORT=$3 DIENST=$4 TLS=$5 CERTBOT_EMAIL=${6:-} SUDO_OHNE_PASSWORT=${7:-nein}
BENUTZER=${SUDO_USER:?Bitte mit sudo aufrufen}
VORLAGEN="$ZIEL/deploy"
ENV_DATEI="$ZIEL/losverkauf.env"

schritt() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
render() { sed -e "s|@ZIEL@|$ZIEL|g" -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@PORT@|$PORT|g" -e "s|@DIENST@|$DIENST|g" "$1"; }
zufall() { python3 -c 'import secrets, sys; print(secrets.token_urlsafe(int(sys.argv[1])))' "$1"; }

[[ $EUID -eq 0 ]] || { echo "Muss als root laufen (sudo)."; exit 1; }

schritt "Pakete installieren"
export DEBIAN_FRONTEND=noninteractive
PAKETE=(python3 python3-venv nginx rsync curl)
[[ $TLS == certbot ]] && PAKETE+=(certbot python3-certbot-nginx)
APT_LOG=/var/log/$DIENST-einrichten-apt.log
if ! { apt-get update -q && apt-get install -y -q "${PAKETE[@]}"; } >"$APT_LOG" 2>&1; then
  tail -n 30 "$APT_LOG"; echo "Paketinstallation fehlgeschlagen (vollständiges Log: $APT_LOG)"; exit 1
fi
echo "${PAKETE[*]} (Log: $APT_LOG)"

schritt "Port $PORT prüfen"
if ss -ltn "sport = :$PORT" | grep -q LISTEN && ! systemctl is-active --quiet "$DIENST"; then
  echo "Port $PORT ist schon von einem anderen Programm belegt:"
  ss -ltnp "sport = :$PORT"
  echo "Bitte in deploy/deploy.conf einen freien PORT wählen."
  exit 1
fi
echo "frei (oder bereits von $DIENST belegt)"

schritt "Verzeichnisse und Rechte"
# Code gehört dem Deploy-Benutzer; data/ gehört www-data (Dienst), die Gruppe des Deploy-Benutzers
# darf lesen und Sicherungen anlegen (setgid, damit neue Dateien die Gruppe erben).
mkdir -p "$ZIEL/data/backups"
chown www-data:"$BENUTZER" "$ZIEL/data" "$ZIEL/data/backups"
chmod 2770 "$ZIEL/data" "$ZIEL/data/backups"
echo "$ZIEL/data → www-data:$BENUTZER (2770)"

schritt "Python-Umgebung"
sudo -u "$BENUTZER" python3 -m venv "$ZIEL/venv"
sudo -u "$BENUTZER" "$ZIEL/venv/bin/pip" install -q --upgrade pip
sudo -u "$BENUTZER" "$ZIEL/venv/bin/pip" install -q -r "$ZIEL/requirements.txt"
"$ZIEL/venv/bin/python" -c 'import flask, gunicorn' && echo "Flask und Gunicorn installiert"

schritt "Konfiguration $ENV_DATEI"
NEUES_ADMIN_PASSWORT=""
if [[ -f $ENV_DATEI ]]; then
  echo "vorhanden – bleibt unverändert"
else
  NEUES_ADMIN_PASSWORT=$(zufall 12)
  [[ $TLS == keine ]] && COOKIE_SECURE=0 || COOKIE_SECURE=1
  sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$(zufall 32)|" \
      -e "s|^ELTERN_TOKEN=.*|ELTERN_TOKEN=$(zufall 12)|" \
      -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$NEUES_ADMIN_PASSWORT|" \
      -e "s|^DB_PATH=.*|DB_PATH=$ZIEL/data/losverkauf.db|" \
      -e "s|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=$COOKIE_SECURE|" \
      "$VORLAGEN/losverkauf.env.example" > "$ENV_DATEI"
  chown root:root "$ENV_DATEI"
  chmod 600 "$ENV_DATEI"
  echo "angelegt (Schlüssel, Elterntoken und Admin-Passwort zufällig erzeugt)"
fi

schritt "systemd-Dienst $DIENST"
render "$VORLAGEN/losverkauf.service" > "/etc/systemd/system/$DIENST.service"
systemctl daemon-reload
systemctl enable --quiet "$DIENST"
systemctl restart "$DIENST"
for _ in $(seq 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
  echo "Dienst antwortet nicht:"; journalctl -u "$DIENST" -n 40 --no-pager; exit 1
fi
echo "läuft auf 127.0.0.1:$PORT"

schritt "nginx (Reverse-Proxy)"
if [[ -d /etc/nginx/sites-available ]]; then
  SITE="/etc/nginx/sites-available/$DIENST"
  AKTIV="/etc/nginx/sites-enabled/$DIENST"
else
  SITE="/etc/nginx/conf.d/$DIENST.conf"
  AKTIV=""
fi
if [[ -f $SITE ]]; then
  echo "$SITE vorhanden – bleibt unverändert"
else
  render "$VORLAGEN/nginx-losverkauf.conf" > "$SITE"
  echo "$SITE angelegt"
fi
[[ -n $AKTIV ]] && ln -sfn "$SITE" "$AKTIV"
systemctl enable --quiet --now nginx
nginx -t
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  schritt "Firewall (ufw): http/https freigeben"
  ufw allow "Nginx Full"
fi

ZERTIFIKAT=nein
if [[ $TLS == certbot ]]; then
  schritt "TLS-Zertifikat für $DOMAIN (Let's Encrypt)"
  # Ein fehlendes Zertifikat bricht die Einrichtung nicht ab – es lässt sich später mit
  # "deploy/deploy.sh zertifikat" nachholen.
  if ! getent ahosts "$DOMAIN" >/dev/null; then
    echo "Übersprungen: Für $DOMAIN gibt es noch keinen DNS-Eintrag (A/AAAA auf diesen Server)."
  elif certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect; then
    systemctl reload nginx
    ZERTIFIKAT=ja
    echo "Automatische Verlängerung: $(systemctl is-enabled certbot.timer 2>/dev/null || echo 'certbot.timer nicht gefunden – bitte prüfen')"
  else
    echo "certbot ist fehlgeschlagen (Details siehe oben)."
  fi
fi

if [[ $SUDO_OHNE_PASSWORT == ja ]]; then
  schritt "sudo ohne Passwort für Neustart/Status/Log von $DIENST"
  SYSTEMCTL=$(command -v systemctl) JOURNALCTL=$(command -v journalctl)
  REGEL=$(mktemp)
  cat > "$REGEL" <<EOF
# Angelegt von losverkauf/deploy/server-einrichten.sh – nur genau diese Befehle
$BENUTZER ALL=(root) NOPASSWD: $SYSTEMCTL restart $DIENST, $SYSTEMCTL status $DIENST --no-pager, $JOURNALCTL -u $DIENST -n 60 --no-pager, $JOURNALCTL -u $DIENST -f --no-pager
EOF
  visudo -cf "$REGEL" >/dev/null
  install -m 440 -o root -g root "$REGEL" "/etc/sudoers.d/$DIENST"
  rm -f "$REGEL"
  echo "/etc/sudoers.d/$DIENST angelegt"
fi

schritt "Fertig"
[[ $TLS == keine ]] && SCHEMA=http || SCHEMA=https
ELTERN_TOKEN=$(sed -n 's/^ELTERN_TOKEN=//p' "$ENV_DATEI")
echo "Elternlink:  $SCHEMA://$DOMAIN/?t=$ELTERN_TOKEN"
echo "Verwaltung:  $SCHEMA://$DOMAIN/admin"
if [[ -n $NEUES_ADMIN_PASSWORT ]]; then
  echo "Admin-Passwort: $NEUES_ADMIN_PASSWORT   (bitte notieren)"
else
  echo "Admin-Passwort: steht in $ENV_DATEI  →  sudo grep ^ADMIN_PASSWORD= $ENV_DATEI"
fi
if [[ $TLS == certbot && $ZERTIFIKAT == nein ]]; then
  echo
  echo "ACHTUNG: Noch kein TLS-Zertifikat – die Seite ist nur per http erreichbar, und Anmeldungen"
  echo "funktionieren erst mit https. Sobald der DNS-Eintrag für $DOMAIN auf diesen Server zeigt:"
  echo "  deploy/deploy.sh zertifikat"
fi
if ! grep -q '^SMTP_HOST=.\+' "$ENV_DATEI"; then
  echo
  echo "Hinweis: Noch kein Mailserver eingetragen – Mails werden nur unter $ZIEL/data/mails/ abgelegt."
  echo "  sudo nano $ENV_DATEI    (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, MAIL_FROM …)"
  echo "  sudo systemctl restart $DIENST"
fi
