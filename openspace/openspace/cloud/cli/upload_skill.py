#!/usr/bin/env python3
"""Upload a skill to the OpenSpace cloud platform.

Usage:
    openspace-upload-skill --skill-dir ./my-skill --origin imported --package-id <uuid>
    openspace-upload-skill --skill-dir ./my-skill --origin imported --parent-package-id <uuid> --new-package-segment "Browser automation"
    openspace-upload-skill --skill-dir ./my-skill --visibility private --origin fix --parent-cloud-ids "<cloud_skill_uuid>"
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import sys
from pathlib import Path

from openspace.cloud.client import OpenSpaceClient
from openspace.cloud.config import load_cloud_config, normalize_cloud_base_url
from openspace.cloud.package_placement import PackagePlacementResolver
from openspace.cloud.upload_trust import (
    SkillUploadTrustError,
    require_trusted_skill_for_upload_db,
    resolve_upload_skill_store_db,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openspace-upload-skill",
        description="Upload a skill to OpenSpace's cloud community",
    )
    parser.add_argument("--skill-dir", required=True, help="Path to skill directory (must contain SKILL.md)")
    parser.add_argument("--visibility", default="private", choices=["public", "private"])
    parser.add_argument(
        "--origin",
        default="imported",
        choices=["imported", "captured", "capture", "derived", "derive", "fixed", "fix"],
    )
    parser.add_argument("--parent-cloud-ids", default="", help="Comma-separated parent cloud skill IDs")
    parser.add_argument("--cloud-base-url", default=None, help="Override OpenSpace cloud service root URL")
    parser.add_argument("--package-id", default=None, help="v2 existing package UUID upload target")
    parser.add_argument("--parent-package-id", default=None, help="v2 parent package UUID for creating/reusing a child")
    parser.add_argument("--new-package-segment", default=None, help="v2 child package segment to create/reuse")
    parser.add_argument("--snapshot-version", default=None, help="v2 package picker snapshot_version used")
    parser.add_argument("--owner-agent-id", default=None, help="v2 owner agent UUID override; normally omitted")
    parser.add_argument("--submitted-skill-id", default=None, help="v2 submitted skill id override")
    parser.add_argument("--content-diff-file", default=None, help="v2 content_diff text file override")
    parser.add_argument(
        "--skill-store-db-path",
        default=None,
        help=(
            "Local SkillStore DB used to verify trusted state; defaults to "
            "OPENSPACE_SKILL_STORE_DB_PATH or the nearest .openspace/openspace.db"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="List files without uploading")

    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.is_dir():
        print(f"ERROR: Not a directory: {skill_dir}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        files = OpenSpaceClient._collect_files(skill_dir)
        print(f"Dry run — would upload {len(files)} file(s):", file=sys.stderr)
        for f in files:
            print(f"  {f.relative_to(skill_dir)}", file=sys.stderr)
        sys.exit(0)

    skill_store_db = resolve_upload_skill_store_db(
        skill_dir,
        explicit_db_path=args.skill_store_db_path,
    )
    try:
        require_trusted_skill_for_upload_db(
            skill_dir,
            db_path=skill_store_db,
        )
    except SkillUploadTrustError as exc:
        print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        sys.exit(1)

    parent_cloud_ids = [p.strip() for p in args.parent_cloud_ids.split(",") if p.strip()]
    try:
        config = load_cloud_config()
        if args.cloud_base_url:
            config = replace(config, base_url=normalize_cloud_base_url(args.cloud_base_url))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Upload Skill: {skill_dir.name}", file=sys.stderr)
    print(f"  Visibility:  {args.visibility}", file=sys.stderr)
    print(f"  Origin:      {args.origin}", file=sys.stderr)
    print("  API Version: v2", file=sys.stderr)
    print(f"  Cloud URL:   {config.base_url}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    try:
        client = OpenSpaceClient(config)
        placement_kwargs = {
            "requested_package_id": args.package_id,
            "requested_parent_package_id": args.parent_package_id,
            "requested_new_package_segment": args.new_package_segment,
            "snapshot_version_used": args.snapshot_version,
        }
        if args.package_id or args.parent_package_id or args.new_package_segment:
            placement = PackagePlacementResolver(client).validate_confirmed_placement(
                requested_package_id=args.package_id,
                requested_parent_package_id=args.parent_package_id,
                requested_new_package_segment=args.new_package_segment,
                snapshot_version_used=args.snapshot_version,
            )
            placement_kwargs = placement.to_upload_kwargs()
        content_diff = None
        if args.content_diff_file:
            content_diff = Path(args.content_diff_file).read_text(encoding="utf-8")
        result = client.upload_skill_v2(
            skill_dir,
            local_skill_store_db_path=skill_store_db,
            visibility=args.visibility,
            origin=args.origin,
            parent_cloud_skill_ids=parent_cloud_ids,
            **placement_kwargs,
            owner_agent_id=args.owner_agent_id,
            submitted_skill_id=args.submitted_skill_id,
            content_diff=content_diff,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nUpload complete!", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
