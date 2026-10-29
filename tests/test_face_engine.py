"""Face engines: events on the old engine move to the new one on start-up, and search never mixes the two."""
import numpy as np
from starlette.testclient import TestClient

from conftest import Sess, jpeg_bytes, upload, wait_idle
from studio import config, db
from studio.processing import VectorIndex
from studio.server import create_app


def test_old_events_are_reindexed_with_the_new_engine_on_start(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "public_url", "")
    data = tmp_path / "data"
    with TestClient(create_app(data_dir=data, workers=1)) as c:
        r = c.post("/api/auth/signup", headers={"X-PF": "1"}, json={"company": "S", "email": "s@x.test", "password": "longenough1"})
        s = Sess(c, r.cookies.get("pf_session"))
        db.run("UPDATE accounts SET storage_quota=?", (config.GB,))   # allocated by the platform admin
        cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
        old = s.post("/api/events", json={"client_id": cid, "name": "Old"}).json()["id"]
        purged = s.post("/api/events", json={"client_id": cid, "name": "Purged"}).json()["id"]
        upload(s, old, [("a.jpg", jpeg_bytes(seed=81))])
        wait_idle(s, old)
        # make them look like events indexed before this version
        db.run("UPDATE events SET face_model='dlib', tolerance=0.45 WHERE id=?", (old,))
        db.run("UPDATE events SET face_model='dlib', tolerance=0.5, faces_purged=1 WHERE id=?", (purged,))
        pid = db.q1("SELECT id FROM photos WHERE event_id=?", (old,))["id"]
        db.ex("INSERT INTO faces(event_id, photo_id, vec, model) VALUES(?,?,?,'dlib')", (old, pid, np.zeros(128, np.float32).tobytes()))
        token = s.token

    with TestClient(create_app(data_dir=data, workers=1)) as c2:  # the upgraded server starts
        s2 = Sess(c2, token)
        e = wait_idle(s2, old)
        assert e["face_model"] == "sface" and e["tolerance"] == 0.92        # "strict" stays strict in the new scale
        assert e["ready"] == 1 and e["failed"] == 0
        assert db.q1("SELECT COUNT(*) n FROM faces WHERE event_id=? AND model='dlib'", (old,))["n"] == 0
        p = s2.get(f"/api/events/{purged}").json()
        assert p["face_model"] == "dlib" and p["faces_purged"] is True      # deleted face data is not rebuilt
        # "Rebuild face index" moves a purged event to the new engine when the studio asks for it
        assert s2.post(f"/api/events/{purged}/reindex").json()["face_model"] == "sface"


def test_search_only_compares_vectors_of_the_events_engine(studio):
    s, _, ev = studio
    upload(s, ev["id"], [("a.jpg", jpeg_bytes(seed=91)), ("b.jpg", jpeg_bytes(seed=92))])
    wait_idle(s, ev["id"])
    a, b = [r["id"] for r in db.q("SELECT id FROM photos WHERE event_id=? ORDER BY id", (ev["id"],))]
    v = (np.ones(128) / np.sqrt(128)).astype(np.float32)
    db.ex("INSERT INTO faces(event_id, photo_id, vec, model) VALUES(?,?,?,'sface')", (ev["id"], a, v.tobytes()))
    db.ex("INSERT INTO faces(event_id, photo_id, vec, model) VALUES(?,?,?,'dlib')", (ev["id"], b, v.tobytes()))
    idx = VectorIndex()
    assert [p for p, _ in idx.search(ev["id"], "sface", v, 0.95)] == [a]
    assert [p for p, _ in idx.search(ev["id"], "dlib", v, 0.5)] == [b]


def _at(q, u, dist):
    """Unit vector `dist` away from unit vector q, turned towards direction u."""
    t = 2 * np.arcsin(dist / 2)
    return (np.cos(t) * q + np.sin(t) * u).astype(np.float32)


def test_grey_zone_faces_need_a_same_camera_match_to_a_sure_one():
    from studio.processing import confirmed

    e = np.eye(128, dtype=np.float32)
    q = e[0]                                    # the selfie
    sure = _at(q, e[1], 0.50)                   # clearly the guest
    lookalike_of_sure = _at(q, e[1], 0.94)      # grey zone, but very like the sure face: same person, other light
    stranger = _at(q, e[2], 0.94)               # grey zone, unlike any sure face: a look-alike
    too_far = _at(q, e[1], 0.99)
    vecs = np.stack([sure, lookalike_of_sure, stranger, too_far])
    d = np.linalg.norm(vecs - q, axis=1)
    assert sorted(confirmed(vecs, d, 0.95).tolist()) == [0, 1]

    # nobody is a sure match: only the safer lower part of the grey zone counts
    safer = _at(q, e[3], 0.88)
    vecs = np.stack([stranger, safer])
    assert confirmed(vecs, np.linalg.norm(vecs - q, axis=1), 0.95).tolist() == [1]
