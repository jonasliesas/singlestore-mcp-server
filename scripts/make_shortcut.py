"""Create a desktop shortcut that opens the standalone SingleStore Workspace.

    uv run python scripts/make_shortcut.py [--database SASDP] [--name "SingleStore Workspace"]

The shortcut runs ``pythonw -m singlestore_mcp.workspace_app`` from this
project's virtual environment (no console window). It also writes a small
database icon (scripts/singlestore-workspace.ico) for it. Windows only.
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "scripts" / "singlestore-workspace.ico"


def _png(size: int) -> bytes:
    """A purple database cylinder on a transparent background."""
    purple, light, white = (124, 58, 237), (167, 139, 250), (255, 255, 255)
    cx, rx = size / 2, size * 0.36
    ry = size * 0.11
    top, bottom = size * 0.2, size * 0.8
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            dx = (px - cx) / rx
            color, alpha = (0, 0, 0), 0
            inside_body = abs(dx) <= 1 and top <= py <= bottom
            def on_ellipse(yc: float) -> float:
                return dx * dx + ((py - yc) / ry) ** 2
            if inside_body or on_ellipse(bottom) <= 1:
                color, alpha = purple, 255
                # two white "disk" bands
                for band in (top + (bottom - top) / 3, top + 2 * (bottom - top) / 3):
                    d = on_ellipse(band)
                    if abs(dx) <= 1 and 0.75 <= d <= 1.0 and py > band:
                        color = white
            if on_ellipse(top) <= 1:
                color, alpha = light, 255
            row += bytes((*color, alpha))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def write_icon(path: Path) -> None:
    images = [_png(s) for s in (16, 32, 48, 256)]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, img in zip((16, 32, 48, 256), images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(img), offset + len(data))
        data += img
    path.write_bytes(header + entries + data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="")
    parser.add_argument("--name", default="SingleStore Workspace")
    parser.add_argument("--view", default="", help="view to open: sql, notebook, schema, pipelines, cluster, history or connections")
    opts = parser.parse_args()
    write_icon(ICON)
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    arguments = "-m singlestore_mcp.workspace_app" + (f" --database {opts.database}" if opts.database else "")
    if opts.view:
        arguments += f" --view {opts.view}"
    ps = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop '{opts.name}.lnk'))
$s.TargetPath = '{pythonw}'
$s.Arguments = '{arguments}'
$s.WorkingDirectory = '{ROOT}'
$s.IconLocation = '{ICON},0'
$s.Description = 'Open the SingleStore Workspace in its own window'
$s.Save()
Write-Output (Join-Path $desktop '{opts.name}.lnk')
"""
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True)
    print("Created", out.stdout.strip())


if __name__ == "__main__":
    main()
