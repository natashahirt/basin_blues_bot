#!/usr/bin/env python3
"""Generate publishable companion images for HEIC/HEIF assets.

This helper scans `assets/` for `.heic`/`.heif` files and writes same-stem
`.jpg` files (for example `assets/foo.heic` -> `assets/foo.jpg`). The enqueue
pipeline can then use the `.jpg` URL for Instagram publishing while preserving
the original HEIC as your source file.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()

DEFAULT_ASSETS_DIR = Path("assets")
HEIF_SUFFIXES = {".heic", ".heif"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=DEFAULT_ASSETS_DIR,
        help="Directory to scan (default: assets)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing .jpg companions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned work without writing files",
    )
    return parser.parse_args()


def iter_heif_files(assets_dir: Path) -> list[Path]:
    return sorted(
        p for p in assets_dir.rglob("*") if p.is_file() and p.suffix.lower() in HEIF_SUFFIXES
    )


def build_jpeg_path(source: Path) -> Path:
    return source.with_suffix(".jpg")


def create_companion(source: Path, target: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would create {target} from {source}")
        return

    with Image.open(source) as image:
        exif = image.info.get("exif")
        rgb = image.convert("RGB")
        save_kwargs: dict[str, object] = {"format": "JPEG", "quality": 95}
        if exif:
            save_kwargs["exif"] = exif
        rgb.save(target, **save_kwargs)
    print(f"created {target} from {source}")


def main() -> None:
    args = parse_args()
    assets_dir = args.assets_dir

    if not assets_dir.exists():
        raise FileNotFoundError(f"Assets directory does not exist: {assets_dir}")
    if not assets_dir.is_dir():
        raise NotADirectoryError(f"Expected directory: {assets_dir}")

    heif_files = iter_heif_files(assets_dir)
    if not heif_files:
        print(f"No HEIC/HEIF files found under {assets_dir}")
        return

    created_count = 0
    skipped_count = 0
    for source in heif_files:
        target = build_jpeg_path(source)
        if target.exists() and not args.force:
            skipped_count += 1
            print(f"skipped {source} (companion already exists: {target})")
            continue

        create_companion(source, target, args.dry_run)
        created_count += 1

    print(f"done: created={created_count}, skipped={skipped_count}, scanned={len(heif_files)}")


if __name__ == "__main__":
    main()
