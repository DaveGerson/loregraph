"""Command-line entry point: ``loregraph-mcp``.

In stdio mode stdout carries the MCP protocol, so diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys

from . import server_version
from .settings import AUDIENCES, SettingsError, load_env_file, load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loregraph-mcp",
        description=(
            "Read-only MCP server that lets agents query a LoreGraph Neo4j projection "
            "(local Neo4j, Aura, or any other Bolt endpoint) with citations."
        ),
    )
    parser.add_argument(
        "--config",
        default=os.getenv("LOREGRAPH_CONFIG"),
        help="Project TOML with optional [mcp] settings (default: $LOREGRAPH_CONFIG; "
        "without one, NEO4J_* environment variables define the connection)",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="KEY=VALUE file to load before reading settings (repeatable; existing "
        "environment variables win)",
    )
    parser.add_argument("--connection", help="Default connection name for tool calls")
    parser.add_argument(
        "--audience",
        choices=sorted(AUDIENCES),
        help="Who the answers are for: player (PUBLIC+PLAYER), dm (+DM_ONLY), owner (+PRIVATE). "
        "Default: $LOREGRAPH_MCP_AUDIENCE, then [mcp.policy].audience, then player",
    )
    parser.add_argument(
        "--allow-raw-cypher",
        action="store_true",
        help="Expose the read_cypher tool (owner audience only)",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument("--path", default="/mcp", help="HTTP endpoint path")
    parser.add_argument(
        "--allow-remote-clients",
        action="store_true",
        help="Permit binding HTTP to a non-loopback address (the endpoint has no authentication)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print connection/index status as JSON and exit (1 if Neo4j is unreachable)",
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {server_version()}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        for env_file in args.env_file:
            load_env_file(env_file)
        settings = load_settings(
            args.config,
            audience=args.audience,
            connection=args.connection,
            allow_raw_cypher=True if args.allow_raw_cypher else None,
        )
    except (SettingsError, OSError) as exc:
        print(f"loregraph-mcp: configuration error: {exc}", file=sys.stderr)
        return 2

    if (
        args.transport == "streamable-http"
        and not _is_loopback(args.host)
        and not args.allow_remote_clients
    ):
        print(
            f"loregraph-mcp: refusing to bind {args.host}; the HTTP endpoint has no authentication. "
            "Use 127.0.0.1, or pass --allow-remote-clients behind your own authenticating proxy.",
            file=sys.stderr,
        )
        return 2

    try:
        from .embedder import build_embedder
        from .graph import GraphGateway
        from .retrieval import LoreGraphReader
    except ImportError as exc:
        print(f"loregraph-mcp: {exc}", file=sys.stderr)
        return 2

    gateway = GraphGateway(settings)
    reader = LoreGraphReader(
        settings, gateway, build_embedder(settings.embeddings), server_version=server_version()
    )
    if args.check:
        try:
            report = reader.status()
        finally:
            gateway.close()
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("neo4j", {}).get("reachable") else 1

    try:
        from .server import build_server
    except ImportError as exc:
        print(f"loregraph-mcp: {exc}", file=sys.stderr)
        return 2
    server = build_server(settings, reader, log_level=args.log_level)
    logging.getLogger(__name__).info(
        "Serving connection %r for audience %r over %s",
        settings.default_connection,
        settings.policy.audience,
        args.transport,
    )
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(
            "streamable-http", host=args.host, port=args.port, streamable_http_path=args.path
        )
    return 0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
