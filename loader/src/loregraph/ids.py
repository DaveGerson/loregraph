from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_name(value: str) -> str:
    value = normalize_text(value).casefold()
    value = re.sub(r"\b(the|a|an)\b", " ", value)
    value = re.sub(r"[^\w']+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str, max_length: int = 72) -> str:
    value = normalize_text(value).casefold()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = value[:max_length].rstrip("-")
    return value or "unnamed"


def stable_id(namespace: str, kind: str, *parts: object, digest_chars: int = 12) -> str:
    canonical = "\x1f".join(normalize_text(str(part)) for part in parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:digest_chars]
    readable = slugify(str(parts[0])) if parts else kind
    return f"urn:loregraph:{namespace}:{kind}:{readable}-{digest}"


def short_id(value: str, length: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
