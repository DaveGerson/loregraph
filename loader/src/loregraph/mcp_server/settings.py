"""Configuration for the LoreGraph MCP read server.

Everything here is plain data so it can be validated and tested without the
Neo4j driver or the MCP SDK installed. Secrets are never read from project
files: a connection names the environment variables that hold them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VISIBILITIES = ("PUBLIC", "PLAYER", "DM_ONLY", "PRIVATE")
REVIEW_STATUSES = ("ACCEPTED", "UNREVIEWED", "NEEDS_REVIEW", "REJECTED")
CANON_SCOPES = (
    "SETTING_SOURCE",
    "ADVENTURE_SOURCE",
    "CAMPAIGN",
    "HOMEBREW",
    "REFERENCE",
    "PERSONAL",
    "NONE",
)
ACTUALITIES = ("AUTHORED", "PLANNED", "OCCURRED", "HYPOTHETICAL", "SUPERSEDED")
TRUTH_STATUSES = ("ASSERTED", "DISPUTED", "UNKNOWN", "FALSE", "RETRACTED")

# Audiences are deny-first presets. The agent can narrow them per call but can
# never widen them; only whoever launches the server chooses the audience.
AUDIENCES: dict[str, tuple[str, ...]] = {
    "player": ("PUBLIC", "PLAYER"),
    "dm": ("PUBLIC", "PLAYER", "DM_ONLY"),
    "owner": VISIBILITIES,
}
DEFAULT_AUDIENCE = "player"
DEFAULT_REVIEW_STATUSES = ("ACCEPTED", "UNREVIEWED", "NEEDS_REVIEW")

URI_SCHEMES = {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
AURA_HOST_SUFFIX = ".databases.neo4j.io"
QUERY_EMBEDDING_MODES = {"auto", "none", "sentence_transformers", "openai"}


class SettingsError(ValueError):
    """Raised when the MCP server configuration is unusable."""


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """Server-side retrieval filter applied before every search and expansion.

    Empty ``canon_scopes`` or ``campaign_ids`` mean "no restriction". Records
    without a campaign ID (setting lore, rules, notes) are never excluded by the
    campaign allow-list; use ``canon_scopes`` to exclude them.
    """

    audience: str = DEFAULT_AUDIENCE
    visibilities: tuple[str, ...] = AUDIENCES[DEFAULT_AUDIENCE]
    review_statuses: tuple[str, ...] = DEFAULT_REVIEW_STATUSES
    canon_scopes: tuple[str, ...] = ()
    campaign_ids: tuple[str, ...] = ()

    @classmethod
    def for_audience(cls, audience: str, **overrides: Any) -> AccessPolicy:
        if audience not in AUDIENCES:
            raise SettingsError(
                f"Unknown audience {audience!r}; choose one of {', '.join(AUDIENCES)}"
            )
        allowed = AUDIENCES[audience]
        visibilities = tuple(overrides.pop("visibilities", None) or allowed)
        widened = set(visibilities) - set(allowed)
        if widened:
            raise SettingsError(
                f"visibilities {sorted(widened)} exceed the {audience!r} audience; "
                "choose a broader audience instead"
            )
        policy = cls(audience=audience, visibilities=visibilities)
        values = {key: tuple(value) for key, value in overrides.items() if value is not None}
        policy = replace(policy, **values)
        policy.validate()
        return policy

    def validate(self) -> None:
        _check_members("visibilities", self.visibilities, VISIBILITIES)
        _check_members("review_statuses", self.review_statuses, REVIEW_STATUSES)
        _check_members("canon_scopes", self.canon_scopes, CANON_SCOPES)
        if not self.visibilities:
            raise SettingsError("The access policy must allow at least one visibility")
        if not self.review_statuses:
            raise SettingsError("The access policy must allow at least one review status")

    @property
    def unrestricted(self) -> bool:
        """True when the policy cannot hide anything that raw Cypher would expose."""

        return (
            set(self.visibilities) == set(VISIBILITIES)
            and not self.canon_scopes
            and not self.campaign_ids
        )

    def narrow(
        self,
        *,
        visibilities: list[str] | None = None,
        review_statuses: list[str] | None = None,
        canon_scopes: list[str] | None = None,
        campaign_id: str | None = None,
    ) -> AccessPolicy:
        """Return a stricter copy for one call. Requests outside the policy fail."""

        policy = self
        if visibilities:
            _check_members("visibilities", visibilities, VISIBILITIES)
            denied = set(visibilities) - set(self.visibilities)
            if denied:
                raise SettingsError(
                    f"Visibility {sorted(denied)} is not permitted for the "
                    f"{self.audience!r} audience"
                )
            policy = replace(policy, visibilities=tuple(visibilities))
        if review_statuses:
            _check_members("review_statuses", review_statuses, REVIEW_STATUSES)
            denied = set(review_statuses) - set(self.review_statuses)
            if denied:
                raise SettingsError(f"Review status {sorted(denied)} is excluded by server policy")
            policy = replace(policy, review_statuses=tuple(review_statuses))
        if canon_scopes:
            _check_members("canon_scopes", canon_scopes, CANON_SCOPES)
            if self.canon_scopes:
                denied = set(canon_scopes) - set(self.canon_scopes)
                if denied:
                    raise SettingsError(
                        f"Canon scope {sorted(denied)} is excluded by server policy"
                    )
            policy = replace(policy, canon_scopes=tuple(canon_scopes))
        if campaign_id:
            if self.campaign_ids and campaign_id not in self.campaign_ids:
                raise SettingsError(f"Campaign {campaign_id!r} is not permitted by server policy")
            policy = replace(policy, campaign_ids=(campaign_id,))
        return policy

    def parameters(self) -> dict[str, Any]:
        return {
            "visibilities": list(self.visibilities),
            "reviewStatuses": list(self.review_statuses),
            "canonScopes": list(self.canon_scopes),
            "campaignIds": list(self.campaign_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "audience": self.audience,
            "visibilities": list(self.visibilities),
            "review_statuses": list(self.review_statuses),
            "canon_scopes": list(self.canon_scopes) or "all",
            "campaign_ids": list(self.campaign_ids) or "all",
            "entity_descriptions": "shown" if self.unrestricted else "redacted",
        }


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """One Neo4j deployment: a local server, an Aura instance, or anything else.

    Values ending in ``_env`` name environment variables so URIs, users, and
    secrets can stay out of the project file.
    """

    name: str
    uri: str = ""
    uri_env: str = ""
    database: str = ""
    database_env: str = ""
    auth: str = "basic"
    username: str = ""
    username_env: str = ""
    password_env: str = ""
    token_env: str = ""
    trusted_ca: str = ""
    connection_timeout_seconds: float = 15.0
    max_connection_pool_size: int = 5

    def resolved_uri(self) -> str:
        return (os.getenv(self.uri_env, "") if self.uri_env else "") or self.uri

    def resolved_database(self, fallback: str = "neo4j") -> str:
        value = os.getenv(self.database_env, "") if self.database_env else ""
        return value or self.database or fallback

    def resolved_username(self) -> str:
        value = os.getenv(self.username_env, "") if self.username_env else ""
        return value or self.username or "neo4j"

    def secret(self) -> str:
        variable = self.token_env if self.auth == "bearer" else self.password_env
        return os.getenv(variable, "") if variable else ""

    @property
    def secret_variable(self) -> str:
        return self.token_env if self.auth == "bearer" else self.password_env

    def validate(self) -> None:
        if self.auth not in {"basic", "bearer", "none"}:
            raise SettingsError(f"Connection {self.name!r}: auth must be basic, bearer, or none")
        if self.auth == "basic" and not self.password_env:
            raise SettingsError(f"Connection {self.name!r}: set password_env for basic auth")
        if self.auth == "bearer" and not self.token_env:
            raise SettingsError(f"Connection {self.name!r}: set token_env for bearer auth")
        uri = self.resolved_uri()
        if not uri:
            source = f"environment variable {self.uri_env}" if self.uri_env else "uri"
            raise SettingsError(f"Connection {self.name!r}: {source} is empty")
        parts = urlsplit(uri)
        if parts.scheme not in URI_SCHEMES:
            raise SettingsError(
                f"Connection {self.name!r}: unsupported URI scheme {parts.scheme!r}; "
                f"use one of {', '.join(sorted(URI_SCHEMES))}"
            )
        if parts.username or parts.password:
            raise SettingsError(
                f"Connection {self.name!r}: put credentials in environment variables, not the URI"
            )
        if self.kind == "aura" and parts.scheme not in {"neo4j+s", "bolt+s"}:
            raise SettingsError(
                f"Connection {self.name!r}: Aura requires an encrypted neo4j+s:// URI"
            )
        if self.trusted_ca:
            if "+" in parts.scheme:
                raise SettingsError(
                    f"Connection {self.name!r}: trusted_ca needs a plain neo4j:// or bolt:// "
                    "URI; the +s/+ssc schemes already choose the trust mode"
                )
            if not Path(self.trusted_ca).is_file():
                raise SettingsError(
                    f"Connection {self.name!r}: trusted_ca file {self.trusted_ca!r} not found"
                )

    @property
    def host(self) -> str:
        return urlsplit(self.resolved_uri()).hostname or ""

    @property
    def kind(self) -> str:
        host = self.host.casefold()
        if host.endswith(AURA_HOST_SUFFIX):
            return "aura"
        if host in LOCAL_HOSTS or host.endswith(".localhost"):
            return "local"
        return "remote"

    @property
    def encrypted(self) -> bool:
        return "+" in urlsplit(self.resolved_uri()).scheme or bool(self.trusted_ca)

    def warnings(self) -> list[str]:
        output = []
        if self.kind == "remote" and not self.encrypted:
            output.append(
                "Unencrypted connection to a non-local host; prefer neo4j+s:// or trusted_ca"
            )
        if "+ssc" in urlsplit(self.resolved_uri()).scheme:
            output.append("neo4j+ssc:// accepts any certificate; prefer trusted_ca for pinning")
        if self.auth != "none" and not self.secret():
            output.append(f"Environment variable {self.secret_variable} is not set")
        return output

    def describe(self, fallback_database: str = "neo4j") -> dict[str, Any]:
        """Safe summary: never includes secrets or URI credentials."""

        parts = urlsplit(self.resolved_uri())
        port = f":{parts.port}" if parts.port else ""
        return {
            "name": self.name,
            "kind": self.kind,
            "uri": f"{parts.scheme}://{parts.hostname or ''}{port}",
            "database": self.resolved_database(fallback_database),
            "auth": self.auth,
            "username": self.resolved_username() if self.auth == "basic" else None,
            "encrypted": self.encrypted,
            "warnings": self.warnings(),
        }


@dataclass(slots=True)
class EmbeddingSettings:
    """Query-time embedding settings mirrored from the loader's [embeddings]."""

    mode: str = "auto"
    provider: str = "none"
    model: str = ""
    dimensions: int = 0
    vector_index_name: str = "chunk_embedding"

    @property
    def effective_provider(self) -> str:
        if self.mode == "none":
            return "none"
        if self.mode == "auto":
            # Query text can be as sensitive as the corpus, so hosted query
            # embedding is never inferred; it must be selected explicitly.
            return "sentence_transformers" if self.provider == "sentence_transformers" else "none"
        return self.mode


@dataclass(slots=True)
class ServerSettings:
    connections: dict[str, ConnectionProfile]
    default_connection: str
    policy: AccessPolicy = field(default_factory=AccessPolicy)
    embeddings: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    fallback_database: str = "neo4j"
    max_rows: int = 200
    query_timeout_seconds: float = 20.0
    allow_raw_cypher: bool = False
    schema_path: Path | None = None
    entity_index: str = "entity_search"
    chunk_index: str = "chunk_fulltext"

    def validate(self) -> None:
        if not self.connections:
            raise SettingsError(
                "No Neo4j connection configured. Set NEO4J_URI and NEO4J_PASSWORD, or add "
                "[mcp.connections.<name>] to the project file."
            )
        if self.default_connection not in self.connections:
            raise SettingsError(
                f"default connection {self.default_connection!r} is not one of "
                f"{sorted(self.connections)}"
            )
        for profile in self.connections.values():
            profile.validate()
        self.policy.validate()
        if self.embeddings.mode not in QUERY_EMBEDDING_MODES:
            raise SettingsError(
                f"mcp.query_embeddings must be one of {', '.join(sorted(QUERY_EMBEDDING_MODES))}"
            )
        for name in (self.embeddings.vector_index_name, self.entity_index, self.chunk_index):
            if not name.replace("_", "").isalnum():
                raise SettingsError(f"Index name {name!r} must be alphanumeric/underscore")
        if not 1 <= self.max_rows <= 5000:
            raise SettingsError("mcp.max_rows must be between 1 and 5000")
        if not 0 < self.query_timeout_seconds <= 600:
            raise SettingsError("mcp.query_timeout_seconds must be between 0 and 600")
        if self.allow_raw_cypher and not self.policy.unrestricted:
            raise SettingsError(
                "allow_raw_cypher bypasses visibility, scope, and campaign filters, so it is "
                "only permitted with the unrestricted 'owner' audience"
            )

    def connection(self, name: str | None) -> ConnectionProfile:
        key = name or self.default_connection
        if key not in self.connections:
            raise SettingsError(
                f"Unknown connection {key!r}; configured: {', '.join(sorted(self.connections))}"
            )
        return self.connections[key]


def load_settings(
    config_path: str | Path | None = None,
    *,
    audience: str | None = None,
    connection: str | None = None,
    allow_raw_cypher: bool | None = None,
) -> ServerSettings:
    """Build settings from an optional project TOML plus environment variables.

    Precedence for the audience and default connection: explicit arguments
    (CLI flags), then ``LOREGRAPH_MCP_*`` environment variables, then ``[mcp]``
    in the project file. ``NEO4J_*`` variables define a connection only when the
    file declares none.
    """

    raw: dict[str, Any] = {}
    base = Path.cwd()
    if config_path is not None:
        path = Path(config_path).expanduser().resolve()
        if not path.is_file():
            raise SettingsError(f"Config file {path} not found")
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        base = path.parent
    mcp = raw.get("mcp", {})
    neo4j_section = raw.get("neo4j", {})
    embeddings_section = raw.get("embeddings", {})
    project = raw.get("project", {})

    fallback_database = str(neo4j_section.get("database", "neo4j"))
    connections: dict[str, ConnectionProfile] = {}
    for name, values in (mcp.get("connections") or {}).items():
        connections[name] = _connection(name, values, base)
    if not connections and os.getenv("NEO4J_URI"):
        # Same variables the loader's load-neo4j command already uses.
        connections["default"] = ConnectionProfile(
            name="default",
            uri_env="NEO4J_URI",
            username_env="NEO4J_USERNAME",
            password_env="NEO4J_PASSWORD",
            database_env="NEO4J_DATABASE",
        )
    default_connection = (
        connection
        or os.getenv("LOREGRAPH_MCP_CONNECTION")
        or mcp.get("default_connection")
        or (next(iter(connections)) if connections else "default")
    )

    policy_section = dict(mcp.get("policy") or {})
    configured_audience = policy_section.pop("audience", None)
    policy_audience = (
        audience or os.getenv("LOREGRAPH_MCP_AUDIENCE") or configured_audience or DEFAULT_AUDIENCE
    )
    unknown = set(policy_section) - {
        "visibilities",
        "review_statuses",
        "canon_scopes",
        "campaign_ids",
    }
    if unknown:
        raise SettingsError(f"Unknown [mcp.policy] keys: {sorted(unknown)}")
    policy = AccessPolicy.for_audience(policy_audience, **policy_section)

    schema_value = project.get("schema_path")
    schema_path = None
    if schema_value:
        candidate = Path(schema_value)
        schema_path = candidate if candidate.is_absolute() else (base / candidate).resolve()

    raw_cypher = allow_raw_cypher
    if raw_cypher is None:
        raw_cypher = bool(mcp.get("allow_raw_cypher", False))

    settings = ServerSettings(
        connections=connections,
        default_connection=str(default_connection),
        policy=policy,
        embeddings=EmbeddingSettings(
            mode=str(mcp.get("query_embeddings", "auto")),
            provider=str(embeddings_section.get("provider", "none")),
            model=str(embeddings_section.get("model", "")),
            dimensions=int(embeddings_section.get("dimensions", 0) or 0),
            vector_index_name=str(embeddings_section.get("vector_index_name", "chunk_embedding")),
        ),
        fallback_database=fallback_database,
        max_rows=int(mcp.get("max_rows", 200)),
        query_timeout_seconds=float(mcp.get("query_timeout_seconds", 20.0)),
        allow_raw_cypher=raw_cypher,
        schema_path=schema_path,
    )
    settings.validate()
    return settings


def load_env_file(path: str | Path, *, override: bool = False) -> list[str]:
    """Load KEY=VALUE lines so MCP client configs need not embed secrets.

    Returns the variable names that were set. Existing environment variables win
    unless ``override`` is true.
    """

    loaded = []
    for line_number, line in enumerate(
        Path(path).expanduser().read_text(encoding="utf-8").splitlines(), start=1
    ):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[len("export ") :].lstrip()
        key, separator, value = text.partition("=")
        key = key.strip()
        if not separator or not key.replace("_", "").isalnum():
            raise SettingsError(f"{path}:{line_number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


_CONNECTION_KEYS = {
    "uri",
    "uri_env",
    "database",
    "database_env",
    "auth",
    "username",
    "username_env",
    "password_env",
    "token_env",
    "trusted_ca",
    "connection_timeout_seconds",
    "max_connection_pool_size",
}


def _connection(name: str, values: dict[str, Any], base: Path) -> ConnectionProfile:
    if not isinstance(values, dict):
        raise SettingsError(f"[mcp.connections.{name}] must be a table")
    if "password" in values or "token" in values:
        raise SettingsError(
            f"[mcp.connections.{name}] must not contain secrets; use password_env or token_env"
        )
    unknown = set(values) - _CONNECTION_KEYS
    if unknown:
        raise SettingsError(f"[mcp.connections.{name}] has unknown keys: {sorted(unknown)}")
    data = dict(values)
    if data.get("trusted_ca"):
        candidate = Path(str(data["trusted_ca"])).expanduser()
        data["trusted_ca"] = str(
            candidate if candidate.is_absolute() else (base / candidate).resolve()
        )
    return ConnectionProfile(name=name, **data)


def _check_members(label: str, values: Any, allowed: tuple[str, ...]) -> None:
    unknown = set(values) - set(allowed)
    if unknown:
        raise SettingsError(f"Unknown {label}: {sorted(unknown)}; allowed: {', '.join(allowed)}")
