"""Export a canonical run view: ``python -m agentpilot.webui ...``."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from agentpilot.persistence import SQLiteReadOnlyUnitOfWorkFactory
from agentpilot.webui import RunViewAdapter


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentpilot-webui")
    parser.add_argument(
        "--database",
        action="append",
        help="run database; repeat to serve several policies from one picker",
    )
    parser.add_argument("--run-id")
    parser.add_argument('--research-root', help='Monitor a research experiment directory, including future runs')
    parser.add_argument(
        "--detail",
        action="store_true",
        help="include the entity-level projection the inspector renders",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve the read-only inspector on the loopback interface",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="checkout the candidates committed into; repeat once per database",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help=(
            "run config for display-only design and worker metadata; repeat "
            "once per database"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.research_root:
        if args.database or not args.serve:
            parser.error('--research-root requires --serve and cannot be mixed with --database')
        from agentpilot.webui.research import serve
        server = serve(args.research_root, args.host, args.port)
        print(f'research monitor on http://{args.host}:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if not args.database:
        parser.error('--database or --research-root is required')

    if args.serve:
        from agentpilot.webui.server import serve

        httpd = serve(
            *args.database,
            repositories=args.repo,
            config_paths=args.config,
            host=args.host,
            port=args.port,
        )
        host, port = httpd.server_address[:2]
        print(f"inspector on http://{host}:{port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0

    if not args.run_id:
        parser.error("--run-id is required unless --serve is used")
    if len(args.database) > 1:
        parser.error("--database may only be repeated with --serve")
    adapter = RunViewAdapter(SQLiteReadOnlyUnitOfWorkFactory(args.database[0]))
    view = adapter.build_detail(args.run_id) if args.detail else adapter.build(
        args.run_id
    )
    print(json.dumps(view, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
