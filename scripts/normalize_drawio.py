#!/usr/bin/env python3
"""Normalize one .drawio file's line endings to LF.

The export writers hand `ET` a binary handle, so the files they produce are
always LF. A .drawio authored or edited on Windows can still arrive with CRLF,
and because `.gitattributes` declares `-text` those bytes would go into the
repository unchanged and make two copies of the same diagram differ in every
line.

This is the sanctioned channel for rewriting such a file. It changes nothing but
the line endings: the bytes are verified as strict UTF-8 and well-formed XML
before anything is written, and no other byte is touched.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


def normalize(raw: bytes) -> bytes:
    """Replace every CRLF in `raw` with LF, leaving the rest byte-identical."""
    normalized = raw.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise ValueError("contains a bare CR that is not part of a CRLF pair")
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether normalization is needed without writing",
    )
    args = parser.parse_args()

    try:
        raw = args.path.read_bytes()
    except OSError as exc:
        print(f"ERROR: {exc}")
        return 1

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"ERROR: {args.path} is not strict UTF-8: {exc}")
        return 1

    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        print(f"ERROR: {args.path} is not well-formed XML: {exc}")
        return 1

    try:
        normalized = normalize(raw)
    except ValueError as exc:
        print(f"ERROR: {args.path}: {exc}")
        return 1

    if normalized == raw:
        print(f"{args.path}: already LF.")
        return 0

    count = raw.count(b"\r\n")
    if args.check:
        print(f"ERROR: {args.path}: {count} CRLF line ending(s); normalize before committing.")
        return 1

    args.path.write_bytes(normalized)
    print(f"{args.path}: rewrote {count} CRLF line ending(s) as LF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
