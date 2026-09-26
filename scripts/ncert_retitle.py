#!/usr/bin/env python3
"""Re-derive NCERT deck names (and optionally re-place decks) via the app API.

Metadata-only counterpart to a force retry: the app re-reads each chapter PDF's
own navigation block and writes the deck name from it ("Unit 2 · Ch 1 · Picture
Time"), without touching cards or review progress.

Usage:
    ./.venv/bin/python scripts/ncert_retitle.py \
        --base-url https://edu.selviz.in --api-key-file /root/.edu_viz_prod.key \
        --bulk 0d6e0cc8-c7b5-45b6-afa6-fd350a25e953 \
        --folder 5b5166d4-d314-45cd-8ef3-b2fa060ce8d4

Pass --folder to also move the bulk's decks into that folder (used to put back
decks a per-deck force retry detached, before that bug was fixed).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _load_key(args) -> str:
    if args.api_key:
        return args.api_key.strip()
    if args.api_key_file:
        with open(args.api_key_file) as handle:
            return handle.read().strip()
    raise SystemExit("pass --api-key or --api-key-file")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--bulk", required=True, help="bulk upload id")
    parser.add_argument("--folder", default=None, help="optional target folder id")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-file", default=None)
    args = parser.parse_args()

    url = f"{args.base_url.rstrip('/')}/api/v1/bulk-ai-upload/{args.bulk}/retitle"
    if args.folder:
        url += f"?folder_id={args.folder}"

    request = urllib.request.Request(
        url, method="POST", headers={"X-Api-Key": _load_key(args)}
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()[:400]}", file=sys.stderr)
        return 1

    for row in payload.get("updated", []):
        print(f"  {row['file']:<14} {row['old_name']!r} -> {row['new_name']!r}")
    for row in payload.get("skipped", []):
        print(f"  {row['file']:<14} skipped: {row['reason']}")
    print(
        f"updated={payload['updated_count']} skipped={payload['skipped_count']} "
        f"folder={payload['folder_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
