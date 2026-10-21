"""Tasks: the studio's project board. Every job (planning, shooting, editing, the album, delivery) is a card that moves
through stages, New to Done, with a deadline, the team members on it, a checklist, tags, and a comment thread.

A task can be given to a vendor (an editor, an album printer, a drone team): their name and number, the work given
to them, and what was agreed and paid, so the board also shows what is still owed to whom.

Roles that manage tasks (owner, admin, manager) add, edit, move and delete any task, and see the vendor money.
Team members who do the work (photographers) move the tasks they are on, set their status, tick the checklist and
comment. Viewers only look. Every change is in the task's history, which keeps the names it mentions.
"""
import json
import re
import time
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends
from pydantic import BaseModel, Field

from . import db, roles

STAGES = {"new": "New", "planned": "Planned", "progress": "In progress", "feedback": "Feedback", "done": "Done",
          "cancelled": "Cancelled"}
CLOSED = ("done", "cancelled")
STATUSES = {"on_track": "On track", "ready": "Ready", "changes": "Changes requested", "blocked": "Blocked"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_AMOUNT = 10_000_000_000  # paise


class CheckItem(BaseModel):
    text: str = Field(max_length=200)
    done: bool = False


class TaskIn(BaseModel):
    title: str = Field(max_length=150)
    description: str = Field("", max_length=4000)
    stage: str = Field("new", max_length=20)
    status: str = Field("on_track", max_length=20)
    starred: bool = False
    deadline: Optional[str] = Field(None, max_length=10)
    tags: List[str] = Field(default_factory=list, max_length=8)
    client_id: Optional[int] = None
    event_id: Optional[int] = None
    assignees: List[int] = Field(default_factory=list, max_length=30)
    checklist: List[CheckItem] = Field(default_factory=list, max_length=50)
    vendor_name: str = Field("", max_length=120)
    vendor_phone: str = Field("", max_length=40)
    vendor_work: str = Field("", max_length=4000)
    vendor_amount: float = Field(0, ge=0)
    vendor_paid: float = Field(0, ge=0)


class MoveIn(BaseModel):
    stage: str = Field(max_length=20)
    order: List[int] = Field(default_factory=list, max_length=2000)  # the target column's ids, top to bottom, after the move


class QuickIn(BaseModel):  # what the people on a task may change without the full form
    status: Optional[str] = Field(None, max_length=20)
    starred: Optional[bool] = None
    checklist: Optional[List[CheckItem]] = Field(None, max_length=50)


class CommentIn(BaseModel):
    text: str = Field(max_length=2000)


def _today(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).date().isoformat() if tz else date.today().isoformat()
    except Exception:
        return date.today().isoformat()


def _valid_date(d) -> bool:
    if not d or not DATE_RE.match(d):
        return False
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return True
    except ValueError:
        return False


TASK_SQL = """SELECT t.*, c.name AS client_name, e.name AS event_name,
              (SELECT COUNT(*) FROM task_log l WHERE l.task_id=t.id AND l.kind='comment') AS comments
              FROM tasks t LEFT JOIN clients c ON c.id=t.client_id LEFT JOIN events e ON e.id=t.event_id """


def task_out(r, people: list, today: str, money: bool) -> dict:
    checklist = json.loads(r["checklist"] or "[]")
    closed = r["stage"] in CLOSED
    return {
        "id": r["id"], "title": r["title"], "description": r["description"], "stage": r["stage"], "position": r["position"],
        "status": r["status"], "status_label": STATUSES.get(r["status"], r["status"]), "starred": bool(r["starred"]),
        "deadline": r["deadline"], "overdue": bool(not closed and r["deadline"] and r["deadline"] < today),
        "tags": json.loads(r["tags"] or "[]"),
        "client": {"id": r["client_id"], "name": r["client_name"]} if r["client_id"] else None,
        "event": {"id": r["event_id"], "name": r["event_name"]} if r["event_id"] else None,
        "assignees": people, "checklist": checklist, "checked": sum(1 for c in checklist if c.get("done")),
        "comments": r["comments"],
        "vendor": {"name": r["vendor_name"], "phone": r["vendor_phone"], "work": r["vendor_work"],
                   **({"amount": r["vendor_amount"] / 100, "paid": r["vendor_paid"] / 100,
                       "balance": max(0, r["vendor_amount"] - r["vendor_paid"]) / 100} if money else {})} if r["vendor_name"] else None,
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def mount_tasks(app, h):
    """h: helpers from server.create_app (user_dep, can, err)."""
    err = h.err
    manage = h.can("tasks")

    def may_manage(u) -> bool:
        return "tasks" in roles.perms(u["role"])

    def may_work(u) -> bool:  # comment, and update the tasks one is on: every role that does the work
        return may_manage(u) or "upload" in roles.perms(u["role"])

    def get(u, tid):
        r = db.q1(TASK_SQL + "WHERE t.id=? AND t.account_id=?", (tid, u["account_id"]))
        if not r:
            raise err(404, "Task not found.", "not_found")
        return r

    def assignees_of(ids) -> dict:
        found = {i: [] for i in ids}
        if ids:
            for a in db.q(f"""SELECT a.task_id, u.id, COALESCE(NULLIF(u.name,''), u.email) AS name FROM task_assignees a
                              JOIN users u ON u.id=a.user_id WHERE a.task_id IN ({','.join('?' * len(ids))})
                              ORDER BY u.name COLLATE NOCASE""", ids):
                found[a["task_id"]].append({"id": a["id"], "name": a["name"]})
        return found

    def on_task(u, tid) -> bool:
        return bool(db.q1("SELECT 1 FROM task_assignees WHERE task_id=? AND user_id=?", (tid, u["user_id"])))

    def may_update(u, tid):
        if may_manage(u) or (may_work(u) and on_task(u, tid)):
            return
        raise err(403, "Only the people on this task, or a manager, can change it.", "role")

    def log(u, tid, kind, text=""):
        db.ex("INSERT INTO task_log(task_id, account_id, kind, actor, text, created_at) VALUES(?,?,?,?,?,?)",
              (tid, u["account_id"], kind, u["name"] or u["email"], text, int(time.time())))

    def out(u, tid):
        r = get(u, tid)
        return task_out(r, assignees_of([tid])[tid], _today(u["timezone"]), may_manage(u))

    def end_of(stage, account_id) -> float:
        return db.q1("SELECT COALESCE(MAX(position), 0) + 1 p FROM tasks WHERE account_id=? AND stage=?", (account_id, stage))["p"]

    def clean_checklist(items) -> str:
        return json.dumps([{"text": c.text.strip(), "done": c.done} for c in items if c.text.strip()])

    def clean(b: TaskIn, u) -> tuple:
        if not b.title.strip():
            raise err(422, "Give the task a title (for example \"Edit the wedding highlights\").", "title")
        if b.stage not in STAGES:
            raise err(422, "Choose a stage from the list.", "stage")
        if b.status not in STATUSES:
            raise err(422, "Choose a status from the list.", "status")
        if b.deadline and not _valid_date(b.deadline):
            raise err(422, "Use a valid deadline.", "deadline")
        client_id = b.client_id
        if b.event_id is not None:
            ev = db.q1("SELECT client_id FROM events WHERE id=? AND account_id=?", (b.event_id, u["account_id"]))
            if not ev:
                raise err(404, "Event not found.", "not_found")
            client_id = client_id or ev["client_id"]  # an event's task is for that event's client
        if client_id is not None and not db.q1("SELECT 1 FROM clients WHERE id=? AND account_id=?", (client_id, u["account_id"])):
            raise err(404, "Client not found.", "not_found")
        people = sorted(set(b.assignees))
        if people and db.q1(f"SELECT COUNT(*) n FROM users WHERE account_id=? AND id IN ({','.join('?' * len(people))})",
                            (u["account_id"], *people))["n"] != len(people):
            raise err(404, "That team member wasn't found.", "not_found")
        tags, seen = [], set()
        for t in b.tags:
            t = " ".join(t.split())[:30]
            if t and t.lower() not in seen:
                tags.append(t)
                seen.add(t.lower())
        vendor = b.vendor_name.strip()
        amount, paid = (int(round(b.vendor_amount * 100)), int(round(b.vendor_paid * 100))) if vendor else (0, 0)
        if amount > MAX_AMOUNT or paid > MAX_AMOUNT:
            raise err(422, "That amount is too large.", "amount")
        f = {"title": b.title.strip(), "description": b.description.strip(), "stage": b.stage, "status": b.status,
             "starred": int(b.starred), "deadline": b.deadline or None, "tags": json.dumps(tags), "client_id": client_id,
             "event_id": b.event_id, "checklist": clean_checklist(b.checklist), "vendor_name": vendor,
             "vendor_phone": b.vendor_phone.strip() if vendor else "", "vendor_work": b.vendor_work.strip() if vendor else "",
             "vendor_amount": amount, "vendor_paid": paid}
        return f, people

    def set_people(tid, people):
        db.run("DELETE FROM task_assignees WHERE task_id=?", (tid,))
        for p in people:
            db.ex("INSERT INTO task_assignees(task_id, user_id) VALUES(?,?)", (tid, p))

    @app.get("/api/tasks")
    def board(u: dict = Depends(h.user_dep)):
        today, money = _today(u["timezone"]), may_manage(u)
        rows = db.q(TASK_SQL + "WHERE t.account_id=? ORDER BY t.position, t.id", (u["account_id"],))
        people = assignees_of([r["id"] for r in rows])
        tasks = [task_out(r, people[r["id"]], today, money) for r in rows]
        live = [t for t in tasks if t["stage"] not in CLOSED]
        owed = [t for t in tasks if t["vendor"] and t["stage"] != "cancelled"]
        members = db.q("SELECT id, name, email FROM users WHERE account_id=? ORDER BY name COLLATE NOCASE, email", (u["account_id"],))
        clients = db.q("SELECT id, name FROM clients WHERE account_id=? ORDER BY name COLLATE NOCASE", (u["account_id"],))
        events = db.q("""SELECT id, name, event_date, client_id FROM events WHERE account_id=?
                         ORDER BY COALESCE(event_date,'') DESC, id DESC LIMIT 300""", (u["account_id"],))
        vendors = db.q("""SELECT vendor_name AS name, vendor_phone AS phone FROM tasks WHERE account_id=? AND vendor_name<>''
                          GROUP BY vendor_name COLLATE NOCASE ORDER BY MAX(updated_at) DESC LIMIT 200""", (u["account_id"],))
        return {
            "tasks": tasks,
            "summary": {"open": len(live), "overdue": sum(t["overdue"] for t in live),
                        "mine": sum(any(a["id"] == u["user_id"] for a in t["assignees"]) for t in live),
                        "vendors": sum(bool(t["vendor"]) for t in live),
                        "vendor_balance": sum(t["vendor"]["balance"] for t in owed) if money else None},
            "stages": [{"key": k, "label": v, "closed": k in CLOSED} for k, v in STAGES.items()], "statuses": STATUSES,
            "members": [{"id": m["id"], "name": m["name"] or m["email"], "me": m["id"] == u["user_id"]} for m in members],
            "clients": [dict(c) for c in clients],
            "events": [{"id": e["id"], "name": e["name"], "date": e["event_date"], "client_id": e["client_id"]} for e in events],
            "vendors": [dict(v) for v in vendors] if money else [],
            "can_manage": money, "can_work": may_work(u), "me": u["user_id"], "today": today,
        }

    @app.post("/api/tasks")
    def create(body: TaskIn, u: dict = Depends(manage)):
        (f, people), now = clean(body, u), int(time.time())
        with db.tx():
            tid = db.ex(f"""INSERT INTO tasks(account_id, {', '.join(f)}, position, created_by, created_at, updated_at)
                            VALUES(?, {', '.join('?' * len(f))}, ?, ?, ?, ?)""",
                        (u["account_id"], *f.values(), end_of(f["stage"], u["account_id"]), u["user_id"], now, now))
            set_people(tid, people)
            log(u, tid, "created", STAGES[f["stage"]])
            if f["vendor_name"]:
                log(u, tid, "vendor", f["vendor_name"])
        return out(u, tid)

    @app.patch("/api/tasks/{tid}")
    def edit(tid: int, body: TaskIn, u: dict = Depends(manage)):
        old = get(u, tid)
        f, people = clean(body, u)
        names = {"title": "title", "description": "description", "deadline": "deadline", "tags": "tags", "client_id": "client",
                 "event_id": "event", "checklist": "checklist", "vendor_phone": "vendor's number", "vendor_work": "vendor's work",
                 "vendor_amount": "vendor amount", "vendor_paid": "vendor payment"}
        changed = [v for k, v in names.items() if old[k] != f[k]]
        if sorted(a["id"] for a in assignees_of([tid])[tid]) != people:
            changed.append("people")
        with db.tx():
            pos = old["position"] if old["stage"] == f["stage"] else end_of(f["stage"], u["account_id"])
            db.run(f"UPDATE tasks SET {', '.join(k + '=?' for k in f)}, position=?, updated_at=? WHERE id=? AND account_id=?",
                   (*f.values(), pos, int(time.time()), tid, u["account_id"]))
            set_people(tid, people)
            if old["stage"] != f["stage"]:
                log(u, tid, "moved", f"{STAGES.get(old['stage'], old['stage'])} → {STAGES[f['stage']]}")
            if old["status"] != f["status"]:
                log(u, tid, "status", STATUSES[f["status"]])
            if old["vendor_name"] != f["vendor_name"]:
                if f["vendor_name"]:
                    log(u, tid, "vendor", f["vendor_name"])
                else:
                    log(u, tid, "vendor_off", old["vendor_name"])
            if changed:
                log(u, tid, "edited", ", ".join(changed))
        return out(u, tid)

    @app.delete("/api/tasks/{tid}")
    def delete(tid: int, u: dict = Depends(manage)):
        get(u, tid)
        db.run("DELETE FROM tasks WHERE id=? AND account_id=?", (tid, u["account_id"]))  # its comments and history go with it
        return {"ok": True}

    @app.post("/api/tasks/{tid}/move")
    def move(tid: int, body: MoveIn, u: dict = Depends(h.user_dep)):
        """Drag and drop: the task goes to `stage`; `order` is that column from top to bottom, with the task in it."""
        r = get(u, tid)
        may_update(u, tid)
        if body.stage not in STAGES:
            raise err(422, "Choose a stage from the list.", "stage")
        order = [i for i in dict.fromkeys(body.order)] or [tid]
        if tid not in order:
            order.append(tid)
        with db.tx():
            mine = {x["id"] for x in db.q(f"SELECT id FROM tasks WHERE account_id=? AND id IN ({','.join('?' * len(order))})",
                                          (u["account_id"], *order))}
            now = int(time.time())
            db.run("UPDATE tasks SET stage=?, updated_at=? WHERE id=?", (body.stage, now, tid))
            for n, i in enumerate(x for x in order if x in mine):  # other companies' ids are ignored
                db.run("UPDATE tasks SET position=? WHERE id=? AND stage=?", (n + 1, i, body.stage))
            if r["stage"] != body.stage:
                log(u, tid, "moved", f"{STAGES.get(r['stage'], r['stage'])} → {STAGES[body.stage]}")
        return out(u, tid)

    @app.post("/api/tasks/{tid}/quick")
    def quick(tid: int, body: QuickIn, u: dict = Depends(h.user_dep)):
        r = get(u, tid)
        may_update(u, tid)
        f = {}
        if body.status is not None:
            if body.status not in STATUSES:
                raise err(422, "Choose a status from the list.", "status")
            f["status"] = body.status
        if body.starred is not None:
            f["starred"] = int(body.starred)
        if body.checklist is not None:
            f["checklist"] = clean_checklist(body.checklist)
        if f:
            with db.tx():
                db.run(f"UPDATE tasks SET {', '.join(k + '=?' for k in f)}, updated_at=? WHERE id=?", (*f.values(), int(time.time()), tid))
                if "status" in f and f["status"] != r["status"]:
                    log(u, tid, "status", STATUSES[f["status"]])
        return out(u, tid)

    @app.get("/api/tasks/{tid}/log")
    def history(tid: int, u: dict = Depends(h.user_dep)):
        get(u, tid)
        return [dict(r) for r in db.q("SELECT id, kind, actor, text, created_at FROM task_log WHERE task_id=? ORDER BY id DESC LIMIT 300",
                                      (tid,))]

    @app.post("/api/tasks/{tid}/comments")
    def comment(tid: int, body: CommentIn, u: dict = Depends(h.user_dep)):
        get(u, tid)
        if not may_work(u):
            raise err(403, f"Your role ({roles.label(u['role'])}) can't comment on tasks.", "role")
        if not body.text.strip():
            raise err(422, "Write something first.", "text")
        log(u, tid, "comment", body.text.strip())
        db.run("UPDATE tasks SET updated_at=? WHERE id=?", (int(time.time()), tid))
        return out(u, tid)
