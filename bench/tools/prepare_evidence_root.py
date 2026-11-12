#!/usr/bin/env python3
"""Prepare (or just check) a PERSISTENT private folder for qualification evidence. It never deletes, moves or overwrites anything.

  python bench/tools/prepare_evidence_root.py --root "$BENCH_STATE_DIR/qualification-evidence"
  python bench/tools/prepare_evidence_root.py --root /root/bench-evidence-volume --check

Exit status: 0 persistent (created with mode 0700 when absent and not --check), 2 not persistent or unreadable mount table, 1 other error.
The folder is judged through the mount table (recipes/qualification_campaign/evidence.py): an overlay or tmpfs root is refused because a
container rebuild would discard the backups kept there."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "recipes"))
from qualification_campaign.evidence import check_persistent_root          # noqa: E402


def nearest_existing(path):
    path = Path(os.path.realpath(path))
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="absolute folder for qualification evidence (outside Git)")
    parser.add_argument("--check", action="store_true", help="only report; create nothing")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_absolute():
        print("error: --root must be an absolute path", file=sys.stderr)
        return 1
    ok, reason, detail = check_persistent_root(nearest_existing(root))
    print(("PERSISTENT: " if ok else "NOT PERSISTENT: ") + reason)
    if not ok:
        return 2
    if args.check:
        print(f"{root} {'exists' if root.is_dir() else 'does not exist yet'}; nothing created (--check)")
        return 0
    old = os.umask(0o077)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old)
    if root.is_symlink() or not root.is_dir():
        print("error: the evidence root is not a plain directory", file=sys.stderr)
        return 1
    mode = root.stat().st_mode & 0o777
    print(f"{root} ready (mode {mode:o})" + ("" if mode & 0o077 == 0 else " - WARNING: group/other access is open; tighten it yourself, nothing was changed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
