"""Durable parsed-document corpus stored beside domain source documents."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from back.core.logging import get_logger
from back.core.databricks.DocumentExtractor import DocumentExtractor

logger = get_logger(__name__)


class ParseStatus(str, Enum):
    """Durable document parse states."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class ParseManifest:
    """Versioned sidecar metadata for one source document."""

    schema_version: int
    filename: str
    source_hash: str
    parser: str
    output_schema: Optional[str]
    status: ParseStatus
    parsed_at: Optional[str]
    error: Optional[str]
    sidecar_path: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-safe manifest representation."""
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ParseManifest":
        """Build a manifest from persisted JSON."""
        return cls(
            schema_version=int(payload["schema_version"]),
            filename=str(payload["filename"]),
            source_hash=str(payload["source_hash"]),
            parser=str(payload["parser"]),
            output_schema=payload.get("output_schema"),
            status=ParseStatus(payload["status"]),
            parsed_at=payload.get("parsed_at"),
            error=payload.get("error"),
            sidecar_path=payload.get("sidecar_path"),
        )


@dataclass(frozen=True)
class ParseSubmission:
    """Result of preparing an upload or retry for parsing."""

    filename: str
    uploaded: bool
    no_op: bool
    should_parse: bool
    parse_status: ParseStatus


class DocumentParseService:
    """Manage source hashes, parse manifests, and ready document text."""

    MANIFEST_SCHEMA_VERSION = 1
    PARSED_DIRECTORY = "_parsed"
    TEXT_EXTENSIONS = frozenset(
        {
            "txt",
            "md",
            "json",
            "csv",
            "xml",
            "ttl",
            "owl",
            "rdf",
            "yaml",
            "yml",
            "toml",
            "ini",
            "cfg",
            "log",
            "sql",
            "py",
            "js",
            "ts",
            "html",
            "css",
        }
    )

    _state_lock = threading.Lock()
    _path_locks: Dict[str, threading.Lock] = {}
    _active_parses: Set[str] = set()

    def __init__(self, volume_service: Any, extractor: Any = None) -> None:
        self._volume = volume_service
        self._extractor = extractor

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _source_hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @classmethod
    def _validate_filename(cls, filename: str) -> str:
        candidate = (filename or "").strip()
        if (
            not candidate
            or candidate in {".", ".."}
            or candidate != os.path.basename(candidate)
            or "/" in candidate
            or "\\" in candidate
        ):
            raise ValueError("Invalid filename")
        return candidate

    @classmethod
    def _lock_for(cls, source_path: str) -> threading.Lock:
        with cls._state_lock:
            return cls._path_locks.setdefault(source_path, threading.Lock())

    @classmethod
    def _set_active(cls, source_path: str, active: bool) -> None:
        with cls._state_lock:
            if active:
                cls._active_parses.add(source_path)
            else:
                cls._active_parses.discard(source_path)

    @classmethod
    def _is_active(cls, source_path: str) -> bool:
        with cls._state_lock:
            return source_path in cls._active_parses

    @classmethod
    def _parsed_directory(cls, base_path: str) -> str:
        return f"{base_path.rstrip('/')}/{cls.PARSED_DIRECTORY}"

    @classmethod
    def _manifest_path(cls, base_path: str, filename: str) -> str:
        return f"{cls._parsed_directory(base_path)}/{filename}.json"

    @classmethod
    def _markdown_path(cls, base_path: str, filename: str) -> str:
        return f"{cls._parsed_directory(base_path)}/{filename}.md"

    @staticmethod
    def _source_path(base_path: str, filename: str) -> str:
        return f"{base_path.rstrip('/')}/{filename}"

    def _ensure_parsed_directory(self, base_path: str) -> None:
        ok, message = self._volume.create_directory(self._parsed_directory(base_path))
        if not ok:
            raise RuntimeError(message)

    def _write_manifest(self, base_path: str, manifest: ParseManifest) -> None:
        self._ensure_parsed_directory(base_path)
        content = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True)
        ok, message = self._volume.write_file(
            self._manifest_path(base_path, manifest.filename),
            content,
            overwrite=True,
        )
        if not ok:
            raise RuntimeError(message)

    def status(self, base_path: str, filename: str) -> Optional[ParseManifest]:
        """Return the persisted manifest, or ``None`` for a legacy source."""
        filename = self._validate_filename(filename)
        ok, content, _message = self._volume.read_file(
            self._manifest_path(base_path, filename)
        )
        if not ok or not content:
            return None
        try:
            return ParseManifest.from_dict(json.loads(content))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Invalid parse manifest for %s: %s", filename, exc)
            return None

    def _manifest(
        self,
        *,
        filename: str,
        source_hash: str,
        parser: str,
        status: ParseStatus,
        error: Optional[str] = None,
    ) -> ParseManifest:
        is_binary = parser == "ai_parse_document"
        return ParseManifest(
            schema_version=self.MANIFEST_SCHEMA_VERSION,
            filename=filename,
            source_hash=source_hash,
            parser=parser,
            output_schema=(
                DocumentExtractor.OUTPUT_SCHEMA_VERSION if is_binary else None
            ),
            status=status,
            parsed_at=self._now_iso() if status is not ParseStatus.PENDING else None,
            error=error,
            sidecar_path=(
                f"{self.PARSED_DIRECTORY}/{filename}.md" if is_binary else None
            ),
        )

    def prepare_upload(
        self, base_path: str, filename: str, content: bytes
    ) -> ParseSubmission:
        """Persist an upload and create its initial durable parse state."""
        filename = self._validate_filename(filename)
        source_path = self._source_path(base_path, filename)
        source_hash = self._source_hash(content)
        extension = DocumentExtractor.file_extension(filename)

        with self._lock_for(source_path):
            existing = self.status(base_path, filename)
            if (
                existing
                and existing.source_hash == source_hash
                and existing.status is ParseStatus.READY
            ):
                return ParseSubmission(
                    filename, False, True, False, ParseStatus.READY
                )
            if (
                existing
                and existing.source_hash == source_hash
                and existing.status is ParseStatus.PENDING
                and self._is_active(source_path)
            ):
                return ParseSubmission(
                    filename, False, True, False, ParseStatus.PENDING
                )

            ok, message = self._volume.write_binary_file(
                source_path, content, overwrite=True
            )
            if not ok:
                raise RuntimeError(message)

            if extension in self.TEXT_EXTENSIONS:
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    manifest = self._manifest(
                        filename=filename,
                        source_hash=source_hash,
                        parser="plaintext",
                        status=ParseStatus.FAILED,
                        error="Document is not valid UTF-8",
                    )
                    self._write_manifest(base_path, manifest)
                    return ParseSubmission(
                        filename, True, False, False, ParseStatus.FAILED
                    )
                manifest = self._manifest(
                    filename=filename,
                    source_hash=source_hash,
                    parser="plaintext",
                    status=ParseStatus.READY,
                )
                self._write_manifest(base_path, manifest)
                return ParseSubmission(
                    filename, True, False, False, ParseStatus.READY
                )

            if not DocumentExtractor.supports(extension):
                manifest = self._manifest(
                    filename=filename,
                    source_hash=source_hash,
                    parser="unsupported",
                    status=ParseStatus.FAILED,
                    error="Unsupported document type",
                )
                self._write_manifest(base_path, manifest)
                return ParseSubmission(
                    filename, True, False, False, ParseStatus.FAILED
                )

            manifest = self._manifest(
                filename=filename,
                source_hash=source_hash,
                parser="ai_parse_document",
                status=ParseStatus.PENDING,
            )
            self._write_manifest(base_path, manifest)
            self._set_active(source_path, True)
            return ParseSubmission(
                filename, True, False, True, ParseStatus.PENDING
            )

    def parse_pending(self, base_path: str, filename: str) -> ParseManifest:
        """Extract one pending binary and persist its markdown sidecar."""
        filename = self._validate_filename(filename)
        source_path = self._source_path(base_path, filename)
        manifest = self.status(base_path, filename)
        if manifest is None:
            raise ValueError("Document has no parse manifest")
        if manifest.status is not ParseStatus.PENDING:
            return manifest

        try:
            parsed = (
                self._extractor.extract(source_path)
                if self._extractor is not None
                else None
            )
            if not parsed:
                failed = self._manifest(
                    filename=filename,
                    source_hash=manifest.source_hash,
                    parser="ai_parse_document",
                    status=ParseStatus.FAILED,
                    error="Document parsing failed",
                )
                self._delete_if_present(self._markdown_path(base_path, filename))
                self._write_manifest(base_path, failed)
                return failed

            ok, _message = self._volume.write_file(
                self._markdown_path(base_path, filename), parsed, overwrite=True
            )
            if not ok:
                failed = self._manifest(
                    filename=filename,
                    source_hash=manifest.source_hash,
                    parser="ai_parse_document",
                    status=ParseStatus.FAILED,
                    error="Parsed document could not be persisted",
                )
                self._delete_if_present(self._markdown_path(base_path, filename))
                self._write_manifest(base_path, failed)
                return failed

            ready = self._manifest(
                filename=filename,
                source_hash=manifest.source_hash,
                parser="ai_parse_document",
                status=ParseStatus.READY,
            )
            self._write_manifest(base_path, ready)
            return ready
        finally:
            self._set_active(source_path, False)

    def retry(self, base_path: str, filename: str) -> ParseSubmission:
        """Create a pending manifest for a failed or stale binary source."""
        filename = self._validate_filename(filename)
        extension = DocumentExtractor.file_extension(filename)
        if not DocumentExtractor.supports(extension):
            raise ValueError("Only supported binary documents can be retried")
        source_path = self._source_path(base_path, filename)

        with self._lock_for(source_path):
            manifest = self.status(base_path, filename)
            if manifest and manifest.status is ParseStatus.READY:
                raise ValueError("Document is already parsed")
            if (
                manifest
                and manifest.status is ParseStatus.PENDING
                and self._is_active(source_path)
            ):
                return ParseSubmission(
                    filename, False, True, False, ParseStatus.PENDING
                )
            ok, content, message = self._volume.read_binary_file(source_path)
            if not ok:
                raise ValueError(message)
            pending = self._manifest(
                filename=filename,
                source_hash=self._source_hash(content),
                parser="ai_parse_document",
                status=ParseStatus.PENDING,
            )
            self._write_manifest(base_path, pending)
            self._set_active(source_path, True)
            return ParseSubmission(
                filename, False, False, True, ParseStatus.PENDING
            )

    def read_document(
        self, base_path: str, filename: str, max_chars: Optional[int] = None
    ) -> Dict[str, Any]:
        """Read ready corpus text without ever invoking the extractor."""
        filename = self._validate_filename(filename)
        extension = DocumentExtractor.file_extension(filename)
        manifest = self.status(base_path, filename)

        if manifest is None:
            if extension not in self.TEXT_EXTENSIONS:
                return {
                    "filename": filename,
                    "parse_status": ParseStatus.FAILED.value,
                    "error": "Document has not been parsed; retry parsing",
                }
            parser = "plaintext"
            read_path = self._source_path(base_path, filename)
        elif manifest.status is not ParseStatus.READY:
            return {
                "filename": filename,
                "parse_status": manifest.status.value,
                "error": manifest.error or "Document parsing is not ready",
            }
        else:
            parser = manifest.parser
            read_path = (
                self._markdown_path(base_path, filename)
                if parser == "ai_parse_document"
                else self._source_path(base_path, filename)
            )

        ok, content, message = self._volume.read_file(read_path)
        if not ok:
            return {
                "filename": filename,
                "parse_status": ParseStatus.FAILED.value,
                "error": message,
            }
        original_size = len(content)
        truncated = bool(max_chars is not None and original_size > max_chars)
        if truncated:
            content = (
                content[:max_chars]
                + f"\n\n[…truncated, {original_size} total chars]"
            )
        return {
            "filename": filename,
            "content": content,
            "size": original_size,
            "truncated": truncated,
            "parsed_with": parser,
            "parse_status": ParseStatus.READY.value,
        }

    def _delete_if_present(self, path: str) -> Optional[str]:
        ok, message = self._volume.delete_file(path)
        if ok or "not found" in message.lower():
            return None
        return message

    def delete_artifacts(self, base_path: str, filename: str) -> List[str]:
        """Delete markdown and manifest sidecars; ignore absent artifacts."""
        filename = self._validate_filename(filename)
        errors = []
        for path in (
            self._markdown_path(base_path, filename),
            self._manifest_path(base_path, filename),
        ):
            error = self._delete_if_present(path)
            if error:
                errors.append(error)
        self._set_active(self._source_path(base_path, filename), False)
        return errors
