#!/usr/bin/env python3
"""Download a skill from the OpenSpace cloud platform.

Usage:
    openspace-download-skill --skill-id "weather__imp_abc12345" --output-dir ./skills/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openspace.cloud.auth import get_api_base, get_auth_headers_or_exit
from openspace.cloud.client import OpenSpaceClient, CloudError


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openspace-download-skill",
        description="Download a skill from OpenSpace's cloud community",
    )
    parser.add_argument("--skill-id", required=True, help="Cloud skill record ID")
    parser.add_argument("--output-dir", required=True, help="Target directory for extraction")
    parser.add_argument("--api-base", default=None, help="Override API base URL")
    parser.add_argument("--force", action="store_true", help="Overwrite existing skill directory")

    args = parser.parse_args()

    api_base = get_api_base(args.api_base)
    headers = get_auth_headers_or_exit()
    output_base = Path(args.output_dir).resolve()

    print(f"Fetching skill: {args.skill_id} ...", file=sys.stderr)

    try:
        client = OpenSpaceClient(headers, api_base)
        result = client.import_skill(args.skill_id, output_base)
    except CloudError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if result.get("status") == "already_exists" and not args.force:
        print(
            f"ERROR: Skill directory already exists: {result.get('local_path')}\n"
            f"  Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    files = result.get("files", [])
    local_path = result.get("local_path", "")
    print(f"  Extracted {len(files)} file(s) to {local_path}", file=sys.stderr)
    for f in files:
        print(f"    {f}", file=sys.stderr)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nSkill downloaded to: {local_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
