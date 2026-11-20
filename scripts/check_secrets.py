#!/usr/bin/env python3
"""Reject credentials in the Git index without printing their values."""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
KEY_PATTERN = re.compile(rb"(?<![A-Za-z0-9_-])db-[A-Za-z0-9_-]{29}(?![A-Za-z0-9_-])")


def contains_credential(content: bytes) -> bool:
    if KEY_PATTERN.search(content):
        return True
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(b"export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition(b"=")
        if separator and name.strip() == b"DATABENTO_API_KEY":
            value = value.split(b"#", 1)[0].strip().strip(b"\"'")
            if value:
                return True
    return False


def main() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    rejected = []
    for path_bytes in filter(None, paths):
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        name = Path(path).name
        if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
            rejected.append(path)
            continue
        content = subprocess.check_output(["git", "show", ":" + path], cwd=ROOT)
        if contains_credential(content):
            rejected.append(path)
    if rejected:
        print("Commit blocked: credentials or local environment files detected:", file=sys.stderr)
        for path in rejected:
            print("  " + path, file=sys.stderr)
        print("Keep real credentials in the ignored .env; leave .env.example values empty.", file=sys.stderr)
        return 1
    print("Credential check passed for all staged files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
