"""Roles inside one company and what each may do. The server checks these on every request (server.can)."""

# permission -> the words used in "Your role (X) can't ..."
PERMS = {
    "team": "manage the team",
    "company": "change the company settings",
    "clients": "add, edit or delete clients",
    "events": "create, edit, publish or delete events",
    "upload": "upload photos",
    "delete_photos": "delete photos",
    "inventory": "manage the equipment inventory",
    "delete_account": "delete the company account",
}

# rank decides who may manage whom: you can only invite, change or remove people ranked below you
ROLES = {
    "owner": {"label": "Owner", "rank": 4, "perms": set(PERMS),
              "about": "Everything, including deleting the company. One per company."},
    "admin": {"label": "Admin", "rank": 3, "perms": set(PERMS) - {"delete_account"},
              "about": "Everything except deleting the company. Manages the team."},
    "manager": {"label": "Manager", "rank": 2, "perms": {"clients", "events", "upload", "delete_photos", "inventory"},
                "about": "Clients and events: create, publish, delete. Uploads and deletes photos. Manages the equipment."},
    "photographer": {"label": "Photographer", "rank": 1, "perms": {"upload"},
                     "about": "Uploads photos to existing events, checks equipment out and back in. Sees everything, changes nothing else."},
    "viewer": {"label": "Viewer", "rank": 0, "perms": set(),
               "about": "Read-only: events, photos, guest stats and share links."},
}
INVITABLE = [r for r in ROLES if r != "owner"]


def perms(role: str) -> set:
    return ROLES.get(role, ROLES["viewer"])["perms"]  # an unknown role gets the least


def label(role: str) -> str:
    return ROLES.get(role, {"label": role})["label"]


def rank(role: str) -> int:
    return ROLES.get(role, {"rank": -1})["rank"]


def can_manage(actor: str, target: str) -> bool:
    """May someone with role `actor` give or take away role `target`?"""
    return "team" in perms(actor) and rank(target) < rank(actor)


def public() -> list:
    return [{"role": k, "label": v["label"], "about": v["about"], "perms": sorted(v["perms"])} for k, v in ROLES.items()]
