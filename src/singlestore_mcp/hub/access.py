"""Who may use Studio, and who may use notebooks: two SAS groups (Viya identities), checked with the user's own token.

    STUDIO_USERS_GROUP      members may use Studio
    STUDIO_NOTEBOOK_GROUP   members may use notebooks (and so Studio as well)
    STUDIO_ALLOWED_USERS    an extra allow-list of SAS user ids (with no group set: the only list, as before)

Both groups empty: everyone who can sign in may use Studio (or only STUDIO_ALLOWED_USERS when that is set).
Notebooks need the server-wide switch STUDIO_NOTEBOOKS=local and then go only to members of the notebook group - or,
with no groups set, to the users in STUDIO_ALLOWED_USERS (nobody when that is empty: local notebooks have no
isolation between users, so they are never on for "everyone").

The groups come from ``/identities/users/@currentUser/memberships`` (paged with ``start``/``limit``; the Viya
identities service's own collection format: ``items``, ``count``, ``links``), with the user's own access token -
no service account. A group matches by its id or its name, ignoring case. Nested groups: only what that call
returns counts (the identities API isn't publicly documented on this point - see deploy/ADMIN-GUIDE.md: put users
in the two groups directly until it is confirmed on the target Viya).

Used by the hub at sign-in and by each Studio after it renewed its Viya token (``studio_server``), so a user taken
out of a group loses access within the token's lifetime. Stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

MEMBERSHIPS = "/identities/users/@currentUser/memberships"
_PAGE = 500
_MAX_PAGES = 20


class GroupCheckFailed(RuntimeError):
    """The identities service didn't answer (or refused): membership unknown."""


@dataclass
class Rules:
    users_group: str = ""
    notebook_group: str = ""
    allowed_users: tuple[str, ...] = ()
    notebooks: bool = False          # STUDIO_NOTEBOOKS: the server-wide switch

    @property
    def needs_groups(self) -> bool:
        return bool(self.users_group or self.notebook_group)

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "allowed_users": list(self.allowed_users)}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Rules:
        return cls(users_group=str(d.get("users_group") or ""), notebook_group=str(d.get("notebook_group") or ""),
                   allowed_users=tuple(d.get("allowed_users") or ()), notebooks=bool(d.get("notebooks")))


@dataclass
class Access:
    app: bool
    app_via: str            # what gave (or refused) app access, for the admin page and the audit log
    notebooks: bool
    notebooks_via: str
    reason: str = ""        # why app access was refused (safe to show)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def fetch_groups(get: Callable[[str], tuple[int, Any]], user_id: str = "") -> list[dict[str, str]]:
    """The user's groups ({"id", "name"}), all pages. ``get(path)`` does an authorized GET on the Viya and returns
    (status, parsed JSON). Raises GroupCheckFailed when the answer isn't a membership list."""
    base = MEMBERSHIPS
    out: list[dict[str, str]] = []
    start = 0
    for _ in range(_MAX_PAGES):
        status, body = get(f"{base}?start={start}&limit={_PAGE}")
        if status == 404 and base == MEMBERSHIPS and user_id and start == 0:
            base = f"/identities/users/{quote(user_id, safe='')}/memberships"  # a Viya without the @currentUser alias
            status, body = get(f"{base}?start={start}&limit={_PAGE}")
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise GroupCheckFailed(f"SAS Viya's identities service answered HTTP {status}.")
        items = body["items"]
        out += [{"id": str(g.get("id") or ""), "name": str(g.get("name") or "")} for g in items if isinstance(g, dict)]
        total = body.get("count")
        more = any(isinstance(link, dict) and link.get("rel") == "next" for link in body.get("links") or [])
        start += len(items)
        if not items or (isinstance(total, int) and start >= total) or (not isinstance(total, int) and not more):
            return out
    return out


def _member(groups: list[dict[str, str]], group: str) -> bool:
    g = group.strip().lower()
    return bool(g) and any(g in (x["id"].lower(), x["name"].lower()) for x in groups)


def decide(user: str, rules: Rules, groups: list[dict[str, str]] | None) -> Access:
    """App and notebook access for ``user``. ``groups``: their memberships, or None when the check failed (only
    asked for when a group is configured)."""
    listed = user.lower() in {u.lower() for u in rules.allowed_users}
    if not rules.needs_groups:
        app = not rules.allowed_users or listed
        via = "STUDIO_ALLOWED_USERS" if rules.allowed_users else "everyone who can sign in"
        nb = app and rules.notebooks and listed
        nb_via = ("off on this server" if not rules.notebooks else "STUDIO_ALLOWED_USERS" if nb
                  else "only for STUDIO_ALLOWED_USERS")
        return Access(app=app, app_via=via, notebooks=nb, notebooks_via=nb_via,
                      reason="" if app else f"{user} isn't allowed to use this Studio. Ask the Studio admin.")
    if groups is None:
        return Access(app=False, app_via="group check failed", notebooks=False, notebooks_via="group check failed",
                      reason="Studio couldn't check your SAS group membership (SAS Viya's identities service didn't "
                             "answer). Try again in a few minutes.")
    in_users = bool(rules.users_group) and _member(groups, rules.users_group)
    in_nb = bool(rules.notebook_group) and _member(groups, rules.notebook_group)
    if in_users:
        app, app_via = True, f"group {rules.users_group}"
    elif in_nb:
        app, app_via = True, f"group {rules.notebook_group}"
    elif listed:
        app, app_via = True, "STUDIO_ALLOWED_USERS"
    else:
        app, app_via = False, "not in " + " or ".join(g for g in (rules.users_group, rules.notebook_group) if g)
    if not rules.notebooks:
        nb, nb_via = False, "off on this server"
    elif rules.notebook_group:
        nb, nb_via = in_nb, f"group {rules.notebook_group}" if in_nb else f"not in {rules.notebook_group}"
    else:  # groups for the app only: notebooks for the extra list
        nb, nb_via = listed, "STUDIO_ALLOWED_USERS" if listed else "only for STUDIO_ALLOWED_USERS"
    groups_text = " or ".join(g for g in (rules.users_group, rules.notebook_group) if g)
    return Access(app=app, app_via=app_via, notebooks=app and nb, notebooks_via=nb_via,
                  reason="" if app else f"{user} isn't in the SAS group {groups_text} that may use this Studio. "
                                        "Ask the Studio admin to add you.")
