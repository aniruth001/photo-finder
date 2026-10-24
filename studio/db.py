"""SQLite storage. One connection per thread; WAL mode so the web server and the photo worker can both write."""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL,
    brand_color TEXT NOT NULL DEFAULT '#f0c52f',
    plan TEXT NOT NULL DEFAULT 'free',
    created_at INTEGER NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    created_at INTEGER NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE clients (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX clients_account ON clients(account_id);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    event_date TEXT,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft',
    allow_download INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    tolerance REAL NOT NULL DEFAULT 0.5,
    faces_purged INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX events_account ON events(account_id, client_id);
CREATE TABLE photos (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    orig_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    status TEXT NOT NULL DEFAULT 'uploading',
    error TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX photos_event ON photos(event_id, status);
CREATE INDEX photos_account ON photos(account_id);
CREATE UNIQUE INDEX photos_dedup ON photos(event_id, sha256);
CREATE TABLE faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    vec BLOB NOT NULL
);
CREATE INDEX faces_event ON faces(event_id);
CREATE TABLE guest_searches (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    matches INTEGER NOT NULL
);
CREATE INDEX searches_event ON guest_searches(event_id);
"""

# version -> SQL, applied once to databases older than that version
MIGRATIONS = {
    2: "ALTER TABLE accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT ''",  # IANA name; '' = the server's own
    3: """
        ALTER TABLE users ADD COLUMN platform_admin INTEGER NOT NULL DEFAULT 0;  -- the product owner (python -m studio grant-admin)
        ALTER TABLE users ADD COLUMN last_seen_at INTEGER;
        ALTER TABLE accounts ADD COLUMN disabled_at INTEGER;                     -- set = suspended by the product owner
        ALTER TABLE accounts ADD COLUMN disabled_reason TEXT NOT NULL DEFAULT '';
        CREATE TABLE admin_log (                                                 -- kept when the account is deleted
            id INTEGER PRIMARY KEY,
            admin_user_id INTEGER,
            admin_email TEXT NOT NULL,
            account_id INTEGER,
            company TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX admin_log_account ON admin_log(account_id, id);
    """,
    4: """
        ALTER TABLE users ADD COLUMN google_sub TEXT;  -- Google account id ("sub"), set once linked; pw_hash '' = no password
        CREATE UNIQUE INDEX users_google ON users(google_sub) WHERE google_sub IS NOT NULL;
    """,
    5: """
        CREATE TABLE password_resets (                 -- only the token's hash is stored, like sessions
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX password_resets_user ON password_resets(user_id);
    """,
    6: """
        ALTER TABLE faces ADD COLUMN model TEXT NOT NULL DEFAULT 'dlib';        -- which engine made the vector
        ALTER TABLE events ADD COLUMN face_model TEXT NOT NULL DEFAULT 'dlib';  -- which engine the event searches with
        CREATE INDEX faces_event_model ON faces(event_id, model);
    """,
    7: """
        CREATE TABLE invites (                          -- team invitations; only the link token's hash is kept
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            email TEXT NOT NULL COLLATE NOCASE,
            name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            accepted_at INTEGER
        );
        CREATE UNIQUE INDEX invites_pending ON invites(account_id, email) WHERE accepted_at IS NULL;
    """,
    8: """
        ALTER TABLE events ADD COLUMN client_enabled INTEGER NOT NULL DEFAULT 0;  -- the all-photos link for the client
        ALTER TABLE events ADD COLUMN client_token TEXT;                          -- the secret in /c/<token>
        ALTER TABLE events ADD COLUMN client_pin TEXT NOT NULL DEFAULT '';        -- scrypt hash of the optional PIN
        ALTER TABLE events ADD COLUMN client_download INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE events ADD COLUMN client_views INTEGER NOT NULL DEFAULT 0;
        CREATE UNIQUE INDEX events_client_token ON events(client_token) WHERE client_token IS NOT NULL;
    """,
    9: """
        ALTER TABLE accounts ADD COLUMN logo TEXT NOT NULL DEFAULT '';            -- random file name; '' = show the name
        ALTER TABLE accounts ADD COLUMN logo_size TEXT NOT NULL DEFAULT 'medium'; -- small | medium | large
        ALTER TABLE accounts ADD COLUMN logo_badge INTEGER NOT NULL DEFAULT 0;   -- too dark for the dark pages: light badge
    """,
    10: """
        ALTER TABLE accounts ADD COLUMN theme TEXT NOT NULL DEFAULT 'dark';        -- guest/client pages: dark | light | auto
        ALTER TABLE accounts ADD COLUMN logo_badge_light INTEGER NOT NULL DEFAULT 0; -- too light for the light pages
    """,
    11: """
        CREATE TABLE camera_logins (                    -- one FTP login per event, for cameras (studio/camera.py)
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
            username TEXT NOT NULL UNIQUE,
            pw_hash TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            last_login_at INTEGER,
            last_upload_at INTEGER,
            uploads INTEGER NOT NULL DEFAULT 0,
            last_note TEXT NOT NULL DEFAULT ''
        );
    """,
    12: """
        ALTER TABLE events ADD COLUMN ftp_allowed INTEGER NOT NULL DEFAULT 0;  -- camera upload, switched on by the platform admin
        UPDATE events SET ftp_allowed=1 WHERE id IN (SELECT event_id FROM camera_logins);  -- logins made before this keep working
    """,
    13: """
        CREATE TABLE estimates (                        -- quotations for clients, with a PDF (studio/estimates.py)
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            number INTEGER NOT NULL,                    -- per company: EST-0001, EST-0002, ...
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            company_name TEXT NOT NULL,                 -- printed on it; defaults to the company's name
            company_details TEXT NOT NULL DEFAULT '',   -- address, phone, email, GSTIN
            show_logo INTEGER NOT NULL DEFAULT 1,
            client_name TEXT NOT NULL,
            client_details TEXT NOT NULL DEFAULT '',
            event_title TEXT NOT NULL DEFAULT '',
            estimate_date TEXT NOT NULL,
            valid_until TEXT,
            currency TEXT NOT NULL DEFAULT '₹',
            items TEXT NOT NULL DEFAULT '[]',           -- JSON [{name, description, amount}], amounts in paise
            discount INTEGER NOT NULL DEFAULT 0,        -- paise
            tax_label TEXT NOT NULL DEFAULT 'GST',
            tax_pct REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(account_id, number)
        );
    """,
    14: """
        CREATE TABLE estimates_new (                    -- Billing: estimates and invoices, each numbered on its own
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'estimate',      -- estimate (EST-0001) | invoice (INV-0001)
            number INTEGER NOT NULL,
            ref TEXT NOT NULL DEFAULT '',               -- an invoice made from an estimate: that estimate's number
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            company_name TEXT NOT NULL,
            company_details TEXT NOT NULL DEFAULT '',
            show_logo INTEGER NOT NULL DEFAULT 1,
            client_name TEXT NOT NULL,
            client_details TEXT NOT NULL DEFAULT '',
            event_title TEXT NOT NULL DEFAULT '',
            estimate_date TEXT NOT NULL,                -- the document's date (estimate date or invoice date)
            valid_until TEXT,                           -- estimate: valid until; invoice: due date
            currency TEXT NOT NULL DEFAULT '₹',
            items TEXT NOT NULL DEFAULT '[]',
            discount INTEGER NOT NULL DEFAULT 0,
            tax_label TEXT NOT NULL DEFAULT 'GST',
            tax_pct REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(account_id, kind, number)
        );
        INSERT INTO estimates_new(id, account_id, number, client_id, company_name, company_details, show_logo, client_name, client_details, event_title, estimate_date, valid_until, currency, items, discount, tax_label, tax_pct, notes, created_by, created_at, updated_at) SELECT id, account_id, number, client_id, company_name, company_details, show_logo, client_name, client_details, event_title, estimate_date, valid_until, currency, items, discount, tax_label, tax_pct, notes, created_by, created_at, updated_at FROM estimates;
        DROP TABLE estimates;
        ALTER TABLE estimates_new RENAME TO estimates;
    """,
    15: """
        CREATE TABLE doc_numbers (                      -- the last number handed out per company and kind, so deleting
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,  -- the newest one can't free it
            kind TEXT NOT NULL,
            last INTEGER NOT NULL,
            PRIMARY KEY(account_id, kind)
        );
        INSERT INTO doc_numbers(account_id, kind, last) SELECT account_id, kind, MAX(number) FROM estimates GROUP BY account_id, kind;
    """,
    16: """
        CREATE TABLE videos (                           -- event videos: shown in the client gallery only (studio/videos.py)
            id INTEGER PRIMARY KEY,
            public_id TEXT NOT NULL UNIQUE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            orig_name TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(event_id, sha256)
        );
        CREATE INDEX videos_account ON videos(account_id);
    """,
    17: """
        CREATE TABLE inventory (                        -- the studio's equipment (studio/inventory.py)
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Other',
            brand TEXT NOT NULL DEFAULT '',
            serial TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1,
            purchase_date TEXT,
            purchase_cost INTEGER NOT NULL DEFAULT 0,   -- paise
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'available',   -- available | out | maintenance | retired
            holder_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            checked_out_at INTEGER,
            due_back TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX inventory_account ON inventory(account_id, status);
        CREATE TABLE inventory_log (                    -- names are copied in, so the history outlives people and events
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            action TEXT NOT NULL,                       -- added | edited | checked_out | returned | maintenance | available | retired
            actor TEXT NOT NULL DEFAULT '',
            person TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX inventory_log_item ON inventory_log(item_id);
    """,
    18: """
        CREATE TABLE bookings (                         -- the calendar (studio/bookings.py)
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            date TEXT NOT NULL,                         -- first day, YYYY-MM-DD
            end_date TEXT,                              -- last day of a multi-day booking
            start_time TEXT, end_time TEXT,             -- HH:MM
            source TEXT NOT NULL DEFAULT 'own',         -- own (own client) | expost
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            client_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            amount INTEGER NOT NULL DEFAULT 0,          -- finalised, paise
            paid INTEGER NOT NULL DEFAULT 0,            -- received so far, paise
            payment_note TEXT NOT NULL DEFAULT '',
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX bookings_account_date ON bookings(account_id, date);
    """,
    19: """
        CREATE TABLE album_picks (                      -- photos the client chose for the album (studio/album.py)
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            picked_at INTEGER NOT NULL,
            PRIMARY KEY (event_id, photo_id)
        );
        CREATE INDEX album_picks_photo ON album_picks(photo_id);
        ALTER TABLE events ADD COLUMN album_note TEXT NOT NULL DEFAULT '';
        ALTER TABLE events ADD COLUMN album_sent_at INTEGER;
        ALTER TABLE events ADD COLUMN album_locked INTEGER NOT NULL DEFAULT 0;
    """,
    20: """
        CREATE TABLE vendor_links (                     -- upload links for vendors: second shooters, drone, video (studio/vendors.py)
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            last_upload_at INTEGER
        );
        CREATE INDEX vendor_links_event ON vendor_links(event_id);
        ALTER TABLE photos ADD COLUMN vendor_id INTEGER REFERENCES vendor_links(id) ON DELETE SET NULL;
        ALTER TABLE videos ADD COLUMN vendor_id INTEGER REFERENCES vendor_links(id) ON DELETE SET NULL;
        CREATE INDEX photos_vendor ON photos(vendor_id);
    """,
    21: """
        CREATE TABLE tasks (                            -- the project board (studio/tasks.py)
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT 'new',          -- new | planned | progress | feedback | done | cancelled
            position REAL NOT NULL DEFAULT 0,           -- order inside the stage's column
            status TEXT NOT NULL DEFAULT 'on_track',    -- on_track | ready | changes | blocked
            starred INTEGER NOT NULL DEFAULT 0,
            deadline TEXT,                              -- YYYY-MM-DD
            tags TEXT NOT NULL DEFAULT '[]',            -- JSON ["Post-prod", "Onsite"]
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
            event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            checklist TEXT NOT NULL DEFAULT '[]',       -- JSON [{text, done}]
            vendor_name TEXT NOT NULL DEFAULT '',       -- '' = done in-house
            vendor_phone TEXT NOT NULL DEFAULT '',
            vendor_work TEXT NOT NULL DEFAULT '',       -- the work given to the vendor
            vendor_amount INTEGER NOT NULL DEFAULT 0,   -- agreed, paise
            vendor_paid INTEGER NOT NULL DEFAULT 0,     -- paid so far, paise
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX tasks_account ON tasks(account_id, stage, position);
        CREATE TABLE task_assignees (
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (task_id, user_id)
        );
        CREATE INDEX task_assignees_user ON task_assignees(user_id);
        CREATE TABLE task_log (                         -- comments and what happened; names copied in, like inventory_log
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,                         -- comment | created | moved | status | vendor | vendor_off | edited
            actor TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX task_log_task ON task_log(task_id);
    """,
    22: "ALTER TABLE events ADD COLUMN guest_pin TEXT NOT NULL DEFAULT ''",  # scrypt hash of the guests' optional PIN
    23: """
        CREATE TABLE photo_scenes (                     -- search by description: one CLIP vector per photo (studio/scenes.py)
            photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            vec BLOB NOT NULL,                          -- 512 float32; empty = the thumbnail couldn't be read
            model TEXT NOT NULL
        );
        CREATE INDEX photo_scenes_event ON photo_scenes(event_id, model);
        ALTER TABLE accounts ADD COLUMN scene_search INTEGER NOT NULL DEFAULT 1;  -- the studio's on/off switch
    """,
    24: """
        ALTER TABLE album_picks ADD COLUMN picked_by TEXT NOT NULL DEFAULT 'client';  -- client | studio | both
        ALTER TABLE album_picks ADD COLUMN picker TEXT NOT NULL DEFAULT '';           -- the team member who added it (studio)
    """,
    25: """
        ALTER TABLE photos ADD COLUMN edited INTEGER NOT NULL DEFAULT 0;  -- 1 = made in the editor: shown everywhere with an Edited tag
        ALTER TABLE photos ADD COLUMN edited_of INTEGER REFERENCES photos(id) ON DELETE SET NULL;  -- the photo it was edited from
        ALTER TABLE photos ADD COLUMN edited_at INTEGER;                  -- last saved from the editor (also busts cached thumbnails)
    """,
    26: "UPDATE photos SET status='uploaded' WHERE edited=1 AND status='ready'",  # edited copies now get face data: redo them
    27: """
        ALTER TABLE photos ADD COLUMN blur REAL;          -- culling (studio/culling.py): 0 crisp .. 1 soft; -1 = couldn't tell; NULL = not yet
        ALTER TABLE photos ADD COLUMN dhash INTEGER;      -- 64-bit picture fingerprint: near-duplicates
        ALTER TABLE photos ADD COLUMN eyes_shut INTEGER;  -- main faces with eyes shut (NULL: not checked)
        ALTER TABLE photos ADD COLUMN edit_recipe TEXT;   -- edited copies: the editor's steps (JSON), so they can be re-edited
        CREATE INDEX photos_unculled ON photos(status) WHERE blur IS NULL;
        CREATE TABLE edit_presets (                       -- the editor's saved looks, shared by the team
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            recipe TEXT NOT NULL,                         -- JSON: the steps (studio/static/editor.html, RECIPE_OPS)
            created_by TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            UNIQUE(account_id, name)
        );
        CREATE TABLE ai_jobs (                            -- slow AI work done in the background (upscaling)
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            photo_id INTEGER REFERENCES photos(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,                           -- upscale2 | upscale4
            status TEXT NOT NULL DEFAULT 'queued',        -- queued | running | done | failed
            progress REAL NOT NULL DEFAULT 0,
            result_photo_id INTEGER REFERENCES photos(id) ON DELETE SET NULL,
            error TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        CREATE INDEX ai_jobs_open ON ai_jobs(status, id);
    """,
}

_local = threading.local()


def db_path():
    return config.settings.data_dir / "studio.db"


def conn() -> sqlite3.Connection:
    p = str(db_path())
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "p", None) != p:
        c = sqlite3.connect(p, timeout=15, isolation_level=None)  # autocommit; use tx() for multi-step writes
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        _local.c, _local.p = c, p
    return c


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn().execute(sql, args).fetchone()


def ex(sql, args=()) -> int:
    """Run a write; returns the new row id."""
    return conn().execute(sql, args).lastrowid


def run(sql, args=()) -> int:
    """Run a write; returns the number of rows changed."""
    return conn().execute(sql, args).rowcount


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def init():
    config.settings.data_dir.mkdir(parents=True, exist_ok=True)
    c = conn()
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v == 0:
        c.executescript(SCHEMA)
        c.execute("PRAGMA user_version=1")
        v = 1
    for n in sorted(MIGRATIONS):  # existing databases are brought up to date in order
        if v < n:
            c.executescript(f"BEGIN; {MIGRATIONS[n]}; PRAGMA user_version={n}; COMMIT;")
            v = n
    # a crash can leave photos mid-upload: those files are incomplete, so mark them failed
    run("UPDATE photos SET status='failed', error='Upload was interrupted. Upload it again.' WHERE status='uploading'")
