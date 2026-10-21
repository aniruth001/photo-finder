"""Product-owner (platform admin) API: every company on the platform, their usage, plans and suspension.

Only users with users.platform_admin = 1 get in (python -m studio grant-admin <email>). Everyone else gets a 404,
so the admin area doesn't reveal that it exists. Every change is written to admin_log.
"""
import csv
import io
import shutil
import time
from datetime import datetime
from typing import Optional

from fastapi import Depends, Request, Response
from pydantic import BaseModel, Field

from . import branding, config, db, roles, security
from .camera import server_info
from .server import COOKIE, err, plan_of

DAY = 86400

ACCOUNT_SQL = """SELECT a.id, a.company, a.plan, a.brand_color, a.timezone, a.created_at, a.disabled_at, a.disabled_reason, a.logo,
  (SELECT email FROM users WHERE account_id=a.id ORDER BY role != 'owner', id LIMIT 1) AS owner_email,
  (SELECT name FROM users WHERE account_id=a.id ORDER BY role != 'owner', id LIMIT 1) AS owner_name,
  (SELECT COUNT(*) FROM users WHERE account_id=a.id) AS n_users,
  (SELECT COUNT(*) FROM invites WHERE account_id=a.id AND accepted_at IS NULL AND expires_at > strftime('%s','now'))
    AS n_invites,
  (SELECT MAX(last_seen_at) FROM users WHERE account_id=a.id) AS last_seen,
  (SELECT COUNT(*) FROM clients WHERE account_id=a.id) AS n_clients,
  (SELECT COUNT(*) FROM events WHERE account_id=a.id) AS n_events,
  (SELECT COUNT(*) FROM events WHERE account_id=a.id AND status='published') AS n_published,
  (SELECT COUNT(*) FROM photos WHERE account_id=a.id) AS n_photos,
  (SELECT COALESCE(SUM(size), 0) FROM photos WHERE account_id=a.id)
    + (SELECT COALESCE(SUM(size), 0) FROM videos WHERE account_id=a.id) AS storage,
  (SELECT COUNT(*) FROM photos WHERE account_id=a.id AND status='failed') AS n_failed,
  (SELECT COUNT(*) FROM guest_searches s JOIN events e ON e.id=s.event_id WHERE e.account_id=a.id) AS n_searches
  FROM accounts a """

SORTS = {"created": "created_at", "company": "company", "storage": "storage", "photos": "photos", "users": "users",
         "events": "events", "last_seen": "last_seen", "searches": "searches"}


class DisableIn(BaseModel):
    reason: str = Field("", max_length=300)


class PlanIn(BaseModel):
    plan: str = Field(max_length=40)


class FtpIn(BaseModel):
    allowed: bool


def _pct(used, limit):
    return None if limit is None else round(used / limit * 100, 1) if limit else 100.0


def account_out(r) -> dict:
    plan = plan_of(r["plan"])
    now = time.time()
    use = {"events": r["n_events"], "photos": r["n_photos"], "storage": r["storage"]}
    pct = {k: _pct(use[k], plan[k]) for k in use}
    return {
        "id": r["id"], "company": r["company"], "owner_email": r["owner_email"] or "", "owner_name": r["owner_name"] or "",
        "plan": r["plan"], "plan_label": plan["label"], "price": plan.get("price", 0),
        "status": "suspended" if r["disabled_at"] else "active",
        "disabled_at": r["disabled_at"], "disabled_reason": r["disabled_reason"],
        "active_30d": bool(r["last_seen"] and r["last_seen"] > now - 30 * DAY),
        "created_at": r["created_at"], "last_seen": r["last_seen"], "timezone": r["timezone"],
        "brand_color": r["brand_color"], "logo_url": branding.logo_url(r["logo"]),
        "users": r["n_users"], "invites": r["n_invites"],
        "clients": r["n_clients"], "events": r["n_events"],
        "published": r["n_published"], "photos": r["n_photos"], "storage": r["storage"], "failed": r["n_failed"],
        "searches": r["n_searches"],
        "limits": {k: plan.get(k) for k in ("events", "photos", "storage", "users")}, "usage_pct": pct,
        "near_limit": any(p is not None and p >= 80 for p in pct.values()),
    }


def _months(n=12):
    """The last n calendar months as 'YYYY-MM', oldest first (server's local calendar)."""
    d = datetime.now()
    y, m, out = d.year, d.month, []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _csv_cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # stop spreadsheets running it as a formula


def mount_admin(app, user_dep):
    def admin_dep(u: dict = Depends(user_dep)) -> dict:
        if not u["platform_admin"]:
            raise err(404, "Not found.", "not_found")
        return u

    def log(u, account_id, company, action, detail=""):
        db.ex("""INSERT INTO admin_log(admin_user_id, admin_email, account_id, company, action, detail, created_at)
                 VALUES(?,?,?,?,?,?,?)""", (u["user_id"], u["email"], account_id, company, action, detail, int(time.time())))

    def all_accounts():
        return [account_out(r) for r in db.q(ACCOUNT_SQL + "ORDER BY a.id")]

    def get_account(aid):
        r = db.q1(ACCOUNT_SQL + "WHERE a.id=?", (aid,))
        if not r:
            raise err(404, "Company not found.", "not_found")
        return account_out(r)

    def log_rows(where="", args=(), limit=50, offset=0):
        rows = db.q(f"SELECT * FROM admin_log {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ overview (the ERP dashboard)
    @app.get("/api/admin/overview")
    def overview(u: dict = Depends(admin_dep)):
        accts = all_accounts()
        now = time.time()
        live = [a for a in accts if a["status"] == "active"]
        plans = {k: {"label": p["label"], "price": p.get("price", 0), "accounts": 0, "suspended": 0}
                 for k, p in config.PLANS.items()}
        for a in accts:
            row = plans.setdefault(a["plan"], {"label": a["plan_label"], "price": a["price"], "accounts": 0, "suspended": 0})
            row["accounts" if a["status"] == "active" else "suspended"] += 1
        months = _months()
        signups = {m: 0 for m in months}
        for a in accts:
            m = datetime.fromtimestamp(a["created_at"]).strftime("%Y-%m")
            if m in signups:
                signups[m] += 1
        start = int(datetime.strptime(months[0] + "-01", "%Y-%m-%d").timestamp())
        searches = {m: 0 for m in months}
        for r in db.q("""SELECT strftime('%Y-%m', created_at, 'unixepoch', 'localtime') m, COUNT(*) c
                         FROM guest_searches WHERE created_at >= ? GROUP BY m""", (start,)):
            if r["m"] in searches:
                searches[r["m"]] = r["c"]
        s30 = db.q1("SELECT COUNT(*) c, COALESCE(SUM(matches > 0), 0) f FROM guest_searches WHERE created_at >= ?",
                    (int(now - 30 * DAY),))
        q = db.q1("""SELECT SUM(status IN ('uploading','uploaded','processing')) busy, SUM(status='failed') failed
                     FROM photos""")
        try:
            du = shutil.disk_usage(config.settings.data_dir)
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            disk = None
        return {
            "currency": config.CURRENCY,
            "companies": {"total": len(accts), "active": len(live), "suspended": len(accts) - len(live),
                          "active_30d": sum(a["active_30d"] for a in live),
                          "new_30d": sum(a["created_at"] > now - 30 * DAY for a in accts),
                          "paying": sum(a["price"] > 0 for a in live)},
            "revenue_month": sum(a["price"] for a in live),
            "storage": sum(a["storage"] for a in accts), "photos": sum(a["photos"] for a in accts),
            "events": sum(a["events"] for a in accts), "published": sum(a["published"] for a in accts),
            "clients": sum(a["clients"] for a in accts), "users": sum(a["users"] for a in accts),
            "searches": {"total": sum(a["searches"] for a in accts), "last_30d": s30["c"], "found_30d": s30["f"]},
            "queue": {"processing": q["busy"] or 0, "failed": q["failed"] or 0},
            "disk": disk,
            "plans": [{"plan": k, **v} for k, v in plans.items()],
            "signups_by_month": [{"month": m, "count": signups[m]} for m in months],
            "searches_by_month": [{"month": m, "count": searches[m]} for m in months],
            "top_storage": sorted(({"id": a["id"], "company": a["company"], "storage": a["storage"],
                                    "limit": a["limits"]["storage"]} for a in accts),
                                  key=lambda x: -x["storage"])[:8],
            "near_limit": [{"id": a["id"], "company": a["company"], "usage_pct": a["usage_pct"], "plan_label": a["plan_label"]}
                           for a in live if a["near_limit"]][:20],
            "recent_log": log_rows(limit=10),
        }

    # ------------------------------------------------------------------ companies
    def filtered(q, status, plan, sort, order):
        accts = all_accounts()
        if q:
            ql = q.strip().lower()
            accts = [a for a in accts if ql in a["company"].lower() or ql in a["owner_email"].lower()]
        if status in ("active", "suspended"):
            accts = [a for a in accts if a["status"] == status]
        elif status == "inactive":  # not suspended, but nobody signed in for 30 days
            accts = [a for a in accts if a["status"] == "active" and not a["active_30d"]]
        elif status == "near_limit":
            accts = [a for a in accts if a["near_limit"]]
        if plan:
            accts = [a for a in accts if a["plan"] == plan]
        key = SORTS.get(sort, "created_at")
        have = [a for a in accts if a[key] is not None]
        have.sort(key=lambda a: a[key].lower() if isinstance(a[key], str) else a[key], reverse=order != "asc")
        return have + [a for a in accts if a[key] is None]  # never-seen accounts go last either way

    @app.get("/api/admin/accounts")
    def list_accounts(q: str = "", status: str = "", plan: str = "", sort: str = "created", order: str = "desc",
                      offset: int = 0, limit: int = 50, u: dict = Depends(admin_dep)):
        accts = filtered(q, status, plan, sort, order)
        limit = max(1, min(limit, 500))
        return {"total": len(accts), "items": accts[max(0, offset):max(0, offset) + limit],
                "plans": [{"plan": k, "label": p["label"]} for k, p in config.PLANS.items()]}

    @app.get("/api/admin/accounts.csv")
    def export_accounts(q: str = "", status: str = "", plan: str = "", sort: str = "created", order: str = "desc",
                        u: dict = Depends(admin_dep)):
        cols = ["id", "company", "owner_email", "owner_name", "status", "plan_label", "price", "created_at", "last_seen",
                "users", "invites", "clients", "events", "published", "photos", "storage", "failed", "searches", "disabled_reason"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["storage_bytes" if c == "storage" else f"price_{config.CURRENCY}" if c == "price" else c for c in cols])
        iso = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
        for a in filtered(q, status, plan, sort, order):
            w.writerow([_csv_cell(iso(a[c]) if c in ("created_at", "last_seen") else a[c]) for c in cols])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="companies-{datetime.now():%Y-%m-%d}.csv"'})

    @app.get("/api/admin/accounts/{aid}")
    def read_account(aid: int, u: dict = Depends(admin_dep)):
        a = get_account(aid)
        a["members"] = [dict(r) for r in db.q(
            """SELECT id, email, name, role, platform_admin, created_at, last_seen_at,
                      (SELECT COUNT(*) FROM sessions s WHERE s.user_id=users.id AND s.expires_at > ?) AS sessions
               FROM users WHERE account_id=? ORDER BY id""", (int(time.time()), aid))]
        for m in a["members"]:
            m["role_label"] = roles.label(m["role"])
        a["pending_invites"] = [dict(r, role_label=roles.label(r["role"])) for r in db.q(
            """SELECT i.email, i.role, i.created_at, i.expires_at, u.email AS invited_by FROM invites i
               LEFT JOIN users u ON u.id=i.invited_by WHERE i.account_id=? AND i.accepted_at IS NULL ORDER BY i.id DESC""",
            (aid,))]
        a["event_list"] = [dict(r) for r in db.q(
            """SELECT e.id, e.name, e.status, e.event_date, e.created_at, e.ftp_allowed, c.name AS client_name,
                      (SELECT uploads FROM camera_logins l WHERE l.event_id=e.id) AS camera_uploads,
                      (SELECT last_upload_at FROM camera_logins l WHERE l.event_id=e.id) AS camera_last_upload,
                      (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id) AS photos,
                      (SELECT COALESCE(SUM(size), 0) FROM photos p WHERE p.event_id=e.id)
                        + (SELECT COALESCE(SUM(size), 0) FROM videos v WHERE v.event_id=e.id) AS storage,
                      (SELECT COUNT(*) FROM guest_searches s WHERE s.event_id=e.id) AS searches
               FROM events e JOIN clients c ON c.id=e.client_id WHERE e.account_id=? ORDER BY e.id DESC LIMIT 2000""",
            (aid,))]
        for e in a["event_list"]:
            e["ftp_allowed"] = bool(e["ftp_allowed"])
        a["ftp_server"] = bool(config.settings.ftp_port)
        a["log"] = log_rows("WHERE account_id=?", (aid,))
        return a

    @app.post("/api/admin/accounts/{aid}/disable")
    def disable_account(aid: int, body: DisableIn, u: dict = Depends(admin_dep)):
        a = get_account(aid)
        if aid == u["account_id"]:
            raise err(409, "You can't suspend the company you are signed in with.", "self")
        if a["status"] == "suspended":
            raise err(409, "This company is already suspended.", "already")
        reason = body.reason.strip()
        with db.tx():
            db.run("UPDATE accounts SET disabled_at=?, disabled_reason=? WHERE id=?", (int(time.time()), reason, aid))
            db.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE account_id=?)", (aid,))
            log(u, aid, a["company"], "suspend", reason)
        return get_account(aid)

    @app.post("/api/admin/accounts/{aid}/enable")
    def enable_account(aid: int, u: dict = Depends(admin_dep)):
        a = get_account(aid)
        if a["status"] != "suspended":
            raise err(409, "This company is not suspended.", "already")
        with db.tx():
            db.run("UPDATE accounts SET disabled_at=NULL, disabled_reason='' WHERE id=?", (aid,))
            log(u, aid, a["company"], "enable")
        return get_account(aid)

    @app.post("/api/admin/accounts/{aid}/plan")
    def change_plan(aid: int, body: PlanIn, u: dict = Depends(admin_dep)):
        a = get_account(aid)
        if body.plan not in config.PLANS:
            raise err(422, f"Unknown plan. Choose one of: {', '.join(config.PLANS)}.", "plan")
        if body.plan == a["plan"]:
            return a
        with db.tx():
            db.run("UPDATE accounts SET plan=? WHERE id=?", (body.plan, aid))
            log(u, aid, a["company"], "plan", f"{a['plan_label']} -> {config.PLANS[body.plan]['label']}")
        return get_account(aid)

    @app.post("/api/admin/accounts/{aid}/signout")
    def sign_out_everywhere(aid: int, request: Request, u: dict = Depends(admin_dep)):
        a = get_account(aid)
        keep = security._digest(request.cookies.get(COOKIE, ""))  # never the admin's own current session
        with db.tx():
            n = db.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE account_id=?) AND token_hash != ?",
                       (aid, keep))
            log(u, aid, a["company"], "signout", f"{n} session{'s' if n != 1 else ''}")
        return {"signed_out": n}

    @app.get("/api/admin/camera")
    def camera_events(u: dict = Depends(admin_dep)):
        """Every event with camera FTP on, across all companies, most recently active first."""
        rows = db.q("""SELECT e.id, e.name, e.status, e.event_date, e.account_id, a.company, a.disabled_at, c.name AS client_name,
                              l.username, l.enabled AS login_enabled, l.created_at AS login_created, l.last_login_at,
                              l.last_upload_at, COALESCE(l.uploads, 0) AS uploads, COALESCE(l.last_note, '') AS last_note,
                              (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id) AS photos
                       FROM events e JOIN accounts a ON a.id=e.account_id JOIN clients c ON c.id=e.client_id
                       LEFT JOIN camera_logins l ON l.event_id=e.id
                       WHERE e.ftp_allowed=1
                       ORDER BY COALESCE(l.last_upload_at, l.last_login_at, l.created_at, 0) DESC, e.id DESC""")
        now = int(time.time())
        out = []
        for r in rows:
            r = dict(r)
            r["state"] = ("suspended" if r["disabled_at"] else "no_login" if r["username"] is None
                          else "off" if not r["login_enabled"] else
                          "receiving" if r["last_upload_at"] and now - r["last_upload_at"] < 600 else "ready")
            r["login_enabled"] = bool(r["login_enabled"]) if r["username"] is not None else None
            out.append(r)
        return {"server": server_info(), "now": now, "events": out}

    @app.post("/api/admin/events/{eid}/ftp")
    def allow_ftp(eid: int, body: FtpIn, u: dict = Depends(admin_dep)):
        """Camera upload (FTP) for one event. Off: its camera login stops working at once (it is kept, not deleted)."""
        e = db.q1("SELECT e.id, e.name, e.account_id, e.ftp_allowed, a.company FROM events e JOIN accounts a ON a.id=e.account_id WHERE e.id=?", (eid,))
        if not e:
            raise err(404, "Event not found.", "not_found")
        if bool(e["ftp_allowed"]) != body.allowed:
            with db.tx():
                db.run("UPDATE events SET ftp_allowed=? WHERE id=?", (int(body.allowed), eid))
                log(u, e["account_id"], e["company"], "ftp_on" if body.allowed else "ftp_off", e["name"])
        return {"id": eid, "ftp_allowed": body.allowed}

    # ------------------------------------------------------------------ audit log
    @app.get("/api/admin/log")
    def read_log(offset: int = 0, limit: int = 100, account_id: Optional[int] = None, u: dict = Depends(admin_dep)):
        limit = max(1, min(limit, 500))
        if account_id is not None:
            return log_rows("WHERE account_id=?", (account_id,), limit, max(0, offset))
        return log_rows(limit=limit, offset=max(0, offset))
