"""The landing-page story (studio/story.py): drafted and published by the studio, shown to guests after the PIN and to
the client above the photos, and never a way into photos it doesn't name."""
from conftest import jpeg_bytes, upload, wait_idle


def setup(s, ev, n=5):
    upload(s, ev["id"], [(f"IMG_{i:04d}.JPG", jpeg_bytes(seed=i + 1)) for i in range(n)])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    ids = {p["name"]: p["id"] for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    return f"/e/{e['slug']}", e["client"]["url"].split("testserver", 1)[1], ids


def story(ids):
    return {"title": "Srini & Megha", "intro": "Thank you for coming.", "cover": ids["IMG_0000.JPG"],
            "chapters": [{"label": "Chapter 1", "title": "Two villains meet", "items": [
                {"pair": [{"file": ids["IMG_0001.JPG"], "caption": "She had a plan.", "focus": "45% 40%"},
                          {"file": ids["IMG_0002.JPG"], "caption": "So did he."}], "caption": "Optional line."},
                {"file": ids["IMG_0003.JPG"], "caption": "Good boy."}]}],
            "thanks": "Rest of the note.", "cta": "Now find yourself.", "signoff": "Srini & Megha"}


def test_draft_publish_and_who_sees_it(studio, anon):
    s, _, ev = studio
    gpath, cpath, ids = setup(s, ev)
    url = f"/api/events/{ev['id']}/story"
    assert s.get(url).json()["exists"] is False
    assert anon.get(gpath + "/story").status_code == 404                       # nothing made yet
    st = s.put(url, json={"story": story(ids)}).json()
    assert st["draft"]["title"] == "Srini & Megha" and st["published"] is None and st["changed"]
    assert st["photos"][ids["IMG_0001.JPG"]]["name"] == "IMG_0001.JPG"
    assert anon.get(gpath + "/story").status_code == 404                       # a draft isn't shown
    st = s.post(url + "/publish").json()
    assert st["published"] == st["draft"] and not st["changed"]
    for base in (gpath, cpath):
        j = anon.get(base + "/story").json()
        assert j["story"]["chapters"][0]["items"][0]["pair"][0]["focus"] == "45% 40%"
        assert j["photos"][ids["IMG_0003.JPG"]] == {"w": 320, "h": 240}
    # editing the draft doesn't change what guests see until it's published again
    d = story(ids); d["title"] = "Changed"
    assert s.put(url, json={"story": d}).json()["changed"] is True
    assert anon.get(gpath + "/story").json()["story"]["title"] == "Srini & Megha"
    # switched off on one page
    s.patch(url, json={"for_guests": False})
    assert anon.get(gpath + "/story").status_code == 404 and anon.get(cpath + "/story").status_code == 200
    s.patch(url, json={"for_guests": True, "for_client": False})
    assert anon.get(gpath + "/story").status_code == 200 and anon.get(cpath + "/story").status_code == 404
    s.post(url + "/unpublish")
    assert anon.get(gpath + "/story").status_code == 404
    assert s.delete(url).json()["exists"] is False


def test_guest_pin_and_story_photos(studio, anon):
    s, _, ev = studio
    gpath, cpath, ids = setup(s, ev)
    s.put(f"/api/events/{ev['id']}/story", json={"story": story(ids)})
    s.post(f"/api/events/{ev['id']}/story/publish")
    s.post(f"/api/events/{ev['id']}/guest-pin", json={"pin": "4321"})
    assert anon.get(gpath + "/story").status_code == 401                       # behind the guest PIN
    assert anon.get(f"{gpath}/story/{ids['IMG_0001.JPG']}.jpg").status_code == 401
    anon.c.post(gpath + "/unlock", json={"pin": "4321"})
    guest = anon.c  # the unlocked browser keeps its cookie
    assert guest.get(gpath + "/story").status_code == 200
    r = guest.get(f"{gpath}/story/{ids['IMG_0001.JPG']}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert guest.get(f"{gpath}/story/{ids['IMG_0004.JPG']}.jpg").status_code == 404   # in the event, not in the story
    assert guest.get(f"{gpath}/story/nope.jpg").status_code == 404


def test_other_events_photos_and_file_names(studio, make_user):
    s, cid, ev = studio
    gpath, _, ids = setup(s, ev)
    other = s.post("/api/events", json={"client_id": cid, "name": "Reception", "event_date": "2026-11-21"}).json()
    upload(s, other["id"], [("OTHER.JPG", jpeg_bytes(seed=40))]); wait_idle(s, other["id"])
    oid = s.get(f"/api/events/{other['id']}/photos").json()[0]["id"]
    bad = story(ids); bad["chapters"][0]["items"].append({"file": oid})
    r = s.put(f"/api/events/{ev['id']}/story", json={"story": bad})
    assert r.status_code == 422 and oid in r.json()["message"]                  # another event's photo is refused
    # the playbook's story.json names photos by file name: they become the event's photos
    by_name = {"title": "T", "chapters": [{"title": "C", "items": [{"file": "img_0004.jpg", "caption": "x"},
                                                                    {"pair": [{"file": "IMG_0001.JPG"}, {"file": ids["IMG_0002.JPG"]}]}]}]}
    st = s.put(f"/api/events/{ev['id']}/story", json={"story": by_name}).json()
    items = st["draft"]["chapters"][0]["items"]
    assert items[0]["file"] == ids["IMG_0004.JPG"] and items[1]["pair"][0]["file"] == ids["IMG_0001.JPG"]
    # another company can't read or change it
    t = make_user("b@studio.test", "Studio B")
    assert t.get(f"/api/events/{ev['id']}/story").status_code == 404
    assert t.put(f"/api/events/{ev['id']}/story", json={"story": by_name}).status_code == 404
    assert t.get(f"/api/events/{ev['id']}/story/preview").status_code == 404


def test_shape_is_checked(studio):
    s, _, ev = studio
    _, _, ids = setup(s, ev, 2)
    url = f"/api/events/{ev['id']}/story"
    one = ids["IMG_0000.JPG"]
    assert s.put(url, json={"story": {"chapters": [{"items": [{"file": one, "pair": [{"file": one}, {"file": one}]}]}]}}).status_code == 422
    assert s.put(url, json={"story": {"chapters": [{"items": [{"pair": [{"file": one}]}]}]}}).status_code == 422
    assert s.put(url, json={"story": {"chapters": [{"items": [{"file": one, "caption": "x" * 301}]}]}}).status_code == 422
    assert s.put(url, json={"story": {"chapters": [{"items": [{"pair": [{"file": one, "focus": "javascript:1"}, {"file": one}]}]}]}}).status_code == 422
    assert s.post(url + "/publish").status_code == 400                         # nothing to publish yet
    assert s.put(url, json={"story": {"title": "<img src=x onerror=alert(1)>"}}).status_code == 200   # stored as text; pages use textContent


def test_deleted_photo_drops_out(studio, anon):
    s, _, ev = studio
    gpath, _, ids = setup(s, ev)
    s.put(f"/api/events/{ev['id']}/story", json={"story": story(ids)})
    s.post(f"/api/events/{ev['id']}/story/publish")
    s.post(f"/api/events/{ev['id']}/photos/delete", json={"ids": [ids["IMG_0002.JPG"], ids["IMG_0000.JPG"]]})
    j = anon.get(gpath + "/story").json()["story"]
    assert "cover" not in j
    first = j["chapters"][0]["items"][0]
    assert first == {"file": ids["IMG_0001.JPG"], "caption": "She had a plan.", "focus": "45% 40%"}   # a pair missing one: a single
    assert s.get(f"/api/events/{ev['id']}/story").json()["missing"] == sorted([ids["IMG_0000.JPG"], ids["IMG_0002.JPG"]])


def test_preview_page_can_be_framed_by_the_dashboard_only(studio, anon):
    s, _, ev = studio
    setup(s, ev, 1)
    r = s.get(f"/api/events/{ev['id']}/story/preview?page=client")
    assert r.status_code == 200 and "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and "pfStory" in r.text
    assert anon.get(f"/api/events/{ev['id']}/story/preview").status_code == 401
    slug = s.get(f"/api/events/{ev['id']}").json()["slug"]
    page = anon.get(f"/e/{slug}")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"] and "pfStory" in page.text
