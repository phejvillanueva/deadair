"""Small helpers: binary discovery, filename sanitising, formatting."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def find_binary(name: str) -> str | None:
    """Locate ffmpeg/ffprobe.

    Order: DEADAIR_FFMPEG / DEADAIR_FFPROBE env var, a bundled copy in
    ``<project>/ffmpeg/bin``, then PATH.
    """
    env = os.environ.get(f"DEADAIR_{name.upper()}")
    if env and Path(env).is_file():
        return env
    exe = name + (".exe" if os.name == "nt" else "")
    for candidate in (PROJECT_ROOT / "ffmpeg" / "bin" / exe, PROJECT_ROOT / "ffmpeg" / exe):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def subprocess_kwargs() -> dict:
    """Keep console windows from flashing on Windows."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def sanitize_filename(name: str, default: str = "video") -> str:
    """Return a safe single-component filename (no paths, no reserved chars)."""
    name = unicodedata.normalize("NFKC", name or "")
    name = name.replace("\\", "/").split("/")[-1]              # drop any directory part
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)           # Windows-illegal + control chars
    stem, dot, ext = name.strip().rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem.strip(" .")[:100] or default
    if stem.split(".")[0].upper() in _WINDOWS_RESERVED:
        stem = "_" + stem
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8].lower()
    return f"{stem}.{ext}" if ext else stem


def has_allowed_extension(name: str) -> bool:
    return Path(name.replace("\\", "/")).suffix.lower() in ALLOWED_EXTENSIONS


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """directory/stem+suffix, adding _2, _3 ... if it already exists."""
    candidate = directory / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def fmt_clock(seconds: float) -> str:
    """12.45 -> '00:12.45'; one hour or more -> '1:02:03.50'."""
    seconds = max(0.0, seconds)
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    s = (total_cs // 100) % 60
    m = (total_cs // 6000) % 60
    h = total_cs // 360000
    if h:
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
    return f"{m:02d}:{s:02d}.{cs:02d}"


def fmt_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def open_in_os(path: Path, select: bool = False) -> None:
    """Open a file/folder with the OS default handler (Explorer on Windows)."""
    if sys.platform == "win32":
        if select:
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if select else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if select else path)])
