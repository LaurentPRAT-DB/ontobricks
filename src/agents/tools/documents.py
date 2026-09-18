"""
Document tools – used by the OWL generator agent.

Provides tools to list and read documents from a Unity Catalog volume.
"""

import json
from typing import Callable, Dict, List, Optional

import requests

from back.core.logging import get_logger
from back.core.databricks import DocumentParseService, VolumeFileService
from agents.tools.context import ToolContext
from shared.config.constants import HTTP_USER_AGENT

logger = get_logger(__name__)

_TOOL_TIMEOUT = 30
_MAX_DOC_CHARS = 80_000  # Increased to allow more context for mapping decisions

def _headers(ctx: ToolContext) -> dict:
    return {"Authorization": f"Bearer {ctx.token}", "User-Agent": HTTP_USER_AGENT}


def _volume_docs_path(ctx: ToolContext) -> Optional[str]:
    reg = ctx.registry
    if not reg or not reg.get("catalog") or not reg.get("volume"):
        logger.debug("_volume_docs_path: missing registry fields — reg=%s", reg)
        return None
    from back.objects.registry import RegistryCfg

    c = RegistryCfg.from_dict(reg)
    folder = ctx.domain_folder or ""
    if not folder:
        from back.objects.session.DomainSession import sanitize_domain_folder

        folder = sanitize_domain_folder(ctx.domain_name or "untitled_domain")
    version = ctx.domain_version or "1"
    from back.objects.registry.RegistryService import _DOMAINS_FOLDER

    path = f"/Volumes/{c.catalog}/{c.schema}/{c.volume}/{_DOMAINS_FOLDER}/{folder}/V{version}/documents"
    logger.debug("_volume_docs_path: resolved to %s", path)
    return path


def _parse_service(ctx: ToolContext) -> DocumentParseService:
    """Build the read-only parsed-corpus service for an agent context."""
    return DocumentParseService(VolumeFileService(host=ctx.host, token=ctx.token))


# =====================================================
# Tool implementations
# =====================================================


def tool_list_documents(ctx: ToolContext, **_kwargs) -> str:
    """List documents available in the domain UC volume."""
    logger.info("tool_list_documents: listing documents in domain volume")
    base_path = _volume_docs_path(ctx)
    if not base_path:
        logger.info("tool_list_documents: no UC location configured — returning error")
        return json.dumps({"error": "Domain not saved to Unity Catalog"})

    url = f"{ctx.host}/api/2.0/fs/directories{base_path}"
    logger.info("tool_list_documents: GET %s", base_path)
    logger.debug("tool_list_documents: full url=%s", url)
    try:
        resp = requests.get(url, headers=_headers(ctx), timeout=_TOOL_TIMEOUT)
        logger.debug(
            "tool_list_documents: response status=%d, size=%d bytes",
            resp.status_code,
            len(resp.content),
        )
        if resp.status_code == 404:
            logger.info("tool_list_documents: documents directory not found (404)")
            return json.dumps({"files": [], "message": "No documents directory yet"})
        resp.raise_for_status()
        entries = resp.json().get("contents", [])
        logger.debug("tool_list_documents: raw entries count=%d", len(entries))
        service = _parse_service(ctx)
        files = []
        for entry in entries:
            if entry.get("is_directory", False):
                continue
            name = entry.get("name", entry.get("path", "").split("/")[-1])
            if not name:
                continue
            item = {"name": name, "size": entry.get("file_size")}
            manifest = service.status(base_path, name)
            if manifest is not None:
                item["parse_status"] = manifest.status.value
                item["parser"] = manifest.parser
                if manifest.error:
                    item["parse_error"] = manifest.error
            else:
                extension = name.rpartition(".")[2].lower()
                if extension in DocumentParseService.TEXT_EXTENSIONS:
                    item["parse_status"] = "ready"
                    item["parser"] = "plaintext"
                else:
                    item["parse_status"] = "failed"
                    item["parse_error"] = (
                        "Document has not been parsed; retry parsing"
                    )
            files.append(item)
        logger.info("tool_list_documents: found %d file(s)", len(files))
        logger.debug("tool_list_documents: files=%s", [f["name"] for f in files])
        return json.dumps({"files": files, "count": len(files)})
    except Exception as exc:
        logger.error("tool_list_documents: request failed: %s", exc)
        return json.dumps({"error": str(exc)})


def tool_read_document(ctx: ToolContext, *, filename: str = "", **_kwargs) -> str:
    """Read one ready document from the durable parsed corpus."""
    logger.info("tool_read_document: reading '%s'", filename)
    if not filename:
        logger.warning("tool_read_document: called without filename parameter")
        return json.dumps({"error": "filename is required"})

    base_path = _volume_docs_path(ctx)
    if not base_path:
        logger.info("tool_read_document: no UC location configured — returning error")
        return json.dumps({"error": "Domain not saved to Unity Catalog"})

    try:
        payload = _parse_service(ctx).read_document(
            base_path,
            filename,
            max_chars=_MAX_DOC_CHARS,
        )
        return json.dumps(payload)
    except Exception as exc:
        logger.error("tool_read_document: unexpected error for '%s': %s", filename, exc)
        return json.dumps({"filename": filename, "error": str(exc)})


_MAX_DOCS_IN_CONTEXT = 10
_MAX_TOTAL_DOC_CHARS = 150_000


def tool_get_documents_context(ctx: ToolContext, **_kwargs) -> str:
    """Return pre-loaded document content from imported domain documents.
    Does NOT query Unity Catalog — uses documents loaded at agent start.
    Limited to avoid context overflow when many/large documents are loaded."""
    logger.info(
        "tool_get_documents_context: returning %d pre-loaded document(s)",
        len(ctx.documents),
    )
    if not ctx.documents:
        return json.dumps(
            {
                "documents": [],
                "message": "No documents were loaded. Upload documents in Domain → Documents to enrich mapping context.",
            }
        )
    result = []
    unavailable = []
    total_chars = 0
    for d in ctx.documents[:_MAX_DOCS_IN_CONTEXT]:
        status = d.get("parse_status", "ready")
        if status != "ready":
            unavailable.append(
                {
                    "name": d.get("name", "?"),
                    "parse_status": status,
                    "error": d.get("error") or "Document parsing is not ready",
                }
            )
            continue
        content = d.get("content", "")
        if total_chars + len(content) > _MAX_TOTAL_DOC_CHARS:
            remaining = _MAX_TOTAL_DOC_CHARS - total_chars
            if remaining > 5000:
                content = (
                    content[:remaining]
                    + f"\n\n[…truncated, document has {len(d.get('content', ''))} chars total]"
                )
                result.append(
                    {
                        "name": d.get("name", "?"),
                        "content": content,
                        "size": len(content),
                    }
                )
                total_chars = _MAX_TOTAL_DOC_CHARS
            break
        result.append(
            {"name": d.get("name", "?"), "content": content, "size": len(content)}
        )
        total_chars += len(content)
    truncated = len(ctx.documents) > len(result) or total_chars < sum(
        len(d.get("content", "")) for d in ctx.documents
    )
    out = {"documents": result, "count": len(result), "total_chars": total_chars}
    if unavailable:
        out["unavailable_documents"] = unavailable
    if truncated:
        out["_message"] = (
            f"Showing first {len(result)} document(s), {total_chars} chars total (limit to avoid context overflow)."
        )
    logger.info(
        "tool_get_documents_context: returning %d doc(s), %d total chars%s",
        len(result),
        total_chars,
        " (truncated)" if truncated else "",
    )
    return json.dumps(out)


# =====================================================
# OpenAI function-calling definitions
# =====================================================

GET_DOCUMENTS_CONTEXT_DEF = {
    "type": "function",
    "function": {
        "name": "get_documents_context",
        "description": (
            "Get the domain's imported documents (context loaded at agent start). "
            "Use this to enrich domain knowledge for mapping decisions. "
            "Does NOT query Unity Catalog."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

DOCUMENT_TOOL_DEFINITIONS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "List all documents in the domain's Unity Catalog volume. "
                "Call this first to discover available documents before reading them."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "Read a ready document from the domain's durable parsed corpus. "
                "Returns parse_status=pending or failed when text is unavailable. "
                "This tool never starts document parsing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File name to read, e.g. 'business_rules.txt'",
                    }
                },
                "required": ["filename"],
            },
        },
    },
]

DOCUMENT_TOOL_HANDLERS: Dict[str, Callable] = {
    "list_documents": tool_list_documents,
    "read_document": tool_read_document,
    "get_documents_context": tool_get_documents_context,
}
