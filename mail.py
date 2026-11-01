"""
Mailversand per SMTP.

Umgebungsvariablen (siehe deploy/losverkauf.env.example):
  SMTP_HOST        SMTP-Server; leer = Testbetrieb (Mails nur als .eml-Datei unter data/mails/)
  SMTP_PORT        Standard 587 (starttls) bzw. 465 (ssl)
  SMTP_SICHERHEIT  starttls (Standard) | ssl | keine
  SMTP_USER, SMTP_PASSWORD
  MAIL_FROM        Absender, z. B. "Losverkauf St. Martin <lose@example.de>"
  MAIL_REPLY_TO    optional: Antwortadresse
"""
import os
import secrets
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_SICHERHEIT = os.environ.get("SMTP_SICHERHEIT", "starttls").lower()
SMTP_PORT = int(os.environ.get("SMTP_PORT") or (465 if SMTP_SICHERHEIT == "ssl" else 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM") or SMTP_USER or "losverkauf@localhost"
MAIL_REPLY_TO = os.environ.get("MAIL_REPLY_TO", "")


def testbetrieb() -> bool:
    return not SMTP_HOST


def einstellungen() -> dict:
    """Aktuelle Mail-Einstellungen zur Anzeige in der Verwaltung (ohne Passwort)."""
    return {"host": SMTP_HOST, "port": SMTP_PORT, "sicherheit": SMTP_SICHERHEIT, "user": SMTP_USER,
            "passwort_gesetzt": bool(SMTP_PASSWORD), "absender": MAIL_FROM, "antwort_an": MAIL_REPLY_TO}


def senden(empfaenger: list[str], betreff: str, text: str, ablage: Path) -> str | None:
    """Verschickt eine Text-Mail. Rückgabe: None bei Erfolg, sonst die Fehlermeldung."""
    msg = EmailMessage()
    msg["Subject"] = betreff
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(empfaenger)
    if MAIL_REPLY_TO:
        msg["Reply-To"] = MAIL_REPLY_TO
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=MAIL_FROM.rpartition("@")[2].strip("> ") or None)
    msg.set_content(text)

    if testbetrieb():
        ablage.mkdir(parents=True, exist_ok=True)
        (ablage / f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.eml").write_bytes(bytes(msg))
        return None
    try:
        kontext = ssl.create_default_context()
        if SMTP_SICHERHEIT == "ssl":
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=kontext, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        with server:
            if SMTP_SICHERHEIT == "starttls":
                server.starttls(context=kontext)
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return None
    except (smtplib.SMTPException, OSError) as e:
        return f"{type(e).__name__}: {e}"
