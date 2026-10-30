import asyncio
import logging
import shutil
import datetime
from pathlib import Path
from typing import Union, Optional

from utils.constants import PLATFORM_CONFIG
from typing import Optional

logger = logging.getLogger(__name__)
_video_run_dir: Optional[Path] = None
_last_recording_url: Optional[str] = None

def register_video_run_dir(path: Path) -> None:
    """Register the active video run directory so screenshots can be saved there."""
    global _video_run_dir
    _video_run_dir = path

def get_video_run_dir() -> Optional[Path]:
    """Get the currently registered video run directory."""
    return _video_run_dir

def set_last_recording_url(url: Optional[str]) -> None:
    """Register the most recent recording URL (e.g., S3 presigned URL)."""
    global _last_recording_url
    _last_recording_url = url

def get_last_recording_url() -> Optional[str]:
    """Retrieve the most recent recording URL if available."""
    return _last_recording_url


def clear_video_run_dir_registration() -> None:
    """Clear the active screenshot/recording directory pointer after a run ends."""
    global _video_run_dir
    _video_run_dir = None


def cleanup_orphan_recording_dir(path: Optional[Path]) -> None:
    """
    Best-effort remove a per-run recording directory if it still exists.
    Used when zipping/upload fails partway so temp folders do not accumulate on disk.
    """
    if not path:
        return
    try:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        logger.exception("cleanup_orphan_recording_dir failed for %s", path)
        

def _prune_old_zips(base: Path, ttl_minutes: int) -> None:
    """
    Delete only .zip files older than ttl_minutes (based on mtime).
    Recurses into subdirectories under the base.
    """
    if not base.exists():
        return

    now = datetime.datetime.now().timestamp()
    ttl_seconds = ttl_minutes * 60

    deleted = 0
    kept = 0

    for zip_path in base.rglob("*.zip"):
        try:
            age = now - zip_path.stat().st_mtime
        except FileNotFoundError:
            continue

        # +30s safety margin in case a file was just written
        if age > (ttl_seconds + 30):
            try:
                zip_path.unlink()
                deleted += 1
                logger.info("Deleted stale recording zip: %s (age=%.0fs)", zip_path, age)
            except Exception:
                logger.exception("Failed to delete zip: %s", zip_path)
        else:
            kept += 1

    logger.debug("Recording prune summary: deleted=%s kept=%s base=%s", deleted, kept, base)

async def clean_recording_folder_forever() -> None:
    """
    Periodically prune ONLY .zip files older than the configured TTL.
    Never deletes the recordings base folder.
    """
    scan_interval_min = int(PLATFORM_CONFIG.get("RECORDING_CLEAN_INTERVAL_MINUTES", 1))
    ttl_min = int(PLATFORM_CONFIG.get("RECORDING_TTL_MINUTES", 15))
    base = _recording_base_dir()

    # Ensure base exists
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to ensure recording dir exists at startup: %s", base)

    logger.info(
        "Recording cleanup scheduler started. Folder=%s, scan=%s min, TTL=%s min",
        str(base), scan_interval_min, ttl_min
    )

    while True:
        try:
            _prune_old_zips(base, ttl_min)
        except Exception:
            logger.exception("Recording cleanup run failed")
        await asyncio.sleep(scan_interval_min * 60)


def _recording_base_dir() -> Path:
    """
    Recording base directory from config (default 'target/recordings'),
    resolved against the repo root if it’s relative.
    """
    cfg = PLATFORM_CONFIG.get("RECORDING_DIR", "target/recordings")
    base = Path(cfg)
    if not base.is_absolute():
        base = _repo_root() / base
    base.mkdir(parents=True, exist_ok=True)
    return base


def _repo_root() -> Path:
    """
    Resolve the project root that owns THIS app.

    Order:
      1) Env overrides: CHEWY_APP_ROOT, APP_ROOT, PROJECT_ROOT
      2) If inside a container and /app exists, prefer /app
      3) Walk upward from this file and pick the highest parent whose `src/` contains this file
      4) First ancestor that has a `.git` or `pyproject.toml`
      5) Fallback to CWD
    """
    # 1) explicit env overrides
    import os
    for k in ("CHEWY_APP_ROOT", "APP_ROOT", "PROJECT_ROOT"):
        v = os.environ.get(k)
        if v:
            p = Path(v).resolve()
            if p.exists():
                return p

    # 2) common container workdir
    app_dir = Path("/app")
    if app_dir.exists() and app_dir.is_dir():
        return app_dir

    # 3) highest parent whose `src/` contains this file
    here = Path(__file__).resolve()
    best = None
    for p in here.parents:
        src_dir = p / "src"
        try:
            if src_dir.is_dir() and src_dir in here.parents:
                best = p  # keep walking; we want the highest such parent
        except Exception:
            pass
    if best:
        return best

    # 4) marker files
    for p in [here] + list(here.parents):
        if (p / ".git").exists() or (p / "pyproject.toml").exists():
            return p

    # 5) last resort
    return Path.cwd()

def _setup_recording_dir(prefix: str, scenario_id: str) -> Path:
    """
    Always place recordings under the configured base dir (repo-root relative).
    """
    base = _recording_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    folder = base / f"{prefix}_{scenario_id}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def _build_download_url(file_path: Path) -> str:
    """
    Build a public URL that maps to the FastAPI static mount at /recordings.
    If PLATFORM_CONFIG['PUBLIC_BASE_URL'] is set, prefix with it, else return a path-only URL.

    Example:
      /recordings/<relative path under recording dir>
      or https://host/recordings/...
    """
    from urllib.parse import quote
    import os

    f = Path(file_path).resolve()
    base = _recording_base_dir().resolve()
    try:
        rel = f.relative_to(base)
    except ValueError:
        # Not under recordings dir; fall back to basename
        rel = f.name

    url_path = f"/recordings/{quote(str(rel).replace(os.sep, '/'))}"
    public_base = PLATFORM_CONFIG.get("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{public_base}{url_path}" if public_base else url_path


def _setup_failure_recording_dir(module_name: str, unique_id: str) -> Path:
    """
    Create a unique folder inside the configured RECORDING_DIR (from PLATFORM_CONFIG).
    Example: src/target/recording/playwright-test-<id>-<timestamp>
    """
    ts = int(datetime.datetime.now().timestamp() * 1000)

    # Always resolve relative to the configured RECORDING_DIR
    base = _recording_base_dir()

    d = base / f"{module_name}-{unique_id}-{ts}"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _cleanup_success_recording_dir(video_dir: Path):
    """Delete recording directory on success."""
    try:
        if video_dir.exists():
            shutil.rmtree(video_dir, ignore_errors=True)
    except Exception:
        pass


def _ensure_under(base: Path, candidate: Path) -> None:
    """Prevent path traversal: ensure candidate is inside base."""
    base = base.resolve()
    candidate = candidate.resolve()
    try:
        # Python 3.9+ has is_relative_to
        if not candidate.is_relative_to(base):  # type: ignore[attr-defined]
            raise ValueError
    except AttributeError:
        # Fallback for older Python
        if str(candidate).startswith(str(base)):
            return
        raise ValueError

def _find_latest_zip_for(prefix: str, base: Path) -> Optional[Path]:
    """Return the newest *.zip whose filename starts with `prefix`."""
    zips = sorted(
        (p for p in base.rglob("*.zip") if p.name.startswith(prefix)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return zips[0] if zips else None