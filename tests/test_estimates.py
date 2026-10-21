"""Estimates: packages, totals, numbering, who may change them, tenant isolation, and the PDF."""
import io

import pytest

from conftest import jpeg_bytes
from studio import db
from studio.estimates import money, totals


def est(**kw):
    body = {"company_name": "Haspit Studio", "company_details": "12 MG Road, Chennai\n+91 98400 00000",
            "client_name": "Arun & Anjali", "client_details": "arun@example.com", "event_title": "Wedding · 2-3 Nov 2026",
            "estimate_date": "2026-10-21", "valid_until": "2026-11-05",
            "items": [{"name": "Candid photography", "description": "2 photographers\n800+ edited photos", "amount": 85000},
                      {"name": "Traditional video", "description": "Full-HD, 3-minute highlight", "amount": 65000.5}],
            "discount": 10000, "tax_label": "GST", "tax_pct": 18, "notes": "50% advance to confirm the booking."}
    body.update(kw)
    return body


def test_money_and_totals():
    assert money(15000000) == "₹ 1,50,000" and money(123456789) == "₹ 12,34,567.89" and money(99900) == "₹ 999"
    assert money(15000000, "$") == "$ 150,000"
    t = totals([{"amount": 8500000}, {"amount": 6500050}], 1000000, 18)
    assert t == {"subtotal": 15000050, "discount": 1000000, "tax": 2520009, "total": 16520059}
    assert totals([{"amount": 100}], 500, 0)["total"] == 0                          # a discount never goes below zero


def test_create_list_edit_copy_delete(studio):
    s, cid, ev = studio
    d = s.get("/api/estimates/defaults").json()
    assert d["label"] == "EST-0001" and d["company_name"] and d["currency"] == "₹" and len(d["estimate_date"]) == 10
    e = s.post("/api/estimates", json=est(client_id=cid)).json()
    assert e["label"] == "EST-0001" and e["subtotal"] == 150000.5 and e["discount"] == 10000
    assert e["tax"] == 25200.09 and e["total"] == 165200.59 and e["items"][1]["amount"] == 65000.5
    d = s.get("/api/estimates/defaults").json()                                     # the next one starts from this one
    assert {k: d[k] for k in ("label", "company_name", "notes", "tax_pct")} == {
        "label": "EST-0002", "company_name": "Haspit Studio", "notes": "50% advance to confirm the booking.", "tax_pct": 18}
    e2 = s.post(f"/api/estimates/{e['id']}/copy").json()
    assert e2["label"] == "EST-0002" and e2["total"] == e["total"] and e2["client_name"] == "Arun & Anjali"
    upd = s.patch(f"/api/estimates/{e2['id']}", json=est(client_name="Meera", items=[{"name": "Album", "amount": 12000}, {"name": "", "amount": 0}],
                                                        discount=0, tax_pct=0)).json()
    assert upd["client_name"] == "Meera" and upd["total"] == 12000 and len(upd["items"]) == 1   # the empty row is dropped
    rows = s.get("/api/estimates").json()
    assert [r["label"] for r in rows] == ["EST-0002", "EST-0001"] and rows[0]["total"] == 12000 and rows[1]["packages"] == 2
    assert s.delete(f"/api/estimates/{e['id']}").json() == {"ok": True}
    assert s.get(f"/api/estimates/{e['id']}").status_code == 404
    assert s.post("/api/estimates", json=est()).json()["label"] == "EST-0003"        # numbers are never reused


def test_validation(studio):
    s, _, _ = studio
    for bad, code in ((est(client_name=" "), "client_name"), (est(company_name=""), "company_name"),
                      (est(estimate_date="2026-02-30"), "date"), (est(valid_until="2026-10-01"), "valid_until"),
                      (est(currency="X"), "currency"), (est(items=[{"name": "", "amount": 5}]), "items"),
                      (est(client_id=99999), "not_found")):
        r = s.post("/api/estimates", json=bad)
        assert r.status_code in (404, 422) and r.json()["error"] == code, (code, r.json())
    assert s.post("/api/estimates", json=est(items=[{"name": "x", "amount": -1}])).status_code == 422


def test_roles_and_other_companies(studio, make_user):
    s, _, _ = studio
    e = s.post("/api/estimates", json=est()).json()
    other = make_user("other@x.test", "Other Co")
    assert other.get(f"/api/estimates/{e['id']}").status_code == 404
    assert other.get(f"/api/estimates/{e['id']}/pdf").status_code == 404
    assert other.get("/api/estimates").json() == []
    assert other.post("/api/estimates", json=est()).json()["label"] == "EST-0001"    # numbering is per company
    me = s.get("/api/auth/me").json()["user"]["email"]
    db.run("UPDATE users SET role='photographer' WHERE email=?", (me,))
    assert s.get(f"/api/estimates/{e['id']}").status_code == 200                      # can read and download
    assert s.get(f"/api/estimates/{e['id']}/pdf").status_code == 200
    assert s.patch(f"/api/estimates/{e['id']}", json=est()).status_code == 403          # can't change
    assert s.post("/api/estimates", json=est()).status_code == 403
    assert s.delete(f"/api/estimates/{e['id']}").status_code == 403


def text_of(pdf: bytes) -> str:
    pypdf = pytest.importorskip("pypdf")  # test-only: pip install pypdf
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)


def test_pdf(studio):
    s, _, _ = studio
    e = s.post("/api/estimates", json=est()).json()
    r = s.get(f"/api/estimates/{e['id']}/pdf?download=1")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert 'attachment; filename="Estimate-EST-0001-Arun-Anjali.pdf"' == r.headers["content-disposition"]
    t = text_of(r.content)
    for want in ("ESTIMATE", "EST-0001", "21 Oct 2026", "5 Nov 2026", "Haspit Studio", "Arun & Anjali", "Wedding",
                 "Candid photography", "800+ edited photos", "₹ 85,000", "₹ 65,000.50", "Discount", "GST (18%)",
                 "Total package cost", "₹ 1,65,200.59", "50% advance"):
        assert want in t, want
    assert s.get(f"/api/estimates/{e['id']}/pdf").headers["content-disposition"].startswith("inline")
    many = est(items=[{"name": f"Package {i}", "description": "line\n" * 6, "amount": 1000} for i in range(40)])
    big = s.get(f"/api/estimates/{s.post('/api/estimates', json=many).json()['id']}/pdf")
    assert big.status_code == 200 and "Page 2" in text_of(big.content)               # long estimates run onto more pages


def test_pdf_shows_the_logo(studio):
    s, _, _ = studio
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 160), (200, 30, 30)).save(buf, "PNG")
    r = s.post("/api/account/logo", files={"file": ("logo.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    e = s.post("/api/estimates", json=est()).json()
    with_logo = s.get(f"/api/estimates/{e['id']}/pdf").content
    e2 = s.post("/api/estimates", json=est(show_logo=False)).json()
    without = s.get(f"/api/estimates/{e2['id']}/pdf").content
    assert b"/Subtype /Image" in with_logo and b"/Subtype /Image" not in without


def test_invoices_are_numbered_on_their_own_and_made_from_estimates(studio):
    s, _, _ = studio
    d = s.get("/api/estimates/defaults").json()
    assert d["labels"] == {"estimate": "EST-0001", "invoice": "INV-0001"}
    e = s.post("/api/estimates", json=est()).json()
    inv = s.post("/api/estimates", json=est(kind="invoice", valid_until="2026-11-20")).json()
    assert (e["kind"], e["label"], inv["kind"], inv["label"]) == ("estimate", "EST-0001", "invoice", "INV-0001")
    assert s.get("/api/estimates/defaults").json()["labels"] == {"estimate": "EST-0002", "invoice": "INV-0002"}
    billed = s.post(f"/api/estimates/{e['id']}/copy?kind=invoice").json()          # bill the estimate
    assert (billed["kind"], billed["label"], billed["ref"], billed["total"]) == ("invoice", "INV-0002", "EST-0001", e["total"])
    assert billed["valid_until"] is None and billed["items"] == e["items"]
    again = s.post(f"/api/estimates/{billed['id']}/copy").json()                     # duplicate keeps the kind
    assert (again["kind"], again["label"], again["ref"]) == ("invoice", "INV-0003", "EST-0001")
    kept = s.patch(f"/api/estimates/{e['id']}", json=est(kind="invoice")).json()    # editing never changes the kind
    assert (kept["kind"], kept["label"]) == ("estimate", "EST-0001")
    assert s.post("/api/estimates", json=est(kind="receipt")).json()["error"] == "kind"
    assert s.post(f"/api/estimates/{e['id']}/copy?kind=receipt").status_code == 422
    assert {r["label"]: r["kind"] for r in s.get("/api/estimates").json()} == {
        "EST-0001": "estimate", "INV-0001": "invoice", "INV-0002": "invoice", "INV-0003": "invoice"}


def test_invoice_pdf_words(studio):
    s, _, _ = studio
    e = s.post("/api/estimates", json=est()).json()
    inv = s.post(f"/api/estimates/{e['id']}/copy?kind=invoice").json()
    s.patch(f"/api/estimates/{inv['id']}", json=est(estimate_date="2026-10-25", valid_until="2026-11-10"))
    r = s.get(f"/api/estimates/{inv['id']}/pdf?download=1")
    assert r.headers["content-disposition"] == 'attachment; filename="Invoice-INV-0001-Arun-Anjali.pdf"'
    t = text_of(r.content)
    for want in ("INVOICE", "Invoice no.", "INV-0001", "Invoice date", "25 Oct 2026", "Due date", "10 Nov 2026",
                 "Estimate ref.", "EST-0001", "BILL TO", "Total amount due", "₹ 1,65,200.59"):
        assert want in t, want
    assert "ESTIMATE" not in t and "PREPARED FOR" not in t
    assert s.patch(f"/api/estimates/{inv['id']}", json=est(estimate_date="2026-10-25", valid_until="2026-10-01")).json()["message"] \
        == "\"Due date\" can't be before the invoice date."
