"""Team members of a company: invite by email, accept, change role, remove.

An invite can only go to an email that has no Photo Finder account yet (in any company), and accepting it adds the
person to the inviting company. It never creates a company. Links are one-time and expire after 7 days; only the
token's hash is stored, like sessions and password resets.
"""
import html
import secrets
import sqlite3
import time

from fastapi import BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field

from . import branding, db, mailer, roles, security

INVITE_DAYS = 7


class InviteIn(BaseModel):
    email: str = Field(max_length=320)
    role: str = Field(max_length=20)
    name: str = Field("", max_length=100)


class RoleIn(BaseModel):
    role: str = Field(max_length=20)


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


class AcceptIn(BaseModel):
    token: str = Field(max_length=200)
    name: str = Field("", max_length=100)
    password: str = Field(min_length=8, max_length=200)


def seats_used(account_id: int) -> int:
    """Members plus invites still waiting: an invite holds its seat until it is accepted, cancelled or expires."""
    r = db.q1("""SELECT (SELECT COUNT(*) FROM users WHERE account_id=?) +
                        (SELECT COUNT(*) FROM invites WHERE account_id=? AND accepted_at IS NULL AND expires_at > ?) n""",
              (account_id, account_id, int(time.time())))
    return r["n"]


def invite_email(inviter: str, company: str, role: str, link: str, logo_url: str = ""):
    """(plain text, html) of the invitation."""
    who = inviter or "Someone"
    text = (f"Hi,\n\n{who} invited you to join {company} on Photo Finder Studio as {roles.label(role)}.\n\n"
            f"Accept the invitation here (the link works once, for {INVITE_DAYS} days):\n{link}\n\n"
            "If you weren't expecting this, you can ignore this email.\n")
    e = lambda v: html.escape(v, quote=True)
    logo = (f'<p style="margin:0 0 18px"><img src="{e(logo_url)}" alt="{e(company)}" style="max-height:48px;max-width:240px"></p>'
            if logo_url else "")
    page = f"""<div style="font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;max-width:480px;margin:0 auto;color:#1d1b16">
  {logo}<h2 style="font-size:20px;margin:0 0 16px">Join {e(company)} on Photo Finder Studio</h2>
  <p>{e(who)} invited you to join <b>{e(company)}</b> as <b>{e(roles.label(role))}</b>.</p>
  <p style="margin:24px 0"><a href="{e(link)}" style="background:#1d1b16;color:#fff;padding:12px 20px;border-radius:9px;text-decoration:none;font-weight:600">Accept invitation</a></p>
  <p style="color:#6f6a5d;font-size:14px">The link works once, for {INVITE_DAYS} days. If the button doesn't work, paste this into your browser:<br><span style="word-break:break-all">{e(link)}</span></p>
  <p style="color:#6f6a5d;font-size:14px">If you weren't expecting this, you can ignore this email.</p></div>"""
    return text, page


def mount_team(app, h):
    """h: helpers from server.create_app (user_dep, can, err, public_base, client_ip, limiter, set_session,
    email_re, plan_of)."""
    err, can = h.err, h.can

    def pending_invite(token: str):
        r = db.q1("""SELECT i.*, a.company, a.disabled_at, u.name AS inviter_name, u.email AS inviter_email
                     FROM invites i JOIN accounts a ON a.id=i.account_id LEFT JOIN users u ON u.id=i.invited_by
                     WHERE i.token_hash=?""", (security._digest(token),))
        if not r or r["accepted_at"] or r["expires_at"] < time.time():
            raise err(410, "This invitation has expired, was cancelled or was already used. Ask for a new one.", "expired")
        if r["disabled_at"]:
            raise err(403, h.suspended, "suspended")
        return r

    def join(token: str, name: str, pw_hash: str, google_sub=None) -> int:
        """Add the invited person to the company; returns the new user id."""
        inv = pending_invite(token)
        if db.q1("SELECT 1 FROM users WHERE email=?", (inv["email"],)):
            raise err(409, "This email already has a Photo Finder account. Sign in instead.", "exists")
        try:
            with db.tx():
                if db.run("UPDATE invites SET accepted_at=? WHERE id=? AND accepted_at IS NULL",
                          (int(time.time()), inv["id"])) != 1:
                    raise err(410, "This invitation was already used.", "expired")
                return db.ex("""INSERT INTO users(account_id, email, name, pw_hash, role, google_sub, created_at)
                                VALUES(?,?,?,?,?,?,?)""",
                             (inv["account_id"], inv["email"], name.strip() or inv["name"], pw_hash, inv["role"],
                              google_sub, int(time.time())))
        except sqlite3.IntegrityError:
            raise err(409, "This email already has a Photo Finder account. Sign in instead.", "exists")

    app.state.join_team = join  # the Google callback uses it when the flow started from an invite
    app.state.pending_invite = pending_invite

    def send(request, tasks, u, email, role, token):
        link = f"{h.public_base(request)}/#/join?token={token}"
        text, page = invite_email(u["name"] or u["email"], u["company"], role, link,
                                  branding.logo_url(u["logo"], h.public_base(request)))
        tasks.add_task(mailer.send, email, f"Join {u['company']} on Photo Finder Studio", text, page)

    def member_out(r, me):
        return {"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"],
                "role_label": roles.label(r["role"]), "last_seen": r["last_seen_at"], "created_at": r["created_at"],
                "google": bool(r["google_sub"]), "me": r["id"] == me["user_id"],
                "manageable": r["id"] != me["user_id"] and roles.can_manage(me["role"], r["role"])}

    # ------------------------------------------------------------------ the team page
    @app.get("/api/team")
    def team(u: dict = Depends(h.user_dep)):
        now = int(time.time())
        members = db.q("SELECT * FROM users WHERE account_id=? ORDER BY role='owner' DESC, created_at", (u["account_id"],))
        invites = db.q("""SELECT i.*, u.email AS inviter FROM invites i LEFT JOIN users u ON u.id=i.invited_by
                          WHERE i.account_id=? AND i.accepted_at IS NULL ORDER BY i.id DESC""", (u["account_id"],))
        limit = h.plan_of(u["plan"]).get("users")
        return {
            "members": [member_out(r, u) for r in members],
            "invites": [{"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"],
                         "role_label": roles.label(r["role"]), "invited_by": r["inviter"] or "",
                         "created_at": r["created_at"], "expires_at": r["expires_at"], "expired": r["expires_at"] <= now,
                         "manageable": roles.can_manage(u["role"], r["role"])} for r in invites],
            "roles": roles.public(),
            "assignable": [r for r in roles.INVITABLE if roles.can_manage(u["role"], r)],
            "seats": {"used": seats_used(u["account_id"]), "limit": limit},
            "can_manage": "team" in roles.perms(u["role"]),
        }

    @app.post("/api/team/invites")
    def invite(body: InviteIn, request: Request, tasks: BackgroundTasks, u: dict = Depends(can("team"))):
        email = body.email.strip().lower()
        if not h.email_re.match(email):
            raise err(422, "Enter a valid email address.", "email")
        if body.role not in roles.INVITABLE or not roles.can_manage(u["role"], body.role):
            raise err(403, f"As {roles.label(u['role'])} you can't invite someone as {roles.label(body.role)}.", "role")
        if not h.limiter.allow(("invite", u["account_id"]), 30, 3600):
            raise err(429, "Too many invitations in the last hour. Try again later.", "rate")
        if db.q1("SELECT 1 FROM users WHERE email=?", (email,)):
            raise err(409, "This email already has a Photo Finder account, so it can't be invited. "
                           "Each person can belong to one company.", "exists")
        now = int(time.time())
        existing = db.q1("SELECT id, expires_at FROM invites WHERE account_id=? AND email=? AND accepted_at IS NULL",
                         (u["account_id"], email))
        limit = h.plan_of(u["plan"]).get("users")
        holds_seat = existing and existing["expires_at"] > now  # a live invite already counts; an expired one doesn't
        token = secrets.token_urlsafe(32)
        args = (body.role, body.name.strip(), security._digest(token), u["user_id"], now, now + INVITE_DAYS * 86400)
        with db.tx():  # the seat count and the write together, so two invitations at once can't both take the last seat
            if not holds_seat and limit is not None and seats_used(u["account_id"]) >= limit:
                raise err(403, f"The {h.plan_of(u['plan'])['label']} plan allows {limit} team members. "
                               "Upgrade or remove someone first.", "plan_limit")
            if existing:  # inviting again: new link and role, the old link stops working
                db.run("UPDATE invites SET role=?, name=?, token_hash=?, invited_by=?, created_at=?, expires_at=? WHERE id=?",
                       (*args, existing["id"]))
            else:
                db.ex("""INSERT INTO invites(role, name, token_hash, invited_by, created_at, expires_at, account_id, email)
                         VALUES(?,?,?,?,?,?,?,?)""", (*args, u["account_id"], email))
        send(request, tasks, u, email, body.role, token)
        return {"ok": True, "message": f"Invitation sent to {email}."}

    def get_invite(u, iid):
        r = db.q1("SELECT * FROM invites WHERE id=? AND account_id=? AND accepted_at IS NULL", (iid, u["account_id"]))
        if not r:
            raise err(404, "Invitation not found.", "not_found")
        if not roles.can_manage(u["role"], r["role"]):
            raise err(403, "You can't manage this invitation.", "role")
        return r

    @app.post("/api/team/invites/{iid}/resend")
    def resend(iid: int, request: Request, tasks: BackgroundTasks, u: dict = Depends(can("team"))):
        r = get_invite(u, iid)
        if not h.limiter.allow(("invite", u["account_id"]), 30, 3600):
            raise err(429, "Too many invitations in the last hour. Try again later.", "rate")
        if db.q1("SELECT 1 FROM users WHERE email=?", (r["email"],)):
            raise err(409, "This email has a Photo Finder account now, so it can't be invited.", "exists")
        token, now = secrets.token_urlsafe(32), int(time.time())
        limit = h.plan_of(u["plan"]).get("users")
        if r["expires_at"] <= now and limit is not None and seats_used(u["account_id"]) >= limit:
            raise err(403, f"The {h.plan_of(u['plan'])['label']} plan allows {limit} team members. "
                           "Upgrade or remove someone first.", "plan_limit")
        db.run("UPDATE invites SET token_hash=?, created_at=?, expires_at=? WHERE id=?",
               (security._digest(token), now, now + INVITE_DAYS * 86400, iid))
        send(request, tasks, u, r["email"], r["role"], token)
        return {"ok": True, "message": f"Invitation sent again to {r['email']}."}

    @app.delete("/api/team/invites/{iid}")
    def cancel(iid: int, u: dict = Depends(can("team"))):
        get_invite(u, iid)
        db.run("DELETE FROM invites WHERE id=?", (iid,))
        return {"ok": True}

    def get_member(u, uid):
        r = db.q1("SELECT * FROM users WHERE id=? AND account_id=?", (uid, u["account_id"]))
        if not r:
            raise err(404, "Team member not found.", "not_found")
        if r["id"] == u["user_id"]:
            raise err(409, "You can't change your own role or remove yourself here.", "self")
        if not roles.can_manage(u["role"], r["role"]):
            raise err(403, f"As {roles.label(u['role'])} you can't change a {roles.label(r['role'])}.", "role")
        return r

    @app.patch("/api/team/members/{uid}")
    def change_role(uid: int, body: RoleIn, u: dict = Depends(can("team"))):
        get_member(u, uid)
        if body.role not in roles.INVITABLE or not roles.can_manage(u["role"], body.role):
            raise err(403, f"As {roles.label(u['role'])} you can't make someone {roles.label(body.role)}.", "role")
        db.run("UPDATE users SET role=? WHERE id=? AND account_id=?", (body.role, uid, u["account_id"]))
        return {"ok": True}

    @app.delete("/api/team/members/{uid}")
    def remove(uid: int, u: dict = Depends(can("team"))):
        get_member(u, uid)
        db.run("DELETE FROM users WHERE id=? AND account_id=?", (uid, u["account_id"]))  # their sessions go with them
        return {"ok": True}

    # ------------------------------------------------------------------ accepting (no login)
    @app.post("/api/invite/check")
    def check(body: TokenIn):
        r = pending_invite(body.token)
        return {"company": r["company"], "email": r["email"], "name": r["name"], "role": r["role"],
                "role_label": roles.label(r["role"]), "about": roles.ROLES[r["role"]]["about"],
                "invited_by": r["inviter_name"] or r["inviter_email"] or "", "taken": bool(
                    db.q1("SELECT 1 FROM users WHERE email=?", (r["email"],)))}

    @app.post("/api/invite/accept")
    def accept(body: AcceptIn, request: Request, response: Response):
        if not h.limiter.allow(("invite-accept", h.client_ip(request)), 20, 3600):
            raise err(429, "Too many attempts. Wait a while and try again.", "rate")
        uid = join(body.token, body.name, security.hash_password(body.password))
        h.set_session(response, uid)
        return {"ok": True}
