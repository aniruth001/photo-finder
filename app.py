"""Local photo finder: point it at a folder, guests scan a QR code, take a selfie, get their photos.

    python app.py /path/to/photos --title "Wedding Photos"
"""
import argparse
import io
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from faces import DEFAULT_MODEL, PRESETS, make_engine, open_image
from indexer import Index
from netutil import all_ips, ensure_cert, lan_ip

HERE = Path(__file__).parent
MAX_SELFIE_BYTES = 20 * 1024 * 1024


def _adb(*args, timeout=15):
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)


def setup_usb(port: int):
    """Android over a USB cable: `adb reverse` makes the phone's localhost:PORT reach this computer."""
    if not shutil.which("adb"):
        raise SystemExit(
            "adb not found. Install Android platform-tools "
            "(https://developer.android.com/tools/releases/platform-tools), add it to PATH, and run again.")
    rows = [ln.split("\t") for ln in _adb("devices").stdout.splitlines()[1:] if "\t" in ln]
    ready = [serial for serial, state in rows if state.strip() == "device"]
    if not ready:
        if any(state.strip() == "unauthorized" for _, state in rows):
            raise SystemExit("Phone found but not authorised. Unlock it and tap Allow on 'Allow USB debugging?'.")
        raise SystemExit("No phone found. Plug in the cable, turn on Settings > Developer options > USB debugging, "
                         "and set the USB mode to File transfer.")
    if len(ready) > 1 and not os.environ.get("ANDROID_SERIAL"):
        raise SystemExit(f"More than one device is connected ({', '.join(ready)}). "
                         "Unplug the others or set the ANDROID_SERIAL environment variable.")
    r = _adb("reverse", f"tcp:{port}", f"tcp:{port}")
    if r.returncode != 0:
        raise SystemExit(f"adb reverse failed: {r.stderr.strip() or r.stdout.strip()}")

    def keep_alive():  # re-create the tunnel if the cable is unplugged and plugged back in
        while True:
            time.sleep(5)
            try:
                if f"tcp:{port}" not in _adb("reverse", "--list", timeout=10).stdout:
                    _adb("reverse", f"tcp:{port}", f"tcp:{port}")
            except Exception:
                pass

    threading.Thread(target=keep_alive, daemon=True).start()


def open_on_phone(url: str):
    try:
        _adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
    except Exception:
        pass


def create_app(index: Index, engine, title: str, subtitle: str, tolerance: float) -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()  # one selfie is processed at a time; dlib and OpenCV nets are not thread-safe
    from studio.security import RateLimiter  # stdlib only
    limiter = RateLimiter()

    def limit(request: Request, what: str, n: int, window: int):
        ip = request.client.host if request.client else "?"
        if not limiter.allow((what, ip), n, window):
            raise HTTPException(429, "Too many tries. Please wait a few minutes.")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(18)
        resp = await call_next(request)
        for k, v in (("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"), ("X-Frame-Options", "DENY"),
                     ("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{request.state.nonce}'; style-src 'self' 'unsafe-inline'; "
                                                 "img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")):
            resp.headers.setdefault(k, v)
        return resp
    page = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    page = page.replace("__TITLE__", title).replace("__SUBTITLE__", subtitle)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):  # the page's script carries this response's nonce (see the policy below)
        return page.replace("<script>", f'<script nonce="{request.state.nonce}">')

    @app.post("/api/search")
    def search(request: Request, file: UploadFile = File(...)):
        limit(request, "search", 30, 600)
        data = file.file.read(MAX_SELFIE_BYTES + 1)
        if len(data) > MAX_SELFIE_BYTES:
            raise HTTPException(413, "That image is too large.")
        try:
            im = open_image(io.BytesIO(data))  # kept in memory only, never written to disk
        except Exception:
            raise HTTPException(400, "We couldn't read that image.")
        with lock:
            found = engine.faces(im, detect_side=1400, min_face=60, jitters=5)
        if not found:
            return JSONResponse({"error": "no_face"}, status_code=422)
        found.sort(key=lambda f: f[0], reverse=True)  # the biggest face is the person holding the phone
        matches = index.search(found[0][1], tolerance)
        return {
            "count": len(matches),
            "multiple_faces": len(found) > 1,
            "photos": [{"id": pid, "name": index.photos[pid]["name"], "distance": round(d, 3)} for pid, d in matches],
        }

    def known(pid: str) -> dict:
        e = index.photos.get(pid)
        if not e:
            raise HTTPException(404)
        return e

    @app.get("/thumb/{pid}.jpg")
    def thumb(pid: str):
        known(pid)
        return FileResponse(index.cache_dir / "thumb" / f"{pid}.jpg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/web/{pid}.jpg")
    def web(pid: str):
        known(pid)
        return FileResponse(index.cache_dir / "web" / f"{pid}.jpg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/download/{pid}")
    def download(pid: str):
        e = known(pid)  # only ids from the index are served, so no path traversal is possible
        return FileResponse(e["path"], filename=e["name"], content_disposition_type="attachment")

    @app.post("/api/zip")
    def zip_all(request: Request, ids: str = Form(...)):
        limit(request, "zip", 10, 600)  # each zip copies up to 1000 photos to a temporary file
        pids = [p for p in ids.split(",") if p in index.photos][:1000]
        if not pids:
            raise HTTPException(400, "No photos selected.")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        used = set()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:  # JPEGs don't compress further
            for pid in pids:
                name = index.photos[pid]["name"]
                if name in used:
                    name = f"{pid}_{name}"
                used.add(name)
                z.write(index.photos[pid]["path"], name)
        tmp.close()
        fname = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower() or "photos"
        return FileResponse(tmp.name, filename=f"{fname}-my-photos.zip", media_type="application/zip",
                            background=BackgroundTask(os.unlink, tmp.name))

    return app


def main():
    ap = argparse.ArgumentParser(description="Face-search gallery for a local folder of photos.")
    ap.add_argument("folder", type=Path, help="folder with the event photos (subfolders are included)")
    ap.add_argument("--title", default="Event Photos")
    ap.add_argument("--subtitle", default="Take a selfie and we'll find every photo you're in.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from phones on the same Wi-Fi")
    ap.add_argument("--usb", action="store_true",
                    help="serve to an Android phone over the USB cable (adb reverse); no Wi-Fi needed")
    ap.add_argument("--ip", help="address to show guests (default: auto-detected). Use if the QR code shows the wrong one")
    ap.add_argument("--https", action="store_true", help="serve over https with a self-signed certificate")
    ap.add_argument("--model", choices=["sface", "dlib"], default=DEFAULT_MODEL,
                    help="face engine: sface (accurate, default) or dlib (the original one)")
    ap.add_argument("--tolerance", type=float, default=None,
                    help="max face distance to count as a match; lower = stricter (default: 0.95 sface, 0.5 dlib)")
    ap.add_argument("--detector", choices=["hog", "cnn"], default="hog", help="dlib only: cnn is more accurate, much slower")
    ap.add_argument("--detect-size", type=int, default=2000,
                    help="long side (px) photos are shrunk to before detection; higher finds smaller faces")
    ap.add_argument("--workers", type=int, default=0, help="indexing processes (default: CPU cores - 1)")
    ap.add_argument("--reindex", action="store_true", help="ignore the cache and process every photo again")
    ap.add_argument("--cache", type=Path, default=HERE / "cache", help="where thumbnails and the index are stored")
    args = ap.parse_args()

    if not args.folder.is_dir():
        ap.error(f"{args.folder} is not a folder")
    if args.usb:
        if args.https:
            ap.error("--https is not needed with --usb: localhost is already treated as secure by the phone browser")
        args.host = "127.0.0.1"  # only reachable through the cable tunnel, not from the network
        setup_usb(args.port)  # fail early, before a long indexing run, if the phone isn't ready

    if args.tolerance is None:
        args.tolerance = PRESETS[args.model]["balanced"]
    index = Index(args.folder, args.cache, args.detect_size, args.detector, args.model)
    index.build(args.workers, args.reindex)
    if not index.photos:
        ap.error("no .jpg/.png/.webp/.heic photos found in that folder")

    app = create_app(index, make_engine(args.model, args.detector), args.title, args.subtitle, args.tolerance)

    scheme = "https" if args.https else "http"
    ip = args.ip or lan_ip()
    url = f"{scheme}://{ip}:{args.port}"
    ssl = {}
    if args.https:
        crt, key = ensure_cert(args.cache, ip)
        ssl = {"ssl_certfile": str(crt), "ssl_keyfile": str(key)}

    if args.usb:
        phone_url = f"http://localhost:{args.port}"
        print(f"\nPhone (USB cable): {phone_url}   <- opening it on the phone now")
        print("Keep the cable plugged in and the phone unlocked. Press Ctrl+C here to stop.\n")
        threading.Timer(1.5, open_on_phone, args=(phone_url,)).start()
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return

    if ip == "127.0.0.1":
        print("\n! No network found. For an iPhone: plug it in, turn on Settings > Personal Hotspot, "
              "then run again (or pass --ip <this computer's address>).")
    elif ip.startswith("172.20.10."):
        print("\nDetected an iPhone Personal Hotspot network (USB cable or Wi-Fi). Open the address below in Safari.")
    print(f"\nGuests open:  {url}")
    others = [i for i in all_ips() if i != ip]
    if others:
        print(f"(other addresses of this computer, e.g. USB tethering: {', '.join(others)})")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
        png = args.cache / "qr.png"
        args.cache.mkdir(parents=True, exist_ok=True)
        qrcode.make(url).save(png)
        print(f"QR code image for printing: {png}\n")
    except ImportError:
        pass
    if args.https:
        print("First visit on a phone shows a certificate warning (self-signed): tap Advanced > Proceed.\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", **ssl)


if __name__ == "__main__":
    main()
