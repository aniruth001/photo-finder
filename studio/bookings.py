"""Calendar: the studio's bookings by date, with what was agreed and what has been paid.

A booking has a date (or a range of days), optional times, the event details and a description, the client, where
the work came from (the studio's own client, or Expost), and the payment: the finalised amount and what has been
received so far, so the balance and the payment status follow. The calendar also shows the dated events from the
Events page. Roles that manage events add and change bookings; everyone in the company sees them.
"""
import re
import time
from datetime import date, datetime
from typing import Optional

from fastapi import Depends
from pydantic import BaseModel, Field

from . import db

SOURCES = {"own": "Own client", "expost": "Expost"}
DATE_RE, TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$"), re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_AMOUNT = 10_000_000_000  # paise


class BookingIn(BaseModel):
    title: str = Field(max_length=150)
    date: str = Field(max_length=10)
    end_date: Optional[str] = Field(None, max_length=10)
    start_time: Optional[str] = Field(None, max_length=5)
    end_time: Optional[str] = Field(None, max_length=5)
    source: str = Field("own", max_length=10)
    client_id: Optional[int] = None
    client_name: str = Field("", max_length=150)
    phone: str = Field("", max_length=40)
    location: str = Field("", max_length=200)
    description: str = Field("", max_length=4000)
    amount: float = Field(0, ge=0)            # finalised amount
    paid: float = Field(0, ge=0)              # received so far
    payment_note: str = Field("", max_length=300)


def _date(d: str) -> bool:
    if not DATE_RE.match(d or ""):
        return False
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def payment_status(amount: int, paid: int) -> str:
    if not amount:
        return "not_finalised"
    return "paid" if paid >= amount else "part" if paid else "unpaid"


def out(r) -> dict:
    return {
        "id": r["id"], "title": r["title"], "date": r["date"], "end_date": r["end_date"], "start_time": r["start_time"],
        "end_time": r["end_time"], "source": r["source"], "source_label": SOURCES.get(r["source"], r["source"]),
        "client_id": r["client_id"], "client_name": r["client_name"], "phone": r["phone"], "location": r["location"],
        "description": r["description"], "amount": r["amount"] / 100, "paid": r["paid"] / 100,
        "balance": max(0, r["amount"] - r["paid"]) / 100, "payment_status": payment_status(r["amount"], r["paid"]),
        "payment_note": r["payment_note"], "created_by": r["creator"] or "", "updated_at": r["updated_at"],
    }


def mount_bookings(app, h):
    """h: helpers from server.create_app (user_dep, can, err)."""
    err = h.err

    def clean(b: BookingIn, u) -> dict:
        if not b.title.strip():
            raise err(422, "Add the event details (a title such as \"Wedding: Arun & Anjali\").", "title")
        if not _date(b.date):
            raise err(422, "Choose a valid date.", "date")
        if b.end_date and (not _date(b.end_date) or b.end_date < b.date):
            raise err(422, "The last day can't be before the first day.", "end_date")
        if b.end_date and (datetime.strptime(b.end_date, "%Y-%m-%d") - datetime.strptime(b.date, "%Y-%m-%d")).days > 60:
            raise err(422, "A booking can span at most 60 days.", "end_date")
        for t in (b.start_time, b.end_time):
            if t and not TIME_RE.match(t):
                raise err(422, "Use times like 18:30.", "time")
        if b.start_time and b.end_time and not b.end_date and b.end_time < b.start_time:
            raise err(422, "The end time is before the start time.", "time")
        if b.source not in SOURCES:
            raise err(422, "Choose Own client or Expost.", "source")
        if b.client_id is not None and not db.q1("SELECT 1 FROM clients WHERE id=? AND account_id=?", (b.client_id, u["account_id"])):
            raise err(404, "Client not found.", "not_found")
        amount, paid = int(round(b.amount * 100)), int(round(b.paid * 100))
        if amount > MAX_AMOUNT or paid > MAX_AMOUNT:
            raise err(422, "That amount is too large.", "amount")
        return {"title": b.title.strip(), "date": b.date, "end_date": b.end_date if b.end_date and b.end_date != b.date else None,
                "start_time": b.start_time or None, "end_time": b.end_time or None, "source": b.source, "client_id": b.client_id,
                "client_name": b.client_name.strip(), "phone": b.phone.strip(), "location": b.location.strip(),
                "description": b.description.strip(), "amount": amount, "paid": paid, "payment_note": b.payment_note.strip()}

    SQL = "SELECT b.*, COALESCE(NULLIF(u.name,''), u.email) AS creator FROM bookings b LEFT JOIN users u ON u.id=b.created_by "

    def get(u, bid):
        r = db.q1(SQL + "WHERE b.id=? AND b.account_id=?", (bid, u["account_id"]))
        if not r:
            raise err(404, "Booking not found.", "not_found")
        return r

    @app.get("/api/calendar")
    def month(start: str, end: str, u: dict = Depends(h.user_dep)):
        """Bookings and dated events that touch [start, end] (dates, inclusive), and the totals of the bookings."""
        if not (_date(start) and _date(end)) or end < start or (date.fromisoformat(end) - date.fromisoformat(start)).days > 62:
            raise err(422, "Ask for at most two months at a time.", "range")
        rows = db.q(SQL + """WHERE b.account_id=? AND b.date <= ? AND COALESCE(b.end_date, b.date) >= ?
                              ORDER BY b.date, COALESCE(b.start_time, ''), b.id""", (u["account_id"], end, start))
        bookings = [out(r) for r in rows]
        events = db.q("""SELECT e.id, e.name, e.event_date, e.status, c.name AS client_name FROM events e JOIN clients c ON c.id=e.client_id
                         WHERE e.account_id=? AND e.event_date BETWEEN ? AND ? ORDER BY e.event_date, e.id""", (u["account_id"], start, end))
        tot = {"bookings": len(bookings), "own": sum(b["source"] == "own" for b in bookings),
               "expost": sum(b["source"] == "expost" for b in bookings),
               "amount": sum(b["amount"] for b in bookings), "paid": sum(b["paid"] for b in bookings),
               "balance": sum(b["balance"] for b in bookings)}
        return {"bookings": bookings, "events": [dict(r) for r in events], "totals": tot, "sources": SOURCES}

    @app.post("/api/calendar")
    def create(body: BookingIn, u: dict = Depends(h.can("events"))):
        f, now = clean(body, u), int(time.time())
        bid = db.ex(f"""INSERT INTO bookings(account_id, created_by, created_at, updated_at, {', '.join(f)})
                        VALUES(?,?,?,?, {', '.join('?' * len(f))})""", (u["account_id"], u["user_id"], now, now, *f.values()))
        return out(get(u, bid))

    @app.patch("/api/calendar/{bid}")
    def update(bid: int, body: BookingIn, u: dict = Depends(h.can("events"))):
        get(u, bid)
        f = clean(body, u)
        db.run(f"UPDATE bookings SET {', '.join(k + '=?' for k in f)}, updated_at=? WHERE id=? AND account_id=?",
               (*f.values(), int(time.time()), bid, u["account_id"]))
        return out(get(u, bid))

    @app.delete("/api/calendar/{bid}")
    def delete(bid: int, u: dict = Depends(h.can("events"))):
        get(u, bid)
        db.run("DELETE FROM bookings WHERE id=? AND account_id=?", (bid, u["account_id"]))
        return {"ok": True}
