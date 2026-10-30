"""The hub's own small pages (sign-in, account, admin). Plain HTML, no scripts (CSP allows none)."""

from __future__ import annotations

import time
from html import escape
from typing import Any

_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#5b6475;--line:#d9dde5;--accent:#0b63c5;--err:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#16181d;--card:#1f2229;--text:#e7e9ee;--muted:#a3abba;--line:#343944;--accent:#6aa9ff;--err:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Segoe UI",sans-serif}
main{max-width:560px;margin:10vh auto 0;padding:0 16px}main.wide{max-width:1100px;margin-top:4vh}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:24px 28px;margin-bottom:16px}
h1{font-size:21px;margin:0 0 4px}h2{font-size:16px;margin:18px 0 8px}p{margin:8px 0;color:var(--muted)}
.brand{font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.btn{display:inline-block;font:inherit;padding:8px 18px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);
color:#fff;text-decoration:none;cursor:pointer}.btn.ghost{background:transparent;color:var(--accent)}
.btn.small{padding:3px 10px;font-size:13px}input[type=text]{font:inherit;width:100%;padding:8px 10px;border-radius:6px;
border:1px solid var(--line);background:var(--bg);color:var(--text);margin:8px 0 12px}
.err{color:var(--err)}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}code{font-size:12.5px}ol{color:var(--muted);padding-left:20px}
"""


def page(title: str, body: str, wide: bool = False) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)} · SAS SpeedyStore Studio</title><style>{_CSS}</style></head>"
            f'<body><main class="{"wide" if wide else ""}">{body}</main></body></html>')


def _brand() -> str:
    return '<div class="brand">SAS SpeedyStore Studio</div>'


def login(viya: str, error: str | None = None, note: str | None = None) -> str:
    msg = f'<p class="err">{escape(error)}</p>' if error else ""
    msg += f"<p>{escape(note)}</p>" if note else ""
    return page("Sign in", f"""<div class="card">{_brand()}<h1>Sign in</h1>
<p>Sign in with your SAS user and password on SAS Logon ({escape(viya)}). Studio never sees your password.</p>{msg}
<a class="btn" href="/login/start">Sign in with SAS</a></div>""")


def paste_code(authorize_url: str, csrf: str, error: str | None = None) -> str:
    msg = f'<p class="err">{escape(error)}</p>' if error else ""
    return page("Sign in", f"""<div class="card">{_brand()}<h1>Sign in with SAS</h1>
<ol><li><a href="{escape(authorize_url)}" target="_blank" rel="noopener noreferrer">Open SAS Logon</a> (a new tab) and sign in.</li>
<li>SAS Logon then shows a code: copy it and paste it here.</li></ol>{msg}
<form method="post" action="/login/code"><input type="hidden" name="csrf" value="{escape(csrf)}">
<input type="text" name="code" autocomplete="off" spellcheck="false" placeholder="paste the code SAS Logon shows" required autofocus>
<button class="btn" type="submit">Continue</button></form></div>""")


def message(title: str, text: str, link: tuple[str, str] | None = None, error: bool = False) -> str:
    action = f'<p><a class="btn ghost" href="{escape(link[1])}">{escape(link[0])}</a></p>' if link else ""
    return page(title, f'<div class="card">{_brand()}<h1>{escape(title)}</h1><p class="{"err" if error else ""}">{escape(text)}</p>{action}</div>')


def account(name: str, user: str, csrf: str, is_admin: bool, running: bool) -> str:
    admin = '<a class="btn ghost" href="/admin">Admin</a>' if is_admin else ""
    state = "Your Studio is running." if running else "Your Studio isn't running; it starts when you open it."
    return page("Account", f"""<div class="card">{_brand()}<h1>{escape(name)}</h1><p>Signed in as <code>{escape(user)}</code>. {state}</p>
<div class="row"><a class="btn" href="/studio/">Open Studio</a>{admin}
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{escape(csrf)}"><button class="btn ghost" type="submit">Sign out</button></form></div>
<p>Signing out stops your Studio. Your connections, SQL tabs and files stay in your own folder for next time.</p></div>""")


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = max(0, time.time() - ts)
    return f"{int(s)} s" if s < 90 else f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h"


def _mb(n: int | None) -> str:
    return "" if n is None else f"{n / 1048576:.0f} MB"


def admin(rows: list[dict[str, Any]], sessions: list[dict[str, Any]], events: list[dict[str, Any]], csrf: str,
          limits: dict[str, Any]) -> str:
    studio_rows = "".join(
        f"<tr><td><code>{escape(r['user'])}</code></td><td>{escape(str(r.get('pid') or ''))}</td><td>{escape(r['state'])}</td>"
        f"<td>{_ago(r.get('started'))}</td><td>{_ago(r.get('last_activity'))}</td><td>{_mb(r.get('rss'))}</td>"
        f'<td><form method="post" action="/admin/stop"><input type="hidden" name="csrf" value="{escape(csrf)}">'
        f'<input type="hidden" name="user" value="{escape(r["user"])}"><button class="btn small ghost" type="submit">Stop</button></form></td></tr>'
        for r in rows) or '<tr><td colspan="7">No Studio is running.</td></tr>'
    session_rows = "".join(
        f"<tr><td><code>{escape(s['user'])}</code></td><td>{escape(s.get('name') or '')}</td><td>{escape(s.get('ip') or '')}</td>"
        f"<td>{_ago(s.get('created'))}</td><td>{_ago(s.get('last_seen'))}</td></tr>" for s in sessions) \
        or '<tr><td colspan="5">Nobody is signed in.</td></tr>'
    event_rows = "".join(
        f"<tr><td>{escape(e.get('ts', ''))}</td><td>{escape(e.get('event', ''))}</td><td><code>{escape(str(e.get('user') or ''))}</code></td>"
        f"<td>{escape(', '.join(f'{k}={v}' for k, v in e.items() if k not in ('ts', 'event', 'user')))}</td></tr>" for e in events)
    lim = ", ".join(f"{k}: {v}" for k, v in limits.items())
    return page("Admin", f"""<div class="card">{_brand()}<h1>Admin</h1><p>{escape(lim)}</p>
<div class="row"><a class="btn ghost" href="/account">Account</a><a class="btn ghost" href="/admin">Refresh</a></div>
<h2>Studios</h2><table><tr><th>User</th><th>PID</th><th>State</th><th>Started</th><th>Last use</th><th>Memory</th><th></th></tr>{studio_rows}</table>
<h2>Signed in</h2><table><tr><th>User</th><th>Name</th><th>Address</th><th>Signed in</th><th>Last request</th></tr>{session_rows}</table>
<h2>Recent events</h2><table><tr><th>Time (UTC)</th><th>Event</th><th>User</th><th>Details</th></tr>{event_rows}</table></div>""", wide=True)
