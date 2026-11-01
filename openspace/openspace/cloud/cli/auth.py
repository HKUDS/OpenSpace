#!/usr/bin/env python3
"""Run OpenSpace cloud auth and agent-key provisioning flows."""

from __future__ import annotations

import argparse
from dataclasses import replace
import getpass
import json
import sys

from openspace.cloud.auth_flow import cloud_auth_flow
from openspace.cloud.config import load_cloud_config, normalize_cloud_base_url
from openspace.cloud.redaction import redact_cloud_secret


def _password(value: str | None) -> str:
    return value if value is not None else getpass.getpass("Password: ")


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openspace-cloud-auth",
        description="Register users and provision OpenSpace cloud agent API keys.",
    )
    parser.add_argument("--cloud-base-url", default=None, help="OpenSpace cloud service root URL")
    parser.add_argument("--credentials-path", default=None, help="Local env file for OPENSPACE_CLOUD_*")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register-user", help="Register or reuse an owner user")
    register.add_argument("--email", required=True)
    register.add_argument("--password", default=None)
    register.add_argument("--name", default=None)

    login = sub.add_parser("login-user", help="Verify user credentials without printing bearer tokens")
    login.add_argument("--email", required=True)
    login.add_argument("--password", default=None)

    bootstrap = sub.add_parser(
        "bootstrap-agent-key",
        help="Create or recover an owner-scoped agent key and store it locally",
    )
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--password", default=None)
    bootstrap.add_argument("--name", default=None)
    bootstrap.add_argument("--agent-name", required=True)
    bootstrap.add_argument("--no-persist", action="store_true")

    list_agents = sub.add_parser("list-agents", help="List agents owned by a user")
    list_agents.add_argument("--email", required=True)
    list_agents.add_argument("--password", default=None)

    rotate = sub.add_parser("rotate-agent-key", help="Rotate an owned agent key and store it locally")
    rotate.add_argument("--email", required=True)
    rotate.add_argument("--password", default=None)
    rotate.add_argument("--agent-id", required=True)
    rotate.add_argument("--no-persist", action="store_true")

    sub.add_parser("verify-agent-key", help="Verify the current stored OPENSPACE_CLOUD_API_KEY")

    args = parser.parse_args()
    try:
        config = load_cloud_config()
        if args.cloud_base_url:
            config = replace(config, base_url=normalize_cloud_base_url(args.cloud_base_url))

        common = {
            "config": config,
            "credentials_path": args.credentials_path,
        }
        if args.command == "register-user":
            result = cloud_auth_flow(
                action="register_user",
                email=args.email,
                password=_password(args.password),
                name=args.name,
                **common,
            )
        elif args.command == "login-user":
            result = cloud_auth_flow(
                action="login_user",
                email=args.email,
                password=_password(args.password),
                **common,
            )
        elif args.command == "bootstrap-agent-key":
            result = cloud_auth_flow(
                action="bootstrap_agent_key",
                email=args.email,
                password=_password(args.password),
                name=args.name,
                agent_name=args.agent_name,
                persist=not args.no_persist,
                **common,
            )
        elif args.command == "list-agents":
            result = cloud_auth_flow(
                action="list_agents",
                email=args.email,
                password=_password(args.password),
                **common,
            )
        elif args.command == "rotate-agent-key":
            result = cloud_auth_flow(
                action="rotate_agent_key",
                email=args.email,
                password=_password(args.password),
                agent_id=args.agent_id,
                persist=not args.no_persist,
                **common,
            )
        elif args.command == "verify-agent-key":
            result = cloud_auth_flow(action="verify_agent_key", **common)
        else:
            parser.error("unknown command")
            return
        _print_json(result)
    except Exception as exc:
        print(f"ERROR: {redact_cloud_secret(str(exc))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
