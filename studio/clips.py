"""Video clips: in the client gallery, the client marks a start and an end on one of the event's videos and downloads
just that part.

The clip is cut on the server by ffmpeg (the copy bundled with the imageio-ffmpeg package, or one on the PATH) and
streamed straight into the client's download as it is made: it is never written to the server's disk, so there is
nothing to store or clean up. It is re-encoded to H.264 + AAC MP4, so the cut is exact to the frame and the clip plays
on every phone, even when the original is an iPhone MOV.

Encoding is the expensive part, so clips are kept short (MAX_SECONDS), at most BUSY_MAX are made at a time, and each
visitor can make a limited number per hour. A clip is a download, so it follows the gallery's "client can download"
switch, and the gallery's link and PIN like everything else there.
"""
import re
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi.responses import StreamingResponse

from . import db
from .videos import video_path

MAX_SECONDS = 180       # the longest clip
MIN_SECONDS = 0.5
BUSY_MAX = 2            # clips being made at the same time, server-wide
busy = threading.BoundedSemaphore(BUSY_MAX)


@lru_cache(maxsize=1)
def ffmpeg() -> str:
    """The ffmpeg program, or '' when there is none (the clip button is then not offered)."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or ""


def available() -> bool:
    return bool(ffmpeg())


def stamp(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}m{s:02d}s"


def command(src: Path, start: float, length: float) -> list:
    return [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",       # seek, then decode exactly from there
            "-map", "0:v:0", "-map", "0:a:0?",                                    # the picture, and the sound if any
            "-vf", "scale='if(gte(iw,ih),min(1920,iw),-2)':'if(gte(iw,ih),-2,min(1920,ih))'",  # at most 1080p
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",          # playable MP4 written in one pass
            "-f", "mp4", "pipe:1"]


def mount_clips(app, h):
    """h: helpers from server.create_app (err, client_open, client_ip, limiter)."""
    err = h.err

    @app.get("/c/{token}/video/{vid}/clip")
    def clip(token: str, vid: str, request: Request, start: float, end: float):
        e = h.client_open(token, request)
        if not e["client_download"]:
            raise err(403, "Downloads are turned off for this gallery.", "no_download")
        if not available():
            raise err(503, "Clips aren't set up on this server.", "not_installed")
        r = db.q1("SELECT * FROM videos WHERE public_id=? AND event_id=?", (vid, e["id"]))
        src = video_path(r) if r else None
        if not src or not src.exists():
            raise err(404, "Video not found.", "not_found")
        if not (start >= 0 and end > start):
            raise err(422, "Choose an end after the start.", "range")
        if end - start < MIN_SECONDS:
            raise err(422, "Make the clip at least half a second long.", "range")
        if end - start > MAX_SECONDS + 0.05:
            raise err(422, f"A clip can be up to {MAX_SECONDS // 60} minutes long.", "too_long")
        ip = h.client_ip(request)
        if not h.limiter.allow(("clip", ip, token), 20, 3600):
            raise err(429, "That's a lot of clips. Please wait a while and try again.", "rate")
        if not busy.acquire(blocking=False):
            raise err(429, "Other clips are being made right now. Try again in a minute.", "busy")
        try:
            proc = subprocess.Popen(command(src, start, end - start), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            busy.release()
            raise err(500, "The clip couldn't be made. Please try again.", "failed")

        def stream():
            try:
                while True:
                    chunk = proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:  # done, failed, or the client went away: stop ffmpeg, free the slot
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                busy.release()

        base = re.sub(r"[^\w\- ]+", "", Path(r["orig_name"]).stem).strip() or "video"
        name = f"{base} clip {stamp(start)}-{stamp(end)}.mp4"
        return StreamingResponse(stream(), media_type="video/mp4",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
