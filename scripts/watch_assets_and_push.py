#!/usr/bin/env python3
"""Watch assets/ and auto-commit/push when files change.

Workflow for each detected batch:
1. Wait for file writes to settle (debounce).
2. Generate HEIC companion JPGs.
3. Stage only assets/.
4. Commit (if there is a staged change under assets/).
5. Push to remote.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

WATCH_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=Path("assets"),
        help="Folder to watch for image changes (default: assets)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="Polling interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--debounce-seconds",
        type=float,
        default=5.0,
        help="Wait time after last change before processing (default: 5.0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one change batch then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without running git commit/push",
    )
    return parser.parse_args()


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def snapshot_assets(assets_dir: Path) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in assets_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in WATCH_SUFFIXES:
            continue
        stat = path.stat()
        snapshot[path.relative_to(assets_dir)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def wait_for_settle(assets_dir: Path, debounce_seconds: float) -> None:
    stable_snapshot = snapshot_assets(assets_dir)
    stable_for = 0.0
    sleep_interval = min(1.0, max(0.2, debounce_seconds / 5.0))
    while stable_for < debounce_seconds:
        time.sleep(sleep_interval)
        current = snapshot_assets(assets_dir)
        if current == stable_snapshot:
            stable_for += sleep_interval
        else:
            stable_snapshot = current
            stable_for = 0.0


def git_has_asset_changes(repo_root: Path, assets_dir: Path) -> bool:
    result = run_command(["git", "status", "--porcelain", "--", str(assets_dir)], repo_root)
    return bool(result.stdout.strip())


def git_has_staged_changes(repo_root: Path, assets_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", str(assets_dir)],
        cwd=repo_root,
        text=True,
    )
    return result.returncode == 1


def process_batch(repo_root: Path, assets_dir: Path, dry_run: bool) -> None:
    helper = repo_root / "scripts/generate_heic_companions.py"
    print("Running HEIC companion generation...")
    run_command(["python", str(helper)], repo_root)

    if not git_has_asset_changes(repo_root, assets_dir):
        print("No asset changes to commit.")
        return

    print("Staging asset changes...")
    run_command(["git", "add", str(assets_dir)], repo_root)
    if not git_has_staged_changes(repo_root, assets_dir):
        print("No staged asset changes after git add.")
        return

    commit_message = f"chore: auto-sync assets {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    if dry_run:
        print(f"[dry-run] Would commit and push: {commit_message}")
        return

    print("Creating commit...")
    run_command(["git", "commit", "-m", commit_message], repo_root)
    print("Pushing to remote...")
    run_command(["git", "push"], repo_root)
    print("Push complete.")


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    assets_dir = (repo_root / args.assets_dir).resolve()

    if not assets_dir.exists() or not assets_dir.is_dir():
        raise NotADirectoryError(f"Assets directory does not exist: {assets_dir}")

    print(f"Watching {assets_dir} every {args.poll_seconds:.1f}s")
    previous_snapshot = snapshot_assets(assets_dir)

    while True:
        time.sleep(args.poll_seconds)
        current_snapshot = snapshot_assets(assets_dir)
        if current_snapshot == previous_snapshot:
            continue

        print("Detected asset change(s); waiting for writes to settle...")
        wait_for_settle(assets_dir, args.debounce_seconds)
        process_batch(repo_root, assets_dir, args.dry_run)
        previous_snapshot = snapshot_assets(assets_dir)

        if args.once:
            print("Processed one batch; exiting (--once).")
            return


if __name__ == "__main__":
    main()
