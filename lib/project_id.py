from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:([/\\\\]|$)")
_MNT_DRIVE_RE = re.compile(r"^/mnt/([A-Za-z])/(.*)$")
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])/(.*)$")


def normalize_work_dir(value: str | Path) -> str:
    """
    Normalize a work_dir into a stable string for hashing and matching.

    Goals:
    - Be stable within a single environment (Linux/WSL/Windows/MSYS).
    - Reduce trivial path-format mismatches (slashes, drive letter casing, /mnt/<drive> mapping).
    - Avoid resolve() by default to reduce symlink/interop surprises.
    """
    raw = str(value).strip()
    if not raw:
        return ""

    # Expand "~" early.
    if raw.startswith("~"):
        try:
            raw = os.path.expanduser(raw)
        except Exception:
            pass

    # Absolutize when relative (best-effort).
    try:
        preview = raw.replace("\\", "/")
        is_abs = (
            preview.startswith("/")
            or preview.startswith("//")
            or preview.startswith("\\\\")
            or bool(_WIN_DRIVE_RE.match(preview))
        )
        if not is_abs:
            raw = str((Path.cwd() / Path(raw)).absolute())
    except Exception:
        pass

    s = raw.replace("\\", "/")

    # Map WSL mount paths to a Windows-like drive form for stable matching.
    m = _MNT_DRIVE_RE.match(s)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2)
        s = f"{drive}:/{rest}"
    else:
        # Map MSYS /c/... to c:/...
        m = _MSYS_DRIVE_RE.match(s)
        if m and ("MSYSTEM" in os.environ or os.name == "nt"):
            drive = m.group(1).lower()
            rest = m.group(2)
            s = f"{drive}:/{rest}"

    # Collapse redundant separators and dot segments using POSIX semantics (we forced "/").
    if s.startswith("//"):
        prefix = "//"
        rest = posixpath.normpath(s[2:])
        s = prefix + rest.lstrip("/")
    else:
        s = posixpath.normpath(s)

    # Normalize Windows drive letter casing.
    if _WIN_DRIVE_RE.match(s):
        s = s[0].lower() + s[1:]

    return s


def _find_ccb_config_root(start_dir: Path) -> Path | None:
    """
    Find the nearest ancestor directory that contains a `.ccb_config/` directory.

    This is CCB's project root anchor and is used to make project_id stable across subdirectories.
    """
    try:
        current = Path(start_dir).expanduser().absolute()
    except Exception:
        current = Path.cwd()
    for candidate in [current, *current.parents]:
        try:
            cfg = candidate / ".ccb_config"
            if cfg.is_dir():
                return candidate
        except Exception:
            continue
    return None


def compute_ccb_project_id(work_dir: Path) -> str:
    """
    Compute CCB's routing project id (ccb_project_id).

    Priority:
    - `CCB_PROJECT_ROOT` env var (explicit project root).
    - Nearest ancestor directory containing `.ccb_config/` (project anchor).
    - Current work_dir (fallback).
    """
    try:
        wd = Path(work_dir).expanduser().absolute()
    except Exception:
        wd = Path.cwd()

    # Priority 1: Explicit env var
    env_root = (os.environ.get("CCB_PROJECT_ROOT") or "").strip()
    if env_root:
        try:
            root = Path(os.path.expanduser(env_root))
            if root.exists() and root.is_dir():
                base = root.absolute()
            else:
                base = None
        except Exception:
            base = None
    else:
        base = None

    # Priority 2: Nearest `.ccb_config/` ancestor
    if base is None:
        base = _find_ccb_config_root(wd) or wd

    norm = normalize_work_dir(base)
    if not norm:
        norm = normalize_work_dir(wd)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
