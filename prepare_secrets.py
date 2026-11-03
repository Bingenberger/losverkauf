"""Create private standalone projections; never print or replace credentials."""
import argparse
import getpass
import os
from pathlib import Path
import secrets
import stat
from werkzeug.security import generate_password_hash, check_password_hash

def prepare(directory, username, password):
    if not username.strip() or len(username)>80 or not 12<=len(password)<=1024:
        raise ValueError("Personal username and password required")
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&511!=0o700:
        raise ValueError("Private 0700 directory owned by this user required")
    name=directory/"admin-name"
    hashed=directory/"admin-hash"
    if name.exists() and name.read_text().strip()!=username.strip():
        raise ValueError("Existing personal identity differs; refusing replacement")
    if hashed.exists() and not check_password_hash(hashed.read_text().strip(),password):
        raise ValueError("Existing password differs; refusing replacement")
    values={"session-key":secrets.token_urlsafe(48),"parent-token":secrets.token_urlsafe(48),
            "admin-name":username.strip(),"admin-hash":generate_password_hash(password)}
    for key,value in values.items():
        path=directory/key
        try:
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o444)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or not path.read_text().strip():
                raise ValueError("Invalid existing projection")
        else:
            os.fchmod(fd,0o444)
            with os.fdopen(fd,"w") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--admin-user",required=True)
    args=parser.parse_args()
    try:
        with open("/dev/tty","r+") as terminal:
            password=getpass.getpass("Persönliches Passwort (mindestens 12 Zeichen): ",stream=terminal)
            if password!=getpass.getpass("Passwort wiederholen: ",stream=terminal):
                raise ValueError("Password confirmation differs")
            prepare(args.directory,args.admin_user,password)
            terminal.write("Private Erstkonfiguration bereit; keine Schlüsselwerte ausgegeben.\n")
    except Exception:
        raise SystemExit("Lokale Einrichtung fehlgeschlagen; Terminal, privates Verzeichnis und vorhandene Identität prüfen.")
