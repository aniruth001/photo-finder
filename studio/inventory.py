"""Equipment inventory (Team > Inventory): the studio's cameras, lenses, lights and the rest, who has each one,
for which event, until when, and everything that happened to it.

Owners, admins and managers manage it (add, edit, delete, check out to anyone, maintenance). Photographers check
available items out to themselves and return what they have. Viewers only look. Every change is in the item's
history, which survives the people and events it mentions.
"""
import csv
import io
import re
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, Response
from pydantic import BaseModel, Field

from . import db, roles

CATEGORIES = ["Camera body", "Lens", "Flash & lighting", "Drone", "Audio", "Video & gimbal", "Tripod & support",
              "Memory card", "Battery & power", "Computer & storage", "Backdrop & props", "Other"]
STATUSES = {"available": "Available", "out": "Checked out", "maintenance": "In maintenance", "retired": "Retired"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ItemIn(BaseModel):
    name: str = Field(max_length=150)
    category: str = Field("Other", max_length=40)
    brand: str = Field("", max_length=80)
    serial: str = Field("", max_length=80)
    quantity: int = Field(1, ge=1, le=100000)
    purchase_date: Optional[str] = Field(None, max_length=10)
    purchase_cost: float = Field(0, ge=0, le=1_000_000_000)
    notes: str = Field("", max_length=2000)


class CheckoutIn(BaseModel):
    user_id: Optional[int] = None          # default: yourself
    event_id: Optional[int] = None
    due_back: Optional[str] = Field(None, max_length=10)
    note: str = Field("", max_length=500)


class NoteIn(BaseModel):
    note: str = Field("", max_length=500)


class StatusIn(BaseModel):
    status: str = Field(max_length=20)     # available | maintenance | retired
    note: str = Field("", max_length=500)


def _today(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).date().isoformat() if tz else date.today().isoformat()
    except Exception:
        return date.today().isoformat()


def _valid_date(d) -> bool:
    if not d:
        return True
    if not DATE_RE.match(d):
        return False
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return True
    except ValueError:
        return False


ITEM_SQL = """SELECT i.*, u.name AS holder_name, u.email AS holder_email, e.name AS event_name
              FROM inventory i LEFT JOIN users u ON u.id=i.holder_id LEFT JOIN events e ON e.id=i.event_id """


def item_out(r, today: str) -> dict:
    return {
        "id": r["id"], "name": r["name"], "category": r["category"], "brand": r["brand"], "serial": r["serial"],
        "quantity": r["quantity"], "purchase_date": r["purchase_date"], "purchase_cost": r["purchase_cost"] / 100,
        "notes": r["notes"], "status": r["status"], "status_label": STATUSES.get(r["status"], r["status"]),
        "holder": {"id": r["holder_id"], "name": r["holder_name"] or r["holder_email"] or ""} if r["holder_id"] else None,
        "event": {"id": r["event_id"], "name": r["event_name"]} if r["event_id"] else None,
        "checked_out_at": r["checked_out_at"], "due_back": r["due_back"],
        "overdue": bool(r["status"] == "out" and r["due_back"] and r["due_back"] < today),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def mount_inventory(app, h):
    """h: helpers from server.create_app (user_dep, can, err)."""
    err = h.err
    manage = h.can("inventory")

    def may_manage(u) -> bool:
        return "inventory" in roles.perms(u["role"])

    def may_use(u) -> bool:  # check out to oneself, return one's own: every role that does the work
        return may_manage(u) or "upload" in roles.perms(u["role"])

    def get(u, iid):
        r = db.q1(ITEM_SQL + "WHERE i.id=? AND i.account_id=?", (iid, u["account_id"]))
        if not r:
            raise err(404, "Item not found.", "not_found")
        return r

    def log(u, iid, action, detail="", to_user=None, event_id=None):
        to = db.q1("SELECT name, email FROM users WHERE id=?", (to_user,)) if to_user else None
        ev = db.q1("SELECT name FROM events WHERE id=?", (event_id,)) if event_id else None
        db.ex("""INSERT INTO inventory_log(item_id, account_id, action, actor, person, event, detail, created_at)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (iid, u["account_id"], action, u["name"] or u["email"], (to["name"] or to["email"]) if to else "",
               ev["name"] if ev else "", detail, int(time.time())))

    def clean(body: ItemIn) -> dict:
        if not body.name.strip():
            raise err(422, "Give the item a name.", "name")
        if body.category not in CATEGORIES:
            raise err(422, "Choose a category from the list.", "category")
        if not _valid_date(body.purchase_date):
            raise err(422, "Use a valid purchase date.", "purchase_date")
        return {"name": body.name.strip(), "category": body.category, "brand": body.brand.strip(), "serial": body.serial.strip(),
                "quantity": body.quantity, "purchase_date": body.purchase_date or None,
                "purchase_cost": int(round(body.purchase_cost * 100)), "notes": body.notes.strip()}

    def out(u, iid):
        return item_out(get(u, iid), _today(u["timezone"]))

    @app.get("/api/inventory")
    def list_items(u: dict = Depends(h.user_dep)):
        today = _today(u["timezone"])
        items = [item_out(r, today) for r in db.q(ITEM_SQL + "WHERE i.account_id=? ORDER BY i.status='retired', i.category, i.name COLLATE NOCASE",
                                                  (u["account_id"],))]
        live = [i for i in items if i["status"] != "retired"]
        value = sum(i["purchase_cost"] * i["quantity"] for i in live)
        if not may_manage(u):  # what things cost is for the people who manage them
            for i in items:
                i["purchase_cost"] = None
            value = None
        members = db.q("SELECT id, name, email FROM users WHERE account_id=? ORDER BY name COLLATE NOCASE, email", (u["account_id"],))
        events = db.q("SELECT id, name, event_date FROM events WHERE account_id=? ORDER BY COALESCE(event_date,'') DESC, id DESC LIMIT 200",
                      (u["account_id"],))
        return {
            "items": items,
            "summary": {"items": len(live), "units": sum(i["quantity"] for i in live),
                        "available": sum(i["status"] == "available" for i in live), "out": sum(i["status"] == "out" for i in live),
                        "overdue": sum(i["overdue"] for i in live), "maintenance": sum(i["status"] == "maintenance" for i in live),
                        "value": value},
            "categories": CATEGORIES, "statuses": STATUSES,
            "members": [{"id": m["id"], "name": m["name"] or m["email"], "me": m["id"] == u["user_id"]} for m in members],
            "events": [{"id": e["id"], "name": e["name"], "date": e["event_date"]} for e in events],
            "can_manage": may_manage(u), "can_use": may_use(u), "me": u["user_id"], "today": today,
        }

    @app.post("/api/inventory")
    def add(body: ItemIn, u: dict = Depends(manage)):
        f, now = clean(body), int(time.time())
        with db.tx():
            iid = db.ex(f"""INSERT INTO inventory(account_id, {', '.join(f)}, status, created_at, updated_at)
                            VALUES(?, {', '.join('?' * len(f))}, 'available', ?, ?)""", (u["account_id"], *f.values(), now, now))
            log(u, iid, "added")
        return out(u, iid)

    @app.patch("/api/inventory/{iid}")
    def edit(iid: int, body: ItemIn, u: dict = Depends(manage)):
        old, f = get(u, iid), clean(body)
        changed = [k.replace("_", " ") for k in f if old[k] != f[k]]
        with db.tx():
            db.run(f"UPDATE inventory SET {', '.join(k + '=?' for k in f)}, updated_at=? WHERE id=? AND account_id=?",
                   (*f.values(), int(time.time()), iid, u["account_id"]))
            if changed:
                log(u, iid, "edited", ", ".join(changed))
        return out(u, iid)

    @app.delete("/api/inventory/{iid}")
    def delete(iid: int, u: dict = Depends(manage)):
        get(u, iid)
        db.run("DELETE FROM inventory WHERE id=? AND account_id=?", (iid, u["account_id"]))  # its history goes with it
        return {"ok": True}

    @app.post("/api/inventory/{iid}/checkout")
    def checkout(iid: int, body: CheckoutIn, u: dict = Depends(h.user_dep)):
        if not may_use(u):
            raise err(403, f"Your role ({roles.label(u['role'])}) can't check equipment out.", "role")
        r = get(u, iid)
        to = body.user_id or u["user_id"]
        if to != u["user_id"] and not may_manage(u):
            raise err(403, "You can check equipment out to yourself only.", "role")
        if not db.q1("SELECT 1 FROM users WHERE id=? AND account_id=?", (to, u["account_id"])):
            raise err(404, "That team member wasn't found.", "not_found")
        if body.event_id is not None and not db.q1("SELECT 1 FROM events WHERE id=? AND account_id=?", (body.event_id, u["account_id"])):
            raise err(404, "Event not found.", "not_found")
        if not _valid_date(body.due_back):
            raise err(422, "Use a valid date for \"due back\".", "due_back")
        if body.due_back and body.due_back < _today(u["timezone"]):
            raise err(422, "\"Due back\" can't be in the past.", "due_back")
        now = int(time.time())
        with db.tx():  # only an available item: two people can't take the same one at once
            if db.run("""UPDATE inventory SET status='out', holder_id=?, event_id=?, due_back=?, checked_out_at=?, updated_at=?
                         WHERE id=? AND account_id=? AND status='available'""",
                      (to, body.event_id, body.due_back or None, now, now, iid, u["account_id"])) != 1:
                why = {"out": f"It's already checked out{' to ' + (r['holder_name'] or r['holder_email']) if r['holder_id'] else ''}.",
                       "maintenance": "It's in maintenance.", "retired": "It's retired."}.get(r["status"], "It isn't available.")
                raise err(409, why, "unavailable")
            due = datetime.strptime(body.due_back, "%Y-%m-%d").strftime("%d %b %Y").lstrip("0") if body.due_back else ""
            log(u, iid, "checked_out", " · ".join(x for x in (body.note.strip(), due and "due back " + due) if x),
                to_user=to, event_id=body.event_id)
        return out(u, iid)

    @app.post("/api/inventory/{iid}/return")
    def give_back(iid: int, body: NoteIn, u: dict = Depends(h.user_dep)):
        r = get(u, iid)
        if r["status"] != "out":
            raise err(409, "It isn't checked out.", "not_out")
        if not (may_manage(u) or (may_use(u) and r["holder_id"] == u["user_id"])):
            raise err(403, "Only the person who has it, or a manager, can check it back in.", "role")
        with db.tx():
            db.run("""UPDATE inventory SET status='available', holder_id=NULL, event_id=NULL, due_back=NULL, checked_out_at=NULL,
                      updated_at=? WHERE id=?""", (int(time.time()), iid))
            log(u, iid, "returned", body.note.strip(), to_user=r["holder_id"], event_id=r["event_id"])
        return out(u, iid)

    @app.post("/api/inventory/{iid}/status")
    def set_status(iid: int, body: StatusIn, u: dict = Depends(manage)):
        r = get(u, iid)
        if body.status not in ("available", "maintenance", "retired"):
            raise err(422, "Choose available, maintenance or retired.", "status")
        if r["status"] == "out":
            raise err(409, "Check it back in first.", "is_out")
        if r["status"] == body.status:
            return out(u, iid)
        with db.tx():
            db.run("UPDATE inventory SET status=?, updated_at=? WHERE id=?", (body.status, int(time.time()), iid))
            log(u, iid, body.status, body.note.strip())
        return out(u, iid)

    @app.get("/api/inventory/{iid}/history")
    def history(iid: int, u: dict = Depends(h.user_dep)):
        get(u, iid)
        return [dict(r) for r in db.q("""SELECT action, actor, person, event, detail, created_at FROM inventory_log
                                         WHERE item_id=? ORDER BY id DESC LIMIT 200""", (iid,))]

    @app.get("/api/inventory.csv")
    def export(u: dict = Depends(h.user_dep)):
        today = _today(u["timezone"])
        buf = io.StringIO()
        w = csv.writer(buf)
        costs = may_manage(u)
        w.writerow(["Name", "Category", "Brand / model", "Serial", "Quantity", "Status", "With", "Event", "Due back",
                    "Overdue", "Purchase date"] + (["Purchase cost"] if costs else []) + ["Notes"])
        for r in db.q(ITEM_SQL + "WHERE i.account_id=? ORDER BY i.category, i.name COLLATE NOCASE", (u["account_id"],)):
            i = item_out(r, today)
            cells = [i["name"], i["category"], i["brand"], i["serial"], i["quantity"], i["status_label"],
                     i["holder"]["name"] if i["holder"] else "", i["event"]["name"] if i["event"] else "", i["due_back"] or "",
                     "yes" if i["overdue"] else "", i["purchase_date"] or ""] + ([f"{i['purchase_cost']:.2f}"] if costs else []) + [i["notes"]]
            w.writerow([("'" + c) if isinstance(c, str) and c[:1] in "=+-@" else c for c in cells])  # no formulas in Excel
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="inventory-{today}.csv"'})
