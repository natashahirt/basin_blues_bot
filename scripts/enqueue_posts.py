from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image
from PIL.ExifTags import TAGS
from pillow_heif import register_heif_opener

register_heif_opener()

ASSETS_DIR = Path("assets")
VALID_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
PUBLISHABLE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
HEIF_SUFFIXES = {".heic", ".heif"}
FILENAME_DT_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})[_-]?(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
    re.compile(r"IMG[_-](?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})[_-](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
]


@dataclass
class ImageJob:
    path: str
    asset_relpath: str
    public_url: str
    sha256: str
    capture_iso: str
    caption: str


def main() -> None:
    ingest_url = require_env("CF_INGEST_URL")
    ingest_token = require_env("CF_INGEST_TOKEN")
    public_base_url = get_public_base_url()
    repository = require_env("GITHUB_REPOSITORY")
    sha = require_env("GITHUB_SHA")
    event_path = require_env("GITHUB_EVENT_PATH")

    changed_files = get_changed_files(event_path)
    image_paths = [Path(p) for p in changed_files if is_supported_asset(Path(p))]
    existing_image_paths = [path for path in image_paths if path.exists()]
    missing_image_paths = [path for path in image_paths if not path.exists()]

    for path in missing_image_paths:
        print(f"Skipping removed asset path from git diff: {path}")

    if not existing_image_paths:
        print("No supported image files changed under assets/.")
        return

    jobs = [build_job(path, public_base_url) for path in sorted(existing_image_paths)]
    payload = {
        "repository": repository,
        "commit_sha": sha,
        "jobs": [job.__dict__ for job in jobs],
    }

    headers = {
        "Authorization": f"Bearer {ingest_token}",
        "Content-Type": "application/json",
    }
    response = requests.post(ingest_url, headers=headers, data=json.dumps(payload), timeout=60)
    response.raise_for_status()
    print(response.text)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_public_base_url() -> str:
    # Keep the old default for backward compatibility, but prefer setting this
    # explicitly per environment (for example assets.nkhirt.com).
    return os.getenv("PUBLIC_BASE_URL", "https://nkhirt.com").rstrip("/")


def get_changed_files(event_path: str) -> list[str]:
    with open(event_path, "r", encoding="utf-8") as f:
        event = json.load(f)

    before = event.get("before")
    after = event.get("after") or os.getenv("GITHUB_SHA")

    if before and before != "0" * 40:
        cmd = ["git", "diff", "--name-only", before, after]
    else:
        cmd = ["git", "show", "--pretty=", "--name-only", after]

    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    files = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return files


def is_supported_asset(path: Path) -> bool:
    return path.parts and path.parts[0] == ASSETS_DIR.name and path.suffix.lower() in VALID_SUFFIXES


def build_job(path: Path, public_base_url: str) -> ImageJob:
    if not path.exists():
        raise FileNotFoundError(f"Changed file no longer exists in checkout: {path}")

    capture_dt = get_capture_time(path)
    sha256 = file_sha256(path)
    publish_path = get_publishable_path(path)
    asset_relpath = publish_path.as_posix()
    public_url = f"{public_base_url}/{asset_relpath}"

    return ImageJob(
        path=asset_relpath,
        asset_relpath=asset_relpath,
        public_url=public_url,
        sha256=sha256,
        capture_iso=capture_dt.isoformat(),
        caption=format_caption(capture_dt),
    )


def format_caption(capture_dt: datetime) -> str:
    """Format caption as HH:MM | DD Month YYYY in 24-hour time."""
    return capture_dt.strftime("%H:%M | %d %B %Y")


def get_publishable_path(path: Path) -> Path:
    suffix = path.suffix.lower()
    if suffix in PUBLISHABLE_SUFFIXES:
        return path
    if suffix not in HEIF_SUFFIXES:
        raise RuntimeError(f"Unsupported image extension for publish URL: {path.suffix}")

    # Meta's image publishing endpoint expects a web-retrievable image URL.
    # Keep HEIC files in-repo, but publish through a committed companion image.
    for candidate_suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = path.with_suffix(candidate_suffix)
        if candidate.exists():
            return candidate

    raise RuntimeError(
        f"{path} is HEIC/HEIF but has no companion publishable asset. "
        "Add a same-stem .jpg/.jpeg/.png/.webp file under assets/."
    )


def get_capture_time(path: Path) -> datetime:
    exif_dt = get_exif_datetime(path)
    if exif_dt is not None:
        return exif_dt

    filename_dt = get_filename_datetime(path.name)
    if filename_dt is not None:
        return filename_dt

    return datetime.fromtimestamp(path.stat().st_mtime)


def get_exif_datetime(path: Path) -> datetime | None:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except Exception:
        return None

    if not exif:
        return None

    mapped = {}
    for tag_id, value in exif.items():
        mapped[TAGS.get(tag_id, tag_id)] = value

    raw = mapped.get("DateTimeOriginal") or mapped.get("DateTime")
    if not raw:
        return None

    try:
        return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def get_filename_datetime(filename: str) -> datetime | None:
    for pattern in FILENAME_DT_PATTERNS:
        match = pattern.search(filename)
        if match:
            try:
                return datetime(
                    year=int(match.group("y")),
                    month=int(match.group("m")),
                    day=int(match.group("d")),
                    hour=int(match.group("h")),
                    minute=int(match.group("mi")),
                    second=int(match.group("s")),
                )
            except ValueError:
                return None
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
