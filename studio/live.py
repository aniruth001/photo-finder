"""Live updates for the dashboard: the server tells open dashboards that something changed, and they refresh what is on
screen. (The same idea as SignalR in .NET, done here with Server-Sent Events: one long-lived HTTP response per open
tab, which browsers reconnect by themselves, and which needs nothing beyond what FastAPI already has.)

What is sent is only a nudge, never data: {"topic": "photos", "event": 12, "tab": "...", ...}. The page then asks the
API again for what it shows, so permissions and the usual responses apply.

Nudges come from three places:
  * any change a signed-in team member makes through the API (the middleware in server.py, after the response);
  * work done in the background: photos finishing processing, upscaling, camera and vendor uploads;
  * clients and guests: album picks, gallery visits, guest searches.

One process holds everyone's connections (the app runs as one process; see "Scaling up" in STUDIO.md).
"""
import asyncio
import json
import threading
import time
from typing import Optional

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

HEARTBEAT = 20          # seconds: keeps proxies from closing a quiet connection
MAX_PER_ACCOUNT = 100   # open dashboards per company
QUEUE = 200             # nudges waiting for one slow tab before the oldest are dropped (it refreshes anyway)


class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs = {}   # account id -> set of (loop, queue)

    def subscribe(self, account_id: int):
        q = asyncio.Queue(QUEUE)
        sub = (asyncio.get_running_loop(), q)
        with self.lock:
            group = self.subs.setdefault(account_id, set())
            if len(group) >= MAX_PER_ACCOUNT:
                return None
            group.add(sub)
        return sub

    def unsubscribe(self, account_id: int, sub):
        with self.lock:
            group = self.subs.get(account_id)
            if group:
                group.discard(sub)
                if not group:
                    del self.subs[account_id]

    def publish(self, account_id: Optional[int], topic: str, **data):
        """Tell this company's open dashboards that `topic` changed. Safe from any thread; never blocks or fails."""
        if not account_id:
            return
        msg = {"topic": topic, "at": int(time.time()), **data}
        with self.lock:
            subs = list(self.subs.get(account_id, ()))
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_put, q, msg)
            except RuntimeError:  # that connection's loop has closed
                pass

    def listeners(self, account_id: int) -> int:
        with self.lock:
            return len(self.subs.get(account_id, ()))


def _put(q: asyncio.Queue, msg: dict):
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(msg)


bus = Bus()
publish = bus.publish


def account_of_event(event_id: int) -> Optional[int]:
    from . import db

    r = db.q1("SELECT account_id FROM events WHERE id=?", (event_id,))
    return r["account_id"] if r else None


def publish_event(event_id: int, topic: str, **data):
    """For changes known by event (clients, guests, background work)."""
    try:
        publish(account_of_event(event_id), topic, event=event_id, **data)
    except Exception:
        pass


def mount_live(app, h):
    """h: helpers from server.create_app (user_dep)."""

    @app.get("/api/live")
    async def live(request: Request, once: int = 0, u: dict = Depends(h.user_dep)):
        """Server-Sent Events: a "ready" message, then one message per change. once=N ends after N changes (tests)."""
        aid = u["account_id"]
        sub = bus.subscribe(aid)

        async def stream():
            if sub is None:
                yield "retry: 60000\nevent: full\ndata: {}\n\n"
                return
            _, q = sub
            sent = 0
            try:
                yield f"retry: 3000\nevent: ready\ndata: {json.dumps({'listeners': bus.listeners(aid)})}\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), HEARTBEAT)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": still here\n\n"
                        continue
                    yield f"data: {json.dumps(msg)}\n\n"
                    sent += 1
                    if once and sent >= once:
                        return
            finally:
                bus.unsubscribe(aid, sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
