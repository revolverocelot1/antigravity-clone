#!/usr/bin/env python3
"""Cross-platform installation script for antigravity-clone."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def get_target_dir() -> Path:
    home = Path.home()
    return home / ".gemini" / "config" / "plugins" / "antigravity-clone"


def main() -> int:
    target_dir = get_target_dir()
    src_dir = Path(__file__).resolve().parent

    print(f"Installing antigravity-clone to: {target_dir}")

    # If running from inside the destination, skip copy
    if src_dir.resolve() == target_dir.resolve():
        print("Running directly inside target plugin directory.")
    else:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            print("Existing target folder found; syncing files...")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)

        # Copy essential plugin files
        items_to_copy = [
            "clone_engine.py",
            "clone_pre.py",
            "wire_pb.py",
            "plugin.json",
            "hooks.json",
            "test_clone_plugin.py",
            "README.md",
            ".gitignore",
        ]
        for item in items_to_copy:
            s = src_dir / item
            d = target_dir / item
            if s.is_file():
                shutil.copy2(s, d)

        # Copy skills directory
        skills_src = src_dir / "skills"
        skills_dst = target_dir / "skills"
        if skills_src.is_dir():
            shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)

    print("Running verification test suite...")
    test_file = target_dir / "test_clone_plugin.py"
    if test_file.is_file():
        res = subprocess.run([sys.executable, "-u", str(test_file)], cwd=str(target_dir))
        if res.returncode != 0:
            print("Warning: verification tests did not pass completely.")
            return res.returncode
    else:
        print("test_clone_plugin.py not found, skipping self-test.")

    print("\nInstallation successful!")
    print("You can now use /clone or .clone directly in Antigravity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
