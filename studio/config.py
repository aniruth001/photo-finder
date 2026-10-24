"""Settings and plan limits.

The numbers in PLANS are placeholders: set the tiers and prices you actually want to sell.
None means unlimited.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GB = 1024 ** 3
MB = 1024 ** 2

PLANS = {
    "free": {"label": "Free", "events": 2, "photos": 500, "storage": 1 * GB, "users": 2, "price": 0},
    "pro": {"label": "Pro", "events": None, "photos": 50_000, "storage": 100 * GB, "users": 10, "price": 1999},
    "studio": {"label": "Studio", "events": None, "photos": 250_000, "storage": 500 * GB, "users": 50, "price": 4999},
}
CURRENCY = "INR"  # "price" is per month in this currency; used only for the admin revenue estimate
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq"}
try:  # camera RAW files are accepted when rawpy (LibRaw) is installed: pip install rawpy
    import rawpy  # noqa: F401
    ALLOWED_EXTS |= RAW_EXTS
except Exception:
    pass
DEFAULT_BRAND = "#f0c52f"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("PF_DATA", ROOT / "data")))
    public_url: str = os.environ.get("PF_PUBLIC_URL", "").rstrip("/")  # e.g. https://photos.example.com
    max_file_mb: int = int(os.environ.get("PF_MAX_FILE_MB", "100"))  # per photo: camera RAW files from 45-megapixel cameras run to 60-80 MB
    max_video_mb: int = int(os.environ.get("PF_MAX_VIDEO_MB", "4096"))  # per video (studio/videos.py)
    max_files_per_request: int = 20
    session_days: int = 14
    allow_signup: bool = os.environ.get("PF_ALLOW_SIGNUP", "1") != "0"
    # X-Forwarded-For: "auto" (default) reads it only from a proxy on this machine or a private network (nginx,
    # Caddy, Docker), "1" always, "0" never (Studio open to the internet directly, on a private network)
    trust_proxy: str = os.environ.get("PF_TRUST_PROXY", "auto").strip().lower()
    max_selfie_mb: int = 15
    detect_side: int = 2000
    face_model: str = os.environ.get("PF_FACE_MODEL", "sface")  # sface (accurate, default) | dlib (the old engine)
    detector: str = "hog"  # dlib only: hog | cnn
    workers: int = 0
    # Google sign-in (OAuth client of type "Web application" in Google Cloud). Both set = the button appears.
    google_client_id: str = os.environ.get("PF_GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("PF_GOOGLE_CLIENT_SECRET", "")
    # Outgoing email (password resets). No host = emails are printed to the console instead (local testing).
    smtp_host: str = os.environ.get("PF_SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("PF_SMTP_PORT", "587"))
    smtp_user: str = os.environ.get("PF_SMTP_USER", "")
    smtp_password: str = os.environ.get("PF_SMTP_PASSWORD", "")
    smtp_security: str = os.environ.get("PF_SMTP_SECURITY", "starttls")  # starttls (port 587) | ssl (465) | none
    mail_from: str = os.environ.get("PF_MAIL_FROM", "")  # e.g. "Photo Finder <no-reply@yourdomain.com>"
    reset_minutes: int = 60
    # Camera upload over FTP/FTPS (studio/camera.py). Port 0 = off.
    ftp_port: int = int(os.environ.get("PF_FTP_PORT", "0"))
    ftp_bind: str = os.environ.get("PF_FTP_BIND", "0.0.0.0")
    ftp_host: str = os.environ.get("PF_FTP_HOST", "")          # address cameras use; default: the public URL's host
    ftp_passive: str = os.environ.get("PF_FTP_PASSIVE", "")    # e.g. 60000-60099 (open these in the firewall too)
    ftp_cert: str = os.environ.get("PF_FTP_CERT", "")          # PEM certificate: enables FTPS
    ftp_key: str = os.environ.get("PF_FTP_KEY", "")
    ftp_require_tls: bool = os.environ.get("PF_FTP_REQUIRE_TLS", "") == "1"
    # A venue's cameras usually share one internet address (its Wi-Fi), and each camera uses up to 2 connections.
    ftp_max_per_ip: int = int(os.environ.get("PF_FTP_MAX_PER_IP", "40"))         # about 20 cameras on one Wi-Fi
    ftp_max_connections: int = int(os.environ.get("PF_FTP_MAX_CONNECTIONS", "400"))


settings = Settings()
