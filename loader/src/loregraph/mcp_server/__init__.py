"""Read-only MCP server exposing a LoreGraph Neo4j projection to agents.

Install with ``pip install -e '.[mcp]'`` and run ``loregraph-mcp``. Settings
and Cypher helpers import without the optional dependencies; the driver and
MCP SDK are loaded only by ``graph`` and ``server``.
"""

from __future__ import annotations

from importlib import metadata

from .. import __version__ as _package_version


def server_version() -> str:
    try:
        return metadata.version("loregraph-loader")
    except metadata.PackageNotFoundError:
        return _package_version
