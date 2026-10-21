"""Billing: estimates (quotations) and invoices for clients. Packages with descriptions and costs, a total, a PDF.

Both kinds share one table and one editor. Each gets the next number of its kind for that company (EST-0001, ... and
INV-0001, ...); an estimate can be turned into an invoice, which then names the estimate it came from. The
company name printed on it is chosen per estimate (it defaults to the company's name), and the logo is shown when
the company has one. Amounts are stored in paise (whole numbers), so totals never pick up rounding errors.
"""
import io
import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from fastapi import Depends, Response
from pydantic import BaseModel, Field

from . import branding, db

FONTS = Path(__file__).parent / "fonts"     # Noto Sans (SIL Open Font License, fonts/OFL.txt): has the ₹ sign
CURRENCIES = ("₹", "$", "€", "£")
MAX_AMOUNT = 10_000_000_000                  # ₹ 10 crore per line, in paise: far above any real package
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = {  # what changes between the two kinds: the number prefix and the words on the page and the PDF
    "estimate": {"prefix": "EST", "title": "ESTIMATE", "noun": "Estimate", "date": "Date", "until": "Valid until",
                 "to": "PREPARED FOR", "total": "Total package cost"},
    "invoice": {"prefix": "INV", "title": "INVOICE", "noun": "Invoice", "date": "Invoice date", "until": "Due date",
                "to": "BILL TO", "total": "Total amount due"},
}


class ItemIn(BaseModel):
    name: str = Field(max_length=150)
    description: str = Field("", max_length=3000)
    amount: float = Field(0, ge=0)


class EstimateIn(BaseModel):
    kind: str = Field("estimate", max_length=10)    # only used when creating; a document keeps its kind
    company_name: str = Field(max_length=150)
    company_details: str = Field("", max_length=1000)
    show_logo: bool = True
    client_id: Optional[int] = None
    client_name: str = Field(max_length=150)
    client_details: str = Field("", max_length=1000)
    event_title: str = Field("", max_length=200)
    estimate_date: str = Field(max_length=10)
    valid_until: Optional[str] = Field(None, max_length=10)
    currency: str = Field("₹", max_length=3)
    items: List[ItemIn] = Field(default_factory=list, max_length=60)
    discount: float = Field(0, ge=0)
    tax_label: str = Field("GST", max_length=40)
    tax_pct: float = Field(0, ge=0, le=100)
    notes: str = Field("", max_length=4000)


def paise(x: float) -> int:
    return int(round(x * 100))


def totals(items: list, discount: int, tax_pct: float) -> dict:
    sub = sum(i["amount"] for i in items)
    disc = min(discount, sub)
    tax = int(round((sub - disc) * tax_pct / 100))
    return {"subtotal": sub, "discount": disc, "tax": tax, "total": sub - disc + tax}


def label(number: int, kind: str = "estimate") -> str:
    return f"{KINDS[kind]['prefix']}-{number:04d}"


def money(p: int, cur: str = "₹") -> str:
    """Paise as text. Rupees use Indian grouping (₹ 1,50,000); the others use thousands (€ 150,000)."""
    neg, p = p < 0, abs(p)
    whole, frac = divmod(p, 100)
    s = str(whole)
    if cur == "₹" and len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    else:
        s = f"{whole:,}"
    return f"{'-' if neg else ''}{cur} {s}" + (f".{frac:02d}" if frac else "")


def nice_date(d: Optional[str]) -> str:
    if not d:
        return ""
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return d


def out(r) -> dict:
    items = json.loads(r["items"] or "[]")
    t = totals(items, r["discount"], r["tax_pct"])
    return {
        "id": r["id"], "kind": r["kind"], "number": r["number"], "label": label(r["number"], r["kind"]), "ref": r["ref"],
        "company_name": r["company_name"], "company_details": r["company_details"], "show_logo": bool(r["show_logo"]),
        "client_id": r["client_id"], "client_name": r["client_name"], "client_details": r["client_details"],
        "event_title": r["event_title"], "estimate_date": r["estimate_date"], "valid_until": r["valid_until"],
        "currency": r["currency"], "tax_label": r["tax_label"], "tax_pct": r["tax_pct"], "notes": r["notes"],
        "items": [{"name": i["name"], "description": i["description"], "amount": i["amount"] / 100} for i in items],
        "discount": r["discount"] / 100, **{k: v / 100 for k, v in t.items()},
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


# ------------------------------------------------------------------ PDF
_fonts_ready = False


def _fonts():
    global _fonts_ready
    if not _fonts_ready:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        pdfmetrics.registerFont(TTFont("Noto", str(FONTS / "NotoSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Noto-Bold", str(FONTS / "NotoSans-Bold.ttf")))
        for bold, italic in ((0, 0), (0, 1)):
            addMapping("Noto", bold, italic, "Noto")
        for bold, italic in ((1, 0), (1, 1)):
            addMapping("Noto", bold, italic, "Noto-Bold")
        _fonts_ready = True


def _accent(hexcolor: str):
    """The company's brand colour, darkened until it reads as text on white (at least 4.5:1)."""
    from reportlab.lib.colors import HexColor

    h = (hexcolor or "#1d1b16").lstrip("#")
    try:
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        rgb = [0.11, 0.1, 0.09]

    def lum(c):
        lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    for _ in range(30):
        if 1.05 / (lum(rgb) + 0.05) >= 4.5:
            break
        rgb = [v * 0.9 for v in rgb]
    return HexColor("#" + "".join(f"{round(v * 255):02x}" for v in rgb))


def _para(text: str) -> str:
    return escape(text or "").replace("\n", "<br/>")


def render_pdf(e: dict, account) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    _fonts()
    accent, ink, muted, line = _accent(account["brand_color"]), colors.HexColor("#1d1b16"), colors.HexColor("#6b665c"), colors.HexColor("#e4e0d8")
    soft = colors.HexColor("#f6f4ef")
    cur, K = e["currency"], KINDS[e["kind"]]
    S = lambda name, **kw: ParagraphStyle(name, **{"fontName": "Noto", "fontSize": 9.5, "leading": 13.5, "textColor": ink, **kw})
    body, small, small_r = S("b"), S("s", fontSize=8.5, leading=12, textColor=muted), S("sr", fontSize=8.5, leading=12, textColor=muted, alignment=TA_RIGHT)
    right, h_co = S("r", alignment=TA_RIGHT), S("co", fontName="Noto-Bold", fontSize=15, leading=19)
    title = S("t", fontName="Noto-Bold", fontSize=22, leading=26, textColor=accent, alignment=TA_RIGHT)
    head = S("h", fontName="Noto-Bold", fontSize=8, leading=11, textColor=muted)
    W = A4[0] - 36 * mm - 12  # the page frame's width inside its 6 pt padding, so tables line up with the text

    # who it is from: the logo (if any, and wanted), the chosen company name, its details
    left = []
    logo = branding.logo_file(account["id"], account["logo"]) if account["logo"] else None
    if e["show_logo"] and logo and logo.exists():
        from PIL import Image as PILImage
        with PILImage.open(logo) as im:
            w, h = im.size
        scale = min(55 * mm / w, 20 * mm / h)
        img = Image(str(logo), width=w * scale, height=h * scale)
        img.hAlign = "LEFT"
        left += [img, Spacer(1, 3 * mm)]
    left.append(Paragraph(_para(e["company_name"]), h_co))
    if e["company_details"].strip():
        left.append(Paragraph(_para(e["company_details"]), small))

    meta = [[f"{K['noun']} no.", e["label"]], [K["date"], nice_date(e["estimate_date"])]]
    if e["valid_until"]:
        meta.append([K["until"], nice_date(e["valid_until"])])
    if e["ref"]:
        meta.append(["Estimate ref.", e["ref"]])
    meta_t = Table([[Paragraph(k, small_r), Paragraph(f"<b>{escape(v)}</b>", right)] for k, v in meta],
                   colWidths=[28 * mm, 34 * mm], hAlign="RIGHT")
    meta_t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    top = Table([[left, [Paragraph(K["title"], title), Spacer(1, 3 * mm), meta_t]]], colWidths=[W * 0.58, W * 0.42])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("LINEBELOW", (0, 0), (-1, 0), 1.5, accent),
                             ("BOTTOMPADDING", (0, 0), (-1, 0), 5 * mm)]))
    story = [top, Spacer(1, 6 * mm)]

    # who it is for
    to = [Paragraph(K["to"], head), Paragraph(f"<b>{_para(e['client_name'])}</b>", body)]
    if e["client_details"].strip():
        to.append(Paragraph(_para(e["client_details"]), small))
    ev = [Paragraph("EVENT", head), Paragraph(_para(e["event_title"]), body)] if e["event_title"].strip() else []
    who = Table([[to, ev]], colWidths=[W * 0.58, W * 0.42])
    who.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [who, Spacer(1, 7 * mm)]

    # the packages
    rows = [[Paragraph("#", head), Paragraph("PACKAGE", head), Paragraph("COST", S("hr", fontName="Noto-Bold", fontSize=8, leading=11, textColor=muted, alignment=TA_RIGHT))]]
    for n, it in enumerate(e["items"], 1):
        cell = [Paragraph(f"<b>{_para(it['name'])}</b>", body)]
        if it["description"].strip():
            cell.append(Paragraph(_para(it["description"]), small))
        rows.append([Paragraph(str(n), small), cell, Paragraph(money(paise(it["amount"]), cur), right)])
    if len(rows) == 1:
        rows.append(["", Paragraph("No packages yet.", small), ""])
    items_t = Table(rows, colWidths=[9 * mm, W - 9 * mm - 38 * mm, 38 * mm], repeatRows=1)
    items_t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), soft),
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, line), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    story += [items_t, Spacer(1, 4 * mm)]

    # the totals
    t = [["Subtotal", money(paise(e["subtotal"]), cur)]]
    if e["discount"]:
        t.append(["Discount", "- " + money(paise(e["discount"]), cur)])
    if e["tax_pct"]:
        pct = f"{e['tax_pct']:g}"
        t.append([f"{e['tax_label'] or 'Tax'} ({pct}%)", money(paise(e["tax"]), cur)])
    trows = [[Paragraph(escape(k), small_r), Paragraph(escape(v), right)] for k, v in t]
    trows.append([Paragraph(f"<b>{K['total']}</b>", S("tl", fontName="Noto-Bold", fontSize=10.5, leading=14, alignment=TA_RIGHT)),
                  Paragraph(f"<b>{escape(money(paise(e['total']), cur))}</b>", S("tv", fontName="Noto-Bold", fontSize=13, leading=17, textColor=accent, alignment=TA_RIGHT))])
    tot = Table(trows, colWidths=[52 * mm, 38 * mm], hAlign="RIGHT")
    tot.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 1.2, accent), ("TOPPADDING", (0, 0), (-1, -1), 3),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, -1), (-1, -1), 7),
                             ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    story += [KeepTogether([tot]), Spacer(1, 9 * mm)]

    if e["notes"].strip():
        story.append(KeepTogether([Paragraph("NOTES &amp; TERMS", head), Spacer(1, 1.5 * mm), Paragraph(_para(e["notes"]), small)]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Noto", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(18 * mm + 6, 11 * mm, f"{e['label']} · {e['company_name']}"[:110])
        canvas.drawRightString(A4[0] - 18 * mm - 6, 11 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"{K['noun']} {e['label']} for {e['client_name']}", author=e["company_name"])
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ------------------------------------------------------------------ API
def mount_estimates(app, h):
    """h: helpers from server.create_app (user_dep, can, err)."""
    err = h.err

    def clean(body: EstimateIn, u) -> dict:
        if body.kind not in KINDS:
            raise err(422, "Choose an estimate or an invoice.", "kind")
        noun = KINDS[body.kind]["noun"].lower()
        if not body.company_name.strip():
            raise err(422, f"Enter the company name to print on the {noun}.", "company_name")
        if not body.client_name.strip():
            raise err(422, "Enter the client's name.", "client_name")
        for d in (body.estimate_date, body.valid_until):
            if d and not (DATE_RE.match(d) and _is_date(d)):
                raise err(422, "Use a valid date.", "date")
        if body.valid_until and body.valid_until < body.estimate_date:
            raise err(422, f"\"{KINDS[body.kind]['until']}\" can't be before the {noun} date.", "valid_until")
        if body.currency not in CURRENCIES:
            raise err(422, "Choose ₹, $, € or £.", "currency")
        if body.client_id is not None and not db.q1("SELECT 1 FROM clients WHERE id=? AND account_id=?", (body.client_id, u["account_id"])):
            raise err(404, "Client not found.", "not_found")
        items = []
        for i in body.items:
            if not i.name.strip():
                if i.description.strip() or i.amount:
                    raise err(422, "Every package needs a name.", "items")
                continue  # an empty row the user left behind
            if paise(i.amount) > MAX_AMOUNT:
                raise err(422, "That cost is too large.", "items")
            items.append({"name": i.name.strip(), "description": i.description.rstrip(), "amount": paise(i.amount)})
        if paise(body.discount) > MAX_AMOUNT:
            raise err(422, "That discount is too large.", "discount")
        return {"company_name": body.company_name.strip(), "company_details": body.company_details.strip(),
                "show_logo": int(body.show_logo), "client_id": body.client_id, "client_name": body.client_name.strip(),
                "client_details": body.client_details.strip(), "event_title": body.event_title.strip(),
                "estimate_date": body.estimate_date, "valid_until": body.valid_until or None, "currency": body.currency,
                "items": json.dumps(items, ensure_ascii=False), "discount": paise(body.discount),
                "tax_label": body.tax_label.strip(), "tax_pct": round(body.tax_pct, 3), "notes": body.notes.strip()}

    def get(u, eid):
        r = db.q1("SELECT * FROM estimates WHERE id=? AND account_id=?", (eid, u["account_id"]))
        if not r:
            raise err(404, "Not found.", "not_found")
        return r

    def next_number(account_id, kind):
        r = db.q1("SELECT last FROM doc_numbers WHERE account_id=? AND kind=?", (account_id, kind))
        return (r["last"] if r else 0) + 1

    def insert(u, kind: str, f: dict, ref: str = "") -> int:
        with db.tx():  # the next number of this kind for this company, without a race between two people saving at once
            n, now = next_number(u["account_id"], kind), int(time.time())
            db.run("""INSERT INTO doc_numbers(account_id, kind, last) VALUES(?,?,?)
                      ON CONFLICT(account_id, kind) DO UPDATE SET last=excluded.last""", (u["account_id"], kind, n))
            cols = ["account_id", "kind", "number", "ref", "created_by", "created_at", "updated_at", *f]
            return db.ex(f"INSERT INTO estimates({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                         (u["account_id"], kind, n, ref, u["user_id"], now, now, *f.values()))

    def today(u) -> str:
        try:
            return (datetime.now(ZoneInfo(u["timezone"])).date() if u["timezone"] else date.today()).isoformat()
        except Exception:
            return date.today().isoformat()

    @app.get("/api/estimates")
    def list_estimates(u: dict = Depends(h.user_dep)):
        return [{k: e[k] for k in ("id", "kind", "number", "label", "ref", "client_name", "event_title", "estimate_date",
                                   "valid_until", "currency", "total", "updated_at")} | {"packages": len(e["items"])}
                for e in map(out, db.q("SELECT * FROM estimates WHERE account_id=? ORDER BY estimate_date DESC, id DESC",
                                       (u["account_id"],)))]

    @app.get("/api/estimates/defaults")
    def defaults(u: dict = Depends(h.user_dep)):
        """What a new document starts with: the company's name, and the details, terms and tax of its last one.
        `labels` is the number each kind would get, so the editor can show it as the kind is switched."""
        last = db.q1("SELECT * FROM estimates WHERE account_id=? ORDER BY id DESC LIMIT 1", (u["account_id"],))
        labels = {k: label(next_number(u["account_id"], k), k) for k in KINDS}
        return {"labels": labels, "label": labels["estimate"], "company_name": last["company_name"] if last else u["company"],
                "company_details": last["company_details"] if last else "", "show_logo": bool(last["show_logo"]) if last else True,
                "currency": last["currency"] if last else "₹", "tax_label": last["tax_label"] if last else "GST",
                "tax_pct": last["tax_pct"] if last else 0, "notes": last["notes"] if last else "",
                "estimate_date": today(u), "has_logo": bool(u["logo"])}

    @app.post("/api/estimates")
    def create(body: EstimateIn, u: dict = Depends(h.can("clients"))):
        return out(get(u, insert(u, body.kind if body.kind in KINDS else "", clean(body, u))))

    @app.get("/api/estimates/{eid}")
    def read(eid: int, u: dict = Depends(h.user_dep)):
        return {**out(get(u, eid)), "has_logo": bool(u["logo"])}

    @app.patch("/api/estimates/{eid}")
    def update(eid: int, body: EstimateIn, u: dict = Depends(h.can("clients"))):
        body.kind = get(u, eid)["kind"]  # a document keeps its kind (and number); make an invoice from an estimate instead
        f = clean(body, u)
        db.run(f"UPDATE estimates SET {', '.join(k + '=?' for k in f)}, updated_at=? WHERE id=? AND account_id=?",
               (*f.values(), int(time.time()), eid, u["account_id"]))
        return out(get(u, eid))

    @app.post("/api/estimates/{eid}/copy")
    def copy(eid: int, kind: Optional[str] = None, u: dict = Depends(h.can("clients"))):
        """Duplicate (same kind), or ?kind=invoice to bill an estimate: dated today, no due date yet, naming the estimate."""
        r = dict(get(u, eid))
        kind = kind or r["kind"]
        if kind not in KINDS:
            raise err(422, "Choose an estimate or an invoice.", "kind")
        f = {k: r[k] for k in ("company_name", "company_details", "show_logo", "client_id", "client_name", "client_details",
                               "event_title", "estimate_date", "valid_until", "currency", "items", "discount", "tax_label",
                               "tax_pct", "notes")}
        ref = r["ref"]
        if kind != r["kind"]:
            f["estimate_date"], f["valid_until"] = today(u), None
            ref = label(r["number"], r["kind"]) if r["kind"] == "estimate" else ""
        return out(get(u, insert(u, kind, f, ref)))

    @app.delete("/api/estimates/{eid}")
    def delete(eid: int, u: dict = Depends(h.can("clients"))):
        get(u, eid)
        db.run("DELETE FROM estimates WHERE id=? AND account_id=?", (eid, u["account_id"]))
        return {"ok": True}

    @app.get("/api/estimates/{eid}/pdf")
    def pdf(eid: int, download: bool = False, u: dict = Depends(h.user_dep)):
        e = out(get(u, eid))
        account = db.q1("SELECT id, brand_color, logo FROM accounts WHERE id=?", (u["account_id"],))
        name = re.sub(r"[^A-Za-z0-9]+", "-", f"{KINDS[e['kind']]['noun']} {e['label']} {e['client_name']}").strip("-")[:90] + ".pdf"
        return Response(render_pdf(e, account), media_type="application/pdf",
                        headers={"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"',
                                 "Cache-Control": "no-store"})


def _is_date(d: str) -> bool:
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return True
    except ValueError:
        return False
