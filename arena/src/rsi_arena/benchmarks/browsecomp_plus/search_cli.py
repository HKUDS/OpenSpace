"""Task-container CLI for one BrowseComp task-local search socket."""

from __future__ import annotations

import json
import socket
import sys

SEARCH_SOCKET = "/opt/rsi-arena/search/search.sock"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _request(query: str) -> object:
    payload = (
        json.dumps(
            {"method": "search", "query": query},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    response = bytearray()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(300.0)
    try:
        connection.connect(SEARCH_SOCKET)
        connection.sendall(payload)
        while True:
            chunk = connection.recv(
                min(64 * 1024, _MAX_RESPONSE_BYTES + 1 - len(response))
            )
            if not chunk:
                raise RuntimeError("search response ended early")
            response.extend(chunk)
            if len(response) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("search response exceeded its byte limit")
            newline = response.find(b"\n")
            if newline >= 0:
                if newline != len(response) - 1:
                    raise RuntimeError("search returned more than one response")
                break
    finally:
        connection.close()
    return json.loads(bytes(response[:-1]))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or not arguments[0].strip():
        print("usage: browsecomp-search QUERY", file=sys.stderr)
        return 2
    try:
        response = _request(arguments[0])
    except Exception:  # noqa: BLE001 - CLI exposes only a fixed safe error
        print(
            json.dumps(
                {"error": "task-local search failed", "ok": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    if (
        not isinstance(response, dict)
        or set(response) != {"ok", "results"}
        or response.get("ok") is not True
        or not isinstance(response.get("results"), list)
        or len(response["results"]) > 5
        or any(
            not isinstance(item, dict) or set(item) != {"docid", "snippet", "url"}
            for item in response["results"]
        )
    ):
        print(
            json.dumps(
                {"error": "task-local search failed", "ok": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {"ok": True, "results": response["results"]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
