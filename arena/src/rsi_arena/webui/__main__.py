"""Command-line entrypoint for the Arena run inspector."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .server import serve


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rsi-arena-webui")
    parser.add_argument(
        "--run-root",
        action="append",
        required=True,
        help="stored Arena generic-command run root; repeat to inspect several runs",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    httpd = serve(args.run_root, host=args.host, port=args.port)
    host, port = httpd.server_address[:2]
    print(f"Arena inspector on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
