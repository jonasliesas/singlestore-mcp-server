"""Move the SingleStore password out of Claude's config files into the OS credential store.

Older setups pass the connection to the server as SINGLESTORE_* variables in the ``env`` of the
``singlestore`` entry in ``~/.claude.json`` (Claude Code) and ``claude_desktop_config.json``
(Claude Desktop), so the password sits there in plain text. This script

1. saves each such connection as a saved connection (password in the OS credential store,
   reusing a saved connection with the same host, port and user when there is one),
2. makes it the active connection if none of the saved ones is active, and
3. removes the SINGLESTORE_* connection variables from the config files.

Nothing secret is printed. It's a dry run unless ``--apply`` is given. Quit Claude Desktop and
Claude Code first: they may write the old config back.

    uv run --no-sync python -m singlestore_mcp.move_password            # show what it would do
    uv run --no-sync python -m singlestore_mcp.move_password --apply    # do it
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import connections as conn

# The variables that describe a connection; others (SINGLESTORE_MCP_*) stay.
CONNECTION_VARS = (
    "SINGLESTORE_HOST", "SINGLESTORE_PORT", "SINGLESTORE_USER", "SINGLESTORE_PASSWORD", "SINGLESTORE_DATABASE",
    "SINGLESTORE_URL", "SINGLESTORE_SSL_DISABLED", "SINGLESTORE_SSL_CA", "SINGLESTORE_SSL_CERT",
    "SINGLESTORE_SSL_KEY", "SINGLESTORE_SSL_VERIFY", "SINGLESTORE_CREDENTIAL_TYPE",
)


def config_files() -> list[Path]:
    """Claude Code's and Claude Desktop's config files (and backups of the latter) that exist."""
    home = Path.home()
    out = [home / ".claude.json"]
    desktop_dirs = [home / "Library" / "Application Support" / "Claude", home / ".config" / "Claude"]
    if os.environ.get("APPDATA"):
        desktop_dirs.append(Path(os.environ["APPDATA"]) / "Claude")
    if os.environ.get("LOCALAPPDATA"):  # the Microsoft Store app keeps its own copy
        desktop_dirs += Path(os.environ["LOCALAPPDATA"], "Packages").glob("Claude_*/LocalCache/Roaming/Claude")
    for d in desktop_dirs:
        out += sorted(d.glob("claude_desktop_config*.json*"))
    return [p for p in out if p.is_file()]


def _server_envs(cfg: Any) -> list[dict[str, Any]]:
    """Every MCP server ``env`` in a config that holds SingleStore connection variables."""
    found: list[dict[str, Any]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            env = o.get("env")
            if "command" in o and isinstance(env, dict) and any(k in env for k in ("SINGLESTORE_HOST", "SINGLESTORE_URL")):
                found.append(env)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(cfg)
    return found


def _is_placeholder(value: str) -> bool:
    return value.startswith("${")  # "${SINGLESTORE_PASSWORD}" / "${input:…}": not a secret in the file


def _same_target(a: conn.Profile, b: conn.Profile) -> bool:
    if a.url or b.url:
        return a.url == b.url
    return (a.host.lower(), a.port, a.user.lower(), a.auth) == (b.host.lower(), b.port, b.user.lower(), b.auth)


def _new_name(p: conn.Profile) -> str:
    taken = {x.name for x in conn.profiles()}
    base = (f"{p.user}@{p.host}" if p.host else "SingleStore")[:52]
    name, n = base, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    return name


def plan_entry(env: dict[str, Any]) -> dict[str, Any]:
    """What to do with one config entry: which saved connection it maps to, and whether to store a password."""
    values = {k: str(v) for k, v in env.items()}
    if any(_is_placeholder(values.get(k, "")) for k in ("SINGLESTORE_HOST", "SINGLESTORE_URL", "SINGLESTORE_PASSWORD")):
        return {"action": "skip", "why": "uses ${…} placeholders, no password in the file"}
    p = conn._env_profile(values)
    if p is None:
        return {"action": "skip", "why": "no host"}
    password = values.get("SINGLESTORE_PASSWORD", "")
    match = next((s for s in conn.profiles() if not s.builtin and _same_target(s, p)), None)
    if match:
        stored = conn.get_password(match.name) if p.auth == "password" else None
        if p.auth == "password" and password and stored and stored != password:
            return {"action": "conflict", "name": match.name,
                    "why": "the saved connection has a different password; fix it in Connections, then run again"}
        return {"action": "reuse", "name": match.name, "store_password": bool(password and not stored and p.auth == "password"),
                "profile": p, "password": password}
    return {"action": "create", "name": _new_name(p), "store_password": bool(password), "profile": p, "password": password}


def _apply_entry(plan: dict[str, Any]) -> None:
    p: conn.Profile = plan["profile"]
    if plan["action"] == "create":
        fields = {k: v for k, v in p.__dict__.items() if k not in ("builtin", "name")}
        conn.save({**fields, "name": plan["name"]}, password=plan["password"] or None)
    elif plan.get("store_password"):
        conn.set_password(plan["name"], plan["password"])


def _strip(env: dict[str, Any]) -> list[str]:
    removed = [k for k in CONNECTION_VARS if k in env]
    for k in removed:
        del env[k]
    return removed


def _write_json(path: Path, original: str, cfg: Any) -> None:
    indent = 2 if "\n " in original[:2000] else None
    text = json.dumps(cfg, indent=indent, ensure_ascii=False) + ("\n" if original.endswith("\n") else "")
    json.loads(text)  # never write something Claude can't read
    tmp = path.with_name(path.name + ".tmp-move-password")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run(apply: bool, files: list[Path] | None = None, out=print) -> int:
    files = config_files() if files is None else files
    out(f"Credential store: {conn.password_store()}")
    problems = 0
    chosen: str | None = None
    for path in files:
        try:
            original = path.read_text(encoding="utf-8")
            cfg = json.loads(original)
        except (OSError, ValueError):
            continue  # not a JSON config (e.g. a half-edited copy): leave it alone
        envs = _server_envs(cfg)
        if not envs:
            continue
        out(f"\n{path}")
        changed = False
        for env in envs:
            plan = plan_entry(env)
            target = f"{env.get('SINGLESTORE_USER', '')}@{env.get('SINGLESTORE_HOST', env.get('SINGLESTORE_URL', '?'))}"
            if plan["action"] == "skip":
                out(f"  {target}: left as is ({plan['why']})")
                continue
            if plan["action"] == "conflict":
                out(f"  {target}: NOT moved – {plan['why']} ({plan['name']!r})")
                problems += 1
                continue
            verb = "save as new connection" if plan["action"] == "create" else "use saved connection"
            pw = ", store its password" if plan.get("store_password") else ""
            out(f"  {target}: {verb} {plan['name']!r}{pw}; remove {', '.join(k for k in CONNECTION_VARS if k in env)}")
            if apply:
                _apply_entry(plan)
                _strip(env)
                changed = True
            chosen = chosen or plan["name"]
        if apply and changed:
            _write_json(path, original, cfg)
            out("  written")
    active = conn._read().get("active")
    if chosen and (not active or active == conn.ENV_PROFILE or active not in {p.name for p in conn.profiles()}):
        out(f"\nActive connection: {chosen!r}" + ("" if apply else " (would be set)"))
        if apply:
            conn.activate(chosen)
    elif chosen:
        out(f"\nActive connection stays {active!r}.")
    if chosen is None and not problems:
        out("\nNo SingleStore passwords found in the Claude config files.")
    elif not apply:
        out("\nDry run: nothing changed. Quit Claude Desktop and Claude Code, then run again with --apply.")
    else:
        out("\nDone. Start Claude again; the server now signs in with the saved connection.")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    args = ap.parse_args(argv)
    return run(args.apply)


if __name__ == "__main__":
    sys.exit(main())
