"""Read-only Neo4j access shared by every MCP tool.

One driver is created lazily per named connection. Every statement runs in a
READ transaction with a server-side timeout and a row cap, so a mistaken or
malicious tool call cannot write, run forever, or flood the agent's context.
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, LiteralString, cast

try:
    import neo4j
    from neo4j import GraphDatabase, Query, RoutingControl, TrustCustomCAs
    from neo4j import exceptions as neo4j_errors
    from neo4j.graph import Node, Path, Relationship
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("Install the MCP extra: pip install -e '.[mcp]'") from exc

from . import server_version
from .cypher import VECTOR_PROPERTIES
from .settings import ConnectionProfile, ServerSettings

logger = logging.getLogger(__name__)


class GraphError(RuntimeError):
    """A Neo4j failure translated into a message that is safe to show an agent."""


@dataclass(slots=True)
class ReadResult:
    rows: list[dict[str, Any]]
    truncated: bool


class GraphGateway:
    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self._drivers: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def driver(self, connection: str | None = None) -> Any:
        profile = self.settings.connection(connection)
        with self._lock:
            if profile.name not in self._drivers:
                self._drivers[profile.name] = _create_driver(profile)
            return self._drivers[profile.name]

    def close(self) -> None:
        with self._lock:
            drivers, self._drivers = list(self._drivers.values()), {}
        for driver in drivers:
            try:
                driver.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.debug("Error closing Neo4j driver", exc_info=True)

    def database(self, connection: str | None = None) -> str:
        return self.settings.connection(connection).resolved_database(
            self.settings.fallback_database
        )

    # -- execution -----------------------------------------------------------

    def read(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        connection: str | None = None,
        max_rows: int | None = None,
        timeout: float | None = None,
    ) -> ReadResult:
        """Run one read-only statement and return JSON-safe rows."""

        cap = min(max_rows or self.settings.max_rows, self.settings.max_rows)

        def collect(result: Any) -> ReadResult:
            records = result.fetch(cap + 1)
            result.consume()
            rows = [
                {key: to_jsonable(value) for key, value in record.items()} for record in records
            ]
            return ReadResult(rows=rows[:cap], truncated=len(rows) > cap)

        return self._execute(query, parameters, connection, collect, timeout)

    def query_type(
        self, query: str, parameters: dict[str, Any] | None = None, *, connection: str | None = None
    ) -> str:
        """Plan (EXPLAIN) a statement without running it; returns r, w, rw, or s."""

        def summarize(result: Any) -> str:
            return result.consume().query_type or ""

        return self._execute("EXPLAIN " + query, parameters, connection, summarize, None)

    def server_info(self, connection: str | None = None) -> dict[str, Any]:
        def components(result: Any) -> dict[str, Any]:
            record = result.single()
            return (
                {"version": record["versions"][0], "edition": record["edition"]} if record else {}
            )

        info = self._execute(
            "CALL dbms.components() YIELD name, versions, edition "
            "WHERE name = 'Neo4j Kernel' RETURN versions, edition",
            None,
            connection,
            components,
            None,
        )
        info["agent"] = self.driver(connection).get_server_info().agent
        return info

    def _execute(
        self,
        query: str,
        parameters: dict[str, Any] | None,
        connection: str | None,
        transformer: Any,
        timeout: float | None,
    ) -> Any:
        profile = self.settings.connection(connection)
        seconds = timeout or self.settings.query_timeout_seconds
        try:
            return self.driver(profile.name).execute_query(
                # Statements are built from fixed fragments with parameters, or are
                # owner-supplied text that already passed check_raw_cypher.
                Query(cast(LiteralString, query), timeout=seconds),
                parameters or {},
                routing_=RoutingControl.READ,
                database_=self.database(profile.name),
                result_transformer_=transformer,
            )
        except Exception as exc:
            raise translate_error(exc, profile, seconds) from exc


def _create_driver(profile: ConnectionProfile) -> Any:
    options: dict[str, Any] = {
        "user_agent": f"loregraph-mcp/{server_version()}",
        "connection_timeout": profile.connection_timeout_seconds,
        "max_connection_pool_size": profile.max_connection_pool_size,
        # Planner notifications (deprecations, cartesian products) are noise for
        # agents and would otherwise be logged on every call.
        "notifications_min_severity": "OFF",
    }
    if profile.trusted_ca:
        options["encrypted"] = True
        options["trusted_certificates"] = TrustCustomCAs(profile.trusted_ca)
    if profile.auth == "basic":
        auth: Any = neo4j.basic_auth(profile.resolved_username(), profile.secret())
    elif profile.auth == "bearer":
        auth = neo4j.bearer_auth(profile.secret())
    else:
        auth = None
    return GraphDatabase.driver(profile.resolved_uri(), auth=auth, **options)


def translate_error(exc: Exception, profile: ConnectionProfile, timeout: float) -> GraphError:
    """Map driver exceptions to actionable messages without leaking secrets."""

    where = f"connection {profile.name!r} ({profile.describe()['uri']})"
    if isinstance(exc, neo4j_errors.AuthError):
        hint = f"check {profile.secret_variable}" if profile.secret_variable else "check auth"
        return GraphError(f"Neo4j rejected the credentials for {where}; {hint}.")
    if isinstance(exc, (neo4j_errors.ServiceUnavailable, neo4j_errors.SessionExpired)):
        hint = (
            " Aura instances that are paused must be resumed from the Aura console."
            if profile.kind == "aura"
            else " Is the database running and reachable from this machine?"
        )
        return GraphError(f"Cannot reach Neo4j at {where}.{hint}")
    if isinstance(exc, neo4j_errors.ConfigurationError):
        return GraphError(f"Invalid driver configuration for {where}: {exc}")
    code = getattr(exc, "code", "") or ""
    message = getattr(exc, "message", None) or str(exc)
    if "TransactionTimedOut" in code or "TransactionTimedOutClientConfiguration" in code:
        return GraphError(
            f"The query exceeded the {timeout:g}s timeout; narrow the request or lower the limit."
        )
    if code == "Neo.ClientError.Statement.AccessMode":
        return GraphError("The server only permits read-only queries.")
    if code == "Neo.ClientError.Procedure.ProcedureCallFailed" and "index" in message.casefold():
        return GraphError(
            f"A required index is missing or not online: {message}. Run `loregraph load-neo4j` "
            "to create the LoreGraph indexes."
        )
    if code.startswith("Neo.ClientError.Security"):
        return GraphError(f"The Neo4j user is not permitted to run this query: {message}")
    if code.startswith("Neo.ClientError"):
        return GraphError(f"Neo4j rejected the query ({code}): {message}")
    if isinstance(exc, neo4j_errors.Neo4jError):
        return GraphError(f"Neo4j error ({code or type(exc).__name__}): {message}")
    return GraphError(f"Unexpected error talking to {where}: {type(exc).__name__}: {exc}")


def to_jsonable(value: Any, *, depth: int = 0) -> Any:
    """Convert driver values into plain JSON types.

    Nodes become ``{"_labels": [...], ...properties}``; vector properties are
    dropped because thousands of floats help no agent.
    """

    if depth > 16:
        return "…"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Node):
        output: dict[str, Any] = {"_labels": sorted(value.labels)}
        output.update(_properties(value, depth))
        return output
    if isinstance(value, Relationship):
        output = {
            "_type": value.type,
            "_start": value.start_node.get("id") if value.start_node is not None else None,
            "_end": value.end_node.get("id") if value.end_node is not None else None,
        }
        output.update(_properties(value, depth))
        return output
    if isinstance(value, Path):
        return {
            "nodes": [to_jsonable(node, depth=depth + 1) for node in value.nodes],
            "relationships": [to_jsonable(rel, depth=depth + 1) for rel in value.relationships],
        }
    if isinstance(value, dict):
        return {str(key): to_jsonable(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item, depth=depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    iso = getattr(value, "iso_format", None)
    if callable(iso):  # neo4j.time.Date/DateTime/Time/Duration
        return iso()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    return str(value)


def _properties(entity: Any, depth: int) -> dict[str, Any]:
    return {
        key: to_jsonable(item, depth=depth + 1)
        for key, item in dict(entity).items()
        if key not in VECTOR_PROPERTIES
    }
