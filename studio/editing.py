"""Photo editing: the studio opens a photo in the editor (studio/static/editor.html), edits it in the browser and saves
it back to the event.

An original upload is never overwritten. The first save makes an edited copy in the same event; saving again updates
that copy in place. A copy made from a copy still points at the original it came from. Edited copies are photos of the
event like the others, tagged Edited wherever they are shown: on the dashboard (with the photo they came from), in the
client gallery and, when a guest's selfie matches them, on the guest page. They can go in the album.

The editor gets the photo through editor/source at its original size: a JPEG, PNG or WebP is sent exactly as uploaded
(the browser turns it the right way up from its EXIF, as everywhere else); HEIC and camera RAW, which browsers can't
show, are converted at full size to a high-quality JPEG. Edited copies are saved in the original's format: a PNG stays
a PNG (with its transparency), a WebP a WebP, everything else JPEG.

A big original takes a while to arrive over a slow connection (40 MB over a home upload line through a tunnel can take
minutes), so the editor first opens editor/preview, the photo at screen size (a few hundred KB, ready in a second),
and fetches the original behind it. Adjustments made meanwhile are steps, replayed on the original when it arrives;
Save always waits for the original. editor/source?v=... may be kept by the browser, so opening it again is instant.

Recipes: whole-photo steps (adjustments, curves, a look, straighten, crop, watermark...) are recorded as JSON, a
"recipe". A copy saved with its recipe can be re-edited later with the sliders where they were left: the editor
opens the original and replays the steps. Recipes saved by name are presets ("Warm wedding"), shared by the team,
and the dashboard can apply one to many photos at once (the editor does the work in the browser, photo by photo,
and saves each as an edited copy).
"""
import json
import hashlib
import threading
from collections import OrderedDict
import io
import os
import sqlite3
import time
from pathlib import Path
from typing import List

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import branding, config, db
from .ingest import add_photo, photo_usage, storage_full, storage_quota
from .storage import get_storage

STATIC = Path(__file__).parent / "static"
AS_IS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}   # browsers open these
SAVE_EXT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}
PREVIEW_SIDE = 2560     # editor/preview: sharp on a 4K screen, a few hundred KB
PREVIEW_MAX_SIDE = 4096
_previews, _previews_lock = OrderedDict(), threading.Lock()   # (photo id, version, side) -> (bytes, media type)
CACHED = "private, max-age=604800"   # editor/source?v=... and editor/preview?v=...: the file for that version never changes


def make_preview(path: Path, ext: str, side: int):
    """The photo the right way up, at most `side` pixels long: (bytes, media type). JPEG, or PNG when it has
    transparency. A JPEG is decoded at a reduced scale straight away (fast, even for 100 MP)."""
    from PIL import Image, ImageOps

    from faces import is_raw, open_image, raw_preview

    if is_raw(path):
        im = raw_preview(path, 10 ** 9)   # a quick develop, the same colours as the full-size one
    elif ext in AS_IS:
        im = Image.open(path)
        if im.format == "JPEG":
            im.draft("RGB", (side, side))
        im = ImageOps.exif_transpose(im)
    else:
        im = open_image(path)
    im.thumbnail((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im.convert("RGBA").save(buf, "PNG", compress_level=3)
        return buf.getvalue(), "image/png"
    im.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


def save_format(ext: str) -> str:
    """The format an edit of a file with this extension is saved in."""
    return {".png": "png", ".webp": "webp"}.get((ext or "").lower(), "jpeg")


def sniff(data: bytes):
    if data.startswith(JPEG):
        return "jpeg"
    if data.startswith(PNG):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None
JPEG = b"\xff\xd8\xff"
PNG = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])
RECIPE_OPS = {  # the steps a recipe may hold (studio/static/editor.html, RECIPE)
    "adjust", "levels", "curves", "hsl", "tone", "wb", "autofix", "autoContrast", "grayscale", "sepia", "invert",
    "blur", "sharpen", "noise", "vignette", "lut", "straighten", "crop", "rotate", "flip", "watermark",
    "bc", "exposure", "vibrance", "hueSat", "colorBalance", "photoFilter", "channelMixer", "gradientMap", "selectiveColor",
    "bwMix", "equalize", "replaceColor"}
MAX_RECIPE = 2 << 20   # a colour look (.cube) is the big part: 33 x 33 x 33 colours
MAX_STEPS = 60


def clean_recipe(text: str, err) -> str:
    """A recipe as the editor sent it, checked: JSON, known steps, a sane size. Returned as compact JSON."""
    if len(text) > MAX_RECIPE:
        raise err(413, "That recipe is too large.", "too_large")
    try:
        r = json.loads(text)
        steps = r["steps"]
        assert isinstance(steps, list) and len(steps) <= MAX_STEPS
        for st in steps:
            assert isinstance(st, dict) and st.get("op") in RECIPE_OPS and isinstance(st.get("v", {}), dict)
    except Exception:
        raise err(422, "That recipe couldn't be read.", "bad_recipe")
    return json.dumps({"v": 1, "steps": steps}, separators=(",", ":"))


class PresetIn(BaseModel):
    name: str
    recipe: str
    replace: bool = False


def mount_editing(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, plan_of, poke)."""
    err = h.err

    def photo(u, eid: int, pid: str):
        h.get_event(u, eid)
        r = db.q1("""SELECT p.*, src.public_id AS source_id, src.orig_name AS source_name FROM photos p
                     LEFT JOIN photos src ON src.id=p.edited_of WHERE p.public_id=? AND p.event_id=? AND p.account_id=?""",
                  (pid, eid, u["account_id"]))
        if not r:
            raise err(404, "Photo not found.", "not_found")
        return r

    def out(r) -> dict:
        return {"id": r["public_id"], "name": r["orig_name"], "size": r["size"], "status": r["status"], "width": r["width"],
                "height": r["height"], "edited": bool(r["edited"]),
                "source": {"id": r["source_id"], "name": r["source_name"]} if r["source_id"] else None, "v": r["edited_at"] or 0,
                "recipe": json.loads(r["edit_recipe"]) if r["edit_recipe"] else None,
                "format": save_format(r["ext"]), "ext": r["ext"]}

    def edited_image(file: UploadFile):
        """What the editor sends: a JPEG, PNG or WebP that opens, within the upload limit. Returns (bytes, format)."""
        limit = config.settings.max_file_mb * 1024 * 1024
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise err(413, f"The edited photo is larger than {config.settings.max_file_mb} MB.", "too_large")
        fmt = sniff(data)
        if not fmt:
            raise err(422, "Send the edited photo as a JPEG, PNG or WebP.", "not_jpeg")
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception:
            raise err(422, "The edited photo couldn't be read. Try saving again.", "bad_image")
        return data, fmt

    # ------------------------------------------------------------------ the editor page (its API calls need a login)
    @app.get("/editor", response_class=HTMLResponse)
    def editor(request: Request):
        page = (STATIC / "editor.html").read_text(encoding="utf-8").replace(
            "<!--ORIGCACHE-->", "<script>" + (STATIC / "origcache.js").read_text(encoding="utf-8") + "</script>")
        page = branding.with_nonce(page, request.state.nonce)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/editor/vendor/ag-psd-33.0.0.js")
    def ag_psd():
        """ag-psd (MIT, studio/static/vendor): reads and writes Photoshop files in the editor. Served from here, as the
        editor's pages may only run their own scripts."""
        return FileResponse(STATIC / "vendor" / "ag-psd-33.0.0.js", media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/events/{eid}/photos/{pid}/editor/info")  # not .../{pid}/{kind}: that route serves thumb/web/orig
    def info(eid: int, pid: str, u: dict = Depends(h.user_dep)):
        e = h.get_event(u, eid)
        return {**out(photo(u, eid, pid)), "event": {"id": e["id"], "name": e["name"]}}

    def original(u, eid: int, pid: str):
        r = photo(u, eid, pid)
        if r["status"] != "ready":
            raise err(409, "This photo is still being processed. Try again in a moment.", "not_ready")
        path = get_storage().photo_path(r, "orig")
        if not path.exists():
            raise err(404, "Photo not found.", "not_found")
        return r, path, (r["ext"] or "").lower()

    def caching(r, v: str) -> str:   # only the URL of the photo's current version may be kept
        return CACHED if v and v == str(r["edited_at"] or 0) else "no-store"

    @app.get("/api/events/{eid}/photos/{pid}/editor/preview")
    def preview(eid: int, pid: str, v: str = "", side: int = PREVIEW_SIDE, u: dict = Depends(h.user_dep)):
        """The photo at screen size, for opening the editor at once while the original is on its way."""
        r, path, ext = original(u, eid, pid)
        side = max(256, min(side, PREVIEW_MAX_SIDE))
        key = (r["id"], r["edited_at"] or 0, side)
        with _previews_lock:
            hit = _previews.get(key)
            if hit:
                _previews.move_to_end(key)
        if not hit:
            try:
                hit = make_preview(path, ext, side)
            except Exception:
                raise err(422, "This photo couldn't be opened for editing.", "bad_image")
            with _previews_lock:
                _previews[key] = hit
                while len(_previews) > 40:
                    _previews.popitem(last=False)
        return Response(hit[0], media_type=hit[1], headers={"Cache-Control": caching(r, v)})

    @app.get("/api/events/{eid}/photos/{pid}/editor/source")
    def source(eid: int, pid: str, v: str = "", u: dict = Depends(h.user_dep)):
        r, path, ext = original(u, eid, pid)
        if ext in AS_IS:   # the original file itself: its full size, its format, its quality
            return FileResponse(path, media_type=AS_IS[ext], headers={"Cache-Control": caching(r, v)})
        from faces import open_image  # HEIC and camera RAW: developed at full size, the right way up

        try:
            im = open_image(path)
        except Exception:
            raise err(422, "This photo couldn't be opened for editing.", "bad_image")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=97, subsampling=0)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": caching(r, v)})

    # ------------------------------------------------------------------ saving
    @app.post("/api/events/{eid}/photos/{pid}/edits")
    def save_copy(eid: int, pid: str, file: UploadFile = File(...), recipe: str = Form(""), u: dict = Depends(h.can("upload"))):
        """A new edited copy of this photo, in the same event. `recipe`: its steps, when they can be replayed."""
        r = photo(u, eid, pid)
        data, fmt = edited_image(file)
        rec = clean_recipe(recipe, err) if recipe else None
        root = r["edited_of"] if r["edited"] else r["id"]          # a copy of a copy points at the original
        base = r["source_name"] if r["edited"] and r["source_name"] else r["orig_name"]
        name = f"{Path(base).stem.removesuffix(' (edited)')} (edited){SAVE_EXT[fmt]}"
        res = add_photo(u["account_id"], eid, h.plan_of(u["plan"]), name, io.BytesIO(data), edited=True, edited_of=root)
        if res["status"] == "duplicate":
            raise err(409, "This exact picture is already in the event.", "duplicate")
        if res["status"] != "queued":
            raise err(422, res["error"] or "The copy couldn't be saved.", "rejected")
        if rec:
            db.run("UPDATE photos SET edit_recipe=? WHERE public_id=?", (rec, res["id"]))
        h.poke()
        return out(photo(u, eid, res["id"]))

    @app.put("/api/events/{eid}/photos/{pid}/edit")
    def update(eid: int, pid: str, file: UploadFile = File(...), recipe: str = Form(""), u: dict = Depends(h.can("upload"))):
        """Save over an edited copy. Originals are never overwritten: those are saved as a copy first.
        Without a recipe (the edit had painting, layers...) the copy can no longer be re-edited step by step."""
        r = photo(u, eid, pid)
        rec = clean_recipe(recipe, err) if recipe else None
        if not r["edited"]:
            raise err(409, "Original uploads are never changed. Save your edit as a copy first.", "original")
        if r["status"] in ("uploading", "uploaded", "processing"):
            raise err(409, "The last save is still being processed. Try again in a moment.", "busy")
        data, fmt = edited_image(file)
        if fmt != save_format(r["ext"]):   # the copy's file keeps its format
            raise err(422, f"This copy is a {save_format(r['ext']).upper()}: save it in that format.", "wrong_format")
        quota = storage_quota(u["account_id"])
        if photo_usage(u["account_id"])["storage"] - r["size"] + len(data) > quota:
            raise err(422, storage_full(quota), "plan_limit")
        final = get_storage().photo_path(r, "orig")
        part = final.with_name(final.name + ".part")
        part.write_bytes(data)
        try:
            with db.tx():
                db.run("""UPDATE photos SET size=?, sha256=?, status='uploaded', error=NULL, edited_at=?, edit_recipe=? WHERE id=?""",
                       (len(data), hashlib.sha256(data).hexdigest(), int(time.time()), rec, r["id"]))
                os.replace(part, final)  # inside the transaction: the row and the file change together
        except sqlite3.IntegrityError:
            part.unlink(missing_ok=True)
            raise err(409, "This exact picture is already in the event.", "duplicate")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        h.poke()  # new thumbnail and web copy
        return out(photo(u, eid, pid))

    # ------------------------------------------------------------------ presets: saved looks, shared by the team
    def preset_out(r) -> dict:
        return {"id": r["id"], "name": r["name"], "recipe": json.loads(r["recipe"]), "created_by": r["created_by"],
                "created_at": r["created_at"]}

    @app.get("/api/edit-presets")
    def list_presets(u: dict = Depends(h.user_dep)):
        return [preset_out(r) for r in db.q("SELECT * FROM edit_presets WHERE account_id=? ORDER BY name COLLATE NOCASE",
                                            (u["account_id"],))]

    @app.post("/api/edit-presets")
    def save_preset(body: PresetIn, u: dict = Depends(h.can("upload"))):
        name = " ".join(body.name.split())[:60]
        if not name:
            raise err(422, "Give the preset a name.", "name")
        rec = clean_recipe(body.recipe, err)
        if not json.loads(rec)["steps"]:
            raise err(422, "There are no steps to save yet. Make an adjustment first.", "empty")
        old = db.q1("SELECT id FROM edit_presets WHERE account_id=? AND name=?", (u["account_id"], name))
        if old and not body.replace:
            raise err(409, f"There is already a preset called {name}.", "exists")
        if not old and db.q1("SELECT COUNT(*) n FROM edit_presets WHERE account_id=?", (u["account_id"],))["n"] >= 200:
            raise err(422, "You have 200 presets. Delete some first.", "too_many")
        who = u["name"] or u["email"]
        if old:
            db.run("UPDATE edit_presets SET recipe=?, created_by=?, created_at=? WHERE id=?", (rec, who, int(time.time()), old["id"]))
            preset_id = old["id"]
        else:
            preset_id = db.ex("INSERT INTO edit_presets(account_id, name, recipe, created_by, created_at) VALUES(?,?,?,?,?)",
                              (u["account_id"], name, rec, who, int(time.time())))
        return preset_out(db.q1("SELECT * FROM edit_presets WHERE id=?", (preset_id,)))

    @app.post("/api/edit-presets/import")
    def import_presets(files: List[UploadFile] = File(...), u: dict = Depends(h.can("upload"))):
        """Load settings: Camera Raw / Lightroom presets (.xmp, .lrtemplate, or a .zip of them) saved as presets of
        the team. A name already taken gets a number: nothing is overwritten."""
        from . import acr_presets

        got = []
        for f in files[:50]:
            data = f.file.read(acr_presets.MAX_UNZIPPED + 1)
            if len(data) > acr_presets.MAX_UNZIPPED:
                raise err(413, f"{f.filename} is too large.", "too_large")
            got.append((Path(f.filename or "preset").name, data))
        found, bad = acr_presets.read_files(got)
        count = db.q1("SELECT COUNT(*) n FROM edit_presets WHERE account_id=?", (u["account_id"],))["n"]
        who, out_, skipped = u["name"] or u["email"], [], [{"file": n, "reason": r} for n, r in bad]
        for name, settings, fname in found:
            steps, left = acr_presets.convert(settings)
            if not steps:
                skipped.append({"file": fname, "reason": "no settings the editor can use" + (f" (only {', '.join(left)})" if left else "")})
                continue
            if count >= 200:
                skipped.append({"file": fname, "reason": "you have 200 presets; delete some first"})
                continue
            base = " ".join(name.split())[:54] or "Preset"
            final, n = base, 1
            while db.q1("SELECT 1 FROM edit_presets WHERE account_id=? AND name=?", (u["account_id"], final)):
                n += 1
                final = f"{base} ({n})"
            rec = json.dumps({"v": 1, "steps": steps}, separators=(",", ":"))
            pid_ = db.ex("INSERT INTO edit_presets(account_id, name, recipe, created_by, created_at) VALUES(?,?,?,?,?)",
                         (u["account_id"], final, rec, who, int(time.time())))
            count += 1
            out_.append({**preset_out(db.q1("SELECT * FROM edit_presets WHERE id=?", (pid_,))), "left_out": left})
        if not out_ and not skipped:
            raise err(422, "Choose .xmp or .lrtemplate presets, or a .zip of them.", "empty")
        return {"imported": out_, "skipped": skipped}

    @app.delete("/api/edit-presets/{preset_id}")
    def delete_preset(preset_id: int, u: dict = Depends(h.can("upload"))):
        if not db.run("DELETE FROM edit_presets WHERE id=? AND account_id=?", (preset_id, u["account_id"])):
            raise err(404, "Preset not found.", "not_found")
        return {"ok": True}
