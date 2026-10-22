"""Command line: python -m studio run | set-plan | reset-password | create-account | grant-admin | revoke-admin | get-scene-model"""
import argparse
import getpass
import sys
import time
from pathlib import Path

from . import config, db, security


def cmd_run(a):
    import uvicorn

    from .server import create_app

    if a.data:
        config.settings.data_dir = Path(a.data)
    if a.no_signup:
        config.settings.allow_signup = False
    for k in ("ftp_port", "ftp_host", "ftp_passive", "ftp_cert", "ftp_key"):
        if getattr(a, k) not in (None, ""):
            setattr(config.settings, k, getattr(a, k))
    if a.ftp_require_tls:
        config.settings.ftp_require_tls = True
    ssl, scheme = {}, "http"
    from netutil import ensure_cert, lan_ip  # shared with app.py; doesn't load dlib

    ip = a.ip or lan_ip()
    if a.https:
        crt, key = ensure_cert(config.settings.data_dir / "certs", ip)
        ssl, scheme = {"ssl_certfile": str(crt), "ssl_keyfile": str(key)}, "https"
    if a.public_url:
        config.settings.public_url = a.public_url.rstrip("/")
    elif not config.settings.public_url:  # share links must open on a phone, so use the LAN address, not "localhost"
        config.settings.public_url = f"{scheme}://{ip}:{a.port}"
    app = create_app(workers=a.workers)
    print(f"\nStudio dashboard:  {scheme}://{ip}:{a.port}")
    print(f"Share links look like: {config.settings.public_url}/e/<event>")
    print(f"Data folder: {config.settings.data_dir}")
    if config.settings.ftp_port:
        tls = " with FTPS" if config.settings.ftp_cert else " (plain FTP: add --ftp-cert for FTPS)"
        print(f"Camera upload: FTP on port {config.settings.ftp_port}{tls}")
    if not config.settings.allow_signup:
        print("Sign-ups are closed. Create accounts with: python -m studio create-account")
    from . import scenes

    if not scenes.installed():
        print("Search by description is off until its model is downloaded: python -m studio get-scene-model")
    print("Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", **ssl)


def cmd_get_scene_model(a):
    from . import scenes

    print(f"Downloading the search-by-description model (about 156 MB, once) to {scenes.MODEL_DIR}")
    try:
        scenes.download()
    except Exception as e:
        sys.exit(f"Download failed: {e}")
    if not scenes.installed():
        sys.exit("The files are here, but onnxruntime or tokenizers is missing: pip install -r requirements.txt")
    print("Done. Restart the app; photos are indexed in the background.")


def _open_db(a):
    if a.data:
        config.settings.data_dir = Path(a.data)
    db.init()


def cmd_set_plan(a):
    _open_db(a)
    if a.plan not in config.PLANS:
        sys.exit(f"Unknown plan {a.plan!r}. Choose one of: {', '.join(config.PLANS)}")
    r = db.q1("SELECT account_id FROM users WHERE email=?", (a.email.lower(),))
    if not r:
        sys.exit(f"No user with email {a.email}")
    db.run("UPDATE accounts SET plan=? WHERE id=?", (a.plan, r["account_id"]))
    print(f"Account of {a.email} is now on the {config.PLANS[a.plan]['label']} plan.")


def cmd_reset_password(a):
    _open_db(a)
    r = db.q1("SELECT id FROM users WHERE email=?", (a.email.lower(),))
    if not r:
        sys.exit(f"No user with email {a.email}")
    pw = a.password or getpass.getpass("New password (min 8 characters): ")
    if len(pw) < 8:
        sys.exit("Password must be at least 8 characters.")
    db.run("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(pw), r["id"]))
    db.run("DELETE FROM sessions WHERE user_id=?", (r["id"],))  # sign them out everywhere
    print(f"Password for {a.email} changed. They have been signed out on all devices.")


def cmd_admin(a, on: bool):
    _open_db(a)
    n = db.run("UPDATE users SET platform_admin=? WHERE email=?", (1 if on else 0, a.email.lower()))
    if not n:
        sys.exit(f"No user with email {a.email}")
    if on:
        print(f"{a.email} is now a platform admin. Open <your address>/admin after signing in.")
    else:
        print(f"{a.email} is no longer a platform admin.")


def cmd_create_account(a):
    _open_db(a)
    pw = a.password or getpass.getpass("Password (min 8 characters): ")
    if len(pw) < 8:
        sys.exit("Password must be at least 8 characters.")
    now = int(time.time())
    try:
        with db.tx():
            aid = db.ex("INSERT INTO accounts(company, brand_color, plan, created_at) VALUES(?,?,?,?)",
                        (a.company, config.DEFAULT_BRAND, a.plan, now))
            db.ex("INSERT INTO users(account_id, email, name, pw_hash, role, created_at) VALUES(?,?,?,?,?,?)",
                  (aid, a.email.lower(), a.name or "", security.hash_password(pw), "owner", now))
    except Exception as e:
        sys.exit(f"Could not create the account: {e}")
    print(f"Created {a.company!r} for {a.email} on the {a.plan} plan.")


def main():
    ap = argparse.ArgumentParser(prog="python -m studio", description="Photo Finder Studio")
    ap.add_argument("--data", help="data folder (default: ./data, or $PF_DATA)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start the web app")
    r.add_argument("--host", default="0.0.0.0")
    r.add_argument("--port", type=int, default=8000)
    r.add_argument("--ip", help="address used in share links when --public-url is not set (default: auto-detected)")
    r.add_argument("--public-url", help="public address of the site, for example https://photos.example.com")
    r.add_argument("--https", action="store_true", help="self-signed https (testing only; use a real certificate in production)")
    r.add_argument("--workers", type=int, default=None, help="photo-processing processes (default: CPU cores - 1)")
    r.add_argument("--no-signup", action="store_true", help="close public sign-up")
    r.add_argument("--ftp-port", type=int, help="turn on camera upload over FTP on this port (e.g. 21 or 2121)")
    r.add_argument("--ftp-host", help="address cameras connect to (default: the public URL's host)")
    r.add_argument("--ftp-passive", help="passive data ports, e.g. 60000-60099 (open them in the firewall)")
    r.add_argument("--ftp-cert", help="PEM certificate for FTPS")
    r.add_argument("--ftp-key", help="PEM private key for FTPS")
    r.add_argument("--ftp-require-tls", action="store_true", help="refuse plain FTP (only cameras with FTPS)")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("set-plan", help="move an account to another plan")
    s.add_argument("email")
    s.add_argument("plan", choices=list(config.PLANS))
    s.set_defaults(fn=cmd_set_plan)

    rp = sub.add_parser("reset-password", help="set a new password for a user who is locked out")
    rp.add_argument("email")
    rp.add_argument("--password")
    rp.set_defaults(fn=cmd_reset_password)

    c = sub.add_parser("create-account", help="create a company account from the terminal")
    c.add_argument("--company", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--name")
    c.add_argument("--password")
    c.add_argument("--plan", default="free", choices=list(config.PLANS))
    c.set_defaults(fn=cmd_create_account)

    g = sub.add_parser("grant-admin", help="make a user a platform admin (the product-owner dashboard at /admin)")
    g.add_argument("email")
    g.set_defaults(fn=lambda a: cmd_admin(a, True))

    v = sub.add_parser("revoke-admin", help="remove platform-admin access from a user")
    v.add_argument("email")
    v.set_defaults(fn=lambda a: cmd_admin(a, False))

    m = sub.add_parser("get-scene-model", help="download the model for search by description (about 156 MB, once)")
    m.set_defaults(fn=cmd_get_scene_model)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
