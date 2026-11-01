#!/usr/bin/env python3
"""Upload a skill to the OpenSpace cloud platform.

Usage:
    openspace-upload-skill --skill-dir ./my-skill --visibility public --origin imported
    openspace-upload-skill --skill-dir ./my-skill --visibility private --origin fixed --parent-ids "parent_id"
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
        prog="openspace-upload-skill",
        description="Upload a skill to OpenSpace's cloud community",
    )
    parser.add_argument("--skill-dir", required=True, help="Path to skill directory (must contain SKILL.md)")
    parser.add_argument("--visibility", required=True, choices=["public", "private"])
    parser.add_argument("--origin", default="imported", choices=["imported", "captured", "derived", "fixed"])
    parser.add_argument("--parent-ids", default="", help="Comma-separated parent skill IDs")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--created-by", default="", help="Creator display name")
    parser.add_argument("--change-summary", default="", help="Change summary text")
    parser.add_argument("--api-base", default=None, help="Override API base URL")
    parser.add_argument("--dry-run", action="store_true", help="List files without uploading")

    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.is_dir():
        print(f"ERROR: Not a directory: {skill_dir}", file=sys.stderr)
        sys.exit(1)

    api_base = get_api_base(args.api_base)

    if args.dry_run:
        files = OpenSpaceClient._collect_files(skill_dir)
        print(f"Dry run — would upload {len(files)} file(s):", file=sys.stderr)
        for f in files:
            print(f"  {f.relative_to(skill_dir)}", file=sys.stderr)
        sys.exit(0)

    headers = get_auth_headers_or_exit()

    parent_ids = [p.strip() for p in args.parent_ids.split(",") if p.strip()]
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Upload Skill: {skill_dir.name}", file=sys.stderr)
    print(f"  Visibility:  {args.visibility}", file=sys.stderr)
    print(f"  Origin:      {args.origin}", file=sys.stderr)
    print(f"  API Base:    {api_base}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    try:
        client = OpenSpaceClient(headers, api_base)
        result = client.upload_skill(
            skill_dir,
            visibility=args.visibility,
            origin=args.origin,
            parent_skill_ids=parent_ids,
            tags=tags,
            created_by=args.created_by,
            change_summary=args.change_summary,
        )
    except CloudError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nUpload complete!", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
