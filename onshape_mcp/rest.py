"""Generic access to the whole Onshape REST API.

The hand-written tools in server.py cover a useful but small slice of Onshape:
45 tools against an API that publishes 302 operations. Rather than emit 302
tool schemas, which would cost more context than the work being done, this
module exposes the API through discovery plus a generic invoker:

  onshape_api_groups     what subject areas exist
  onshape_api_search     find operations by keyword or group
  onshape_api_describe   full parameter and body detail for one operation
  onshape_api_call       invoke any operation by operationId

Two file-transfer operations get dedicated tools because they need multipart
and binary transport that a JSON invoker cannot express:

  onshape_import_file    upload a STEP/IGES/etc file and translate it in
  onshape_export_download  poll an export translation and write the result

The OpenAPI document ships alongside this module and is refreshable from
https://cad.onshape.com/api/openapi, which needs no authentication.
"""

import json
import os
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple

from mcp.types import Tool

# The spec declares a single server, https://cad.onshape.com/api/v17. Spec
# paths are version-free, so the prefix is applied when building a request.
DEFAULT_API_PREFIX = "/api/v17"

_SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openapi.json")

_spec_cache: Optional[Dict[str, Any]] = None
_index_cache: Optional[Dict[str, Dict[str, Any]]] = None

HTTP_METHODS = ("get", "post", "delete", "put", "patch")


def load_spec() -> Dict[str, Any]:
    """Load and memoise the bundled OpenAPI document."""
    global _spec_cache
    if _spec_cache is None:
        with open(_SPEC_PATH, "r") as fh:
            _spec_cache = json.load(fh)
    return _spec_cache


def _operation_index() -> Dict[str, Dict[str, Any]]:
    """Map operationId to its path, method, and operation object."""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    spec = load_spec()
    index: Dict[str, Dict[str, Any]] = {}
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method not in HTTP_METHODS:
                continue
            op_id = op.get("operationId")
            if not op_id:
                continue
            index[op_id] = {"path": path, "method": method, "op": op}
    _index_cache = index
    return index


def _resolve_ref(ref: str) -> Dict[str, Any]:
    """Resolve a local $ref such as #/components/schemas/BTFoo."""
    node: Any = load_spec()
    for part in ref.lstrip("#/").split("/"):
        node = node.get(part, {})
        if not isinstance(node, dict):
            return {}
    return node


def _schema_summary(schema: Dict[str, Any], depth: int = 0) -> Any:
    """Reduce a JSON schema to property names and types.

    Onshape's component schemas are large and deeply cross-referenced;
    returning them verbatim would defeat the point of this module.
    """
    if not schema:
        return None
    if "$ref" in schema:
        if depth >= 1:
            return schema["$ref"].split("/")[-1]
        return _schema_summary(_resolve_ref(schema["$ref"]), depth + 1)

    kind = schema.get("type")
    if kind == "array":
        return [_schema_summary(schema.get("items", {}), depth + 1)]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        out = {}
        for name, sub in list(props.items())[:60]:
            if "$ref" in sub:
                out[name] = sub["$ref"].split("/")[-1]
            elif sub.get("type") == "array":
                out[name] = f"array<{sub.get('items', {}).get('type', 'object')}>"
            else:
                out[name] = sub.get("type", "object")
        return out
    return kind or "object"


def _request_body_info(op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    body = op.get("requestBody")
    if not body:
        return None
    for content_type, media in body.get("content", {}).items():
        return {
            "contentType": content_type,
            "required": body.get("required", False),
            "schema": _schema_summary(media.get("schema", {})),
        }
    return None


def list_groups() -> List[Tuple[str, int]]:
    """Count operations per OpenAPI tag."""
    counts: Dict[str, int] = {}
    for entry in _operation_index().values():
        for tag in entry["op"].get("tags", ["(untagged)"]):
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def search_operations(
    query: str = "", group: str = "", limit: int = 40
) -> List[Dict[str, str]]:
    """Find operations whose id, path, summary, or tag matches the query."""
    terms = [t for t in query.lower().split() if t]
    results: List[Dict[str, str]] = []

    for op_id, entry in sorted(_operation_index().items()):
        op = entry["op"]
        tags = op.get("tags", [])
        if group and not any(group.lower() == t.lower() for t in tags):
            continue

        haystack = " ".join(
            [op_id, entry["path"], op.get("summary", ""), " ".join(tags)]
        ).lower()
        if terms and not all(t in haystack for t in terms):
            continue

        results.append(
            {
                "operationId": op_id,
                "method": entry["method"].upper(),
                "path": entry["path"],
                "group": tags[0] if tags else "",
                "summary": (op.get("summary") or "").strip(),
            }
        )
        if len(results) >= limit:
            break
    return results


def describe_operation(operation_id: str) -> Dict[str, Any]:
    """Return the full callable contract for one operation."""
    entry = _operation_index().get(operation_id)
    if entry is None:
        raise KeyError(operation_id)

    op = entry["op"]
    params: Dict[str, List[Dict[str, Any]]] = {"path": [], "query": [], "header": []}
    for param in op.get("parameters", []):
        location = param.get("in", "query")
        params.setdefault(location, []).append(
            {
                "name": param.get("name"),
                "required": param.get("required", False),
                "type": param.get("schema", {}).get("type", "string"),
                "description": (param.get("description") or "").strip()[:200],
            }
        )

    return {
        "operationId": operation_id,
        "method": entry["method"].upper(),
        "path": entry["path"],
        "url": DEFAULT_API_PREFIX + entry["path"],
        "group": (op.get("tags") or [""])[0],
        "summary": (op.get("summary") or "").strip(),
        "description": (op.get("description") or "").strip()[:800],
        "parameters": {k: v for k, v in params.items() if v},
        "requestBody": _request_body_info(op),
        "responses": sorted(op.get("responses", {}).keys()),
    }


def build_path(
    operation_id: str, path_params: Dict[str, Any], api_prefix: str = DEFAULT_API_PREFIX
) -> Tuple[str, str]:
    """Substitute path parameters and return (method, concrete path)."""
    entry = _operation_index().get(operation_id)
    if entry is None:
        raise KeyError(operation_id)

    path = entry["path"]
    missing = []
    for param in entry["op"].get("parameters", []):
        if param.get("in") != "path":
            continue
        name = param["name"]
        if name not in path_params:
            if param.get("required", False):
                missing.append(name)
            continue
        # Percent-encode: Onshape part ids legitimately contain "/" (e.g. "J/D").
        # Substituted raw, such an id splits into extra path segments, which
        # routes the request elsewhere and silently returns 204 without
        # applying the write.
        path = path.replace(
            "{" + name + "}", quote(str(path_params[name]), safe="")
        )

    if missing:
        raise ValueError(
            f"{operation_id} requires path parameters: {', '.join(sorted(missing))}"
        )
    if "{" in path:
        leftover = path[path.index("{") : path.index("}") + 1]
        raise ValueError(f"{operation_id} still has an unsubstituted parameter {leftover}")

    return entry["method"].upper(), api_prefix + path


REST_TOOLS = [
    Tool(
        name="onshape_api_groups",
        description=(
            "List every Onshape REST API subject area and how many operations "
            "each contains. Start here when you do not know which part of the "
            "API covers a task."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="onshape_api_search",
        description=(
            "Search all Onshape REST API operations by keyword and/or group. "
            "Returns operationId, method, path, and summary. Use the returned "
            "operationId with onshape_api_describe or onshape_api_call."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Space-separated terms, all must match (e.g. 'export assembly')",
                },
                "group": {
                    "type": "string",
                    "description": "Restrict to one group, e.g. Assembly, PartStudio, Translation",
                },
                "limit": {"type": "integer", "description": "Max results", "default": 40},
            },
        },
    ),
    Tool(
        name="onshape_api_describe",
        description=(
            "Show the full contract for one Onshape API operation: path, query "
            "and header parameters, request body shape, and response codes. "
            "Call this before onshape_api_call on an unfamiliar operation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "operationId": {
                    "type": "string",
                    "description": "Operation id, e.g. createTranslation",
                }
            },
            "required": ["operationId"],
        },
    ),
    Tool(
        name="onshape_api_call",
        description=(
            "Invoke any Onshape REST API operation by operationId with a JSON "
            "body. Covers all 302 operations, including everything the "
            "purpose-built tools do not reach. For file upload use "
            "onshape_import_file, and for binary export use onshape_export_download."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "operationId": {"type": "string", "description": "Operation id to invoke"},
                "pathParams": {
                    "type": "object",
                    "description": "Path parameter values, e.g. {\"did\": \"...\", \"wid\": \"...\"}",
                },
                "query": {"type": "object", "description": "Query string parameters"},
                "body": {"type": "object", "description": "JSON request body"},
            },
            "required": ["operationId"],
        },
    ),
    Tool(
        name="onshape_import_file",
        description=(
            "Import a local CAD file (STEP, IGES, SLDPRT, X_T, STL and others) "
            "into an Onshape document by uploading it and translating it into "
            "parts or assemblies. This is the import path the other tools lack."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filePath": {"type": "string", "description": "Absolute path to the local file"},
                "documentId": {"type": "string", "description": "Target document ID"},
                "workspaceId": {"type": "string", "description": "Target workspace ID"},
                "translate": {
                    "type": "boolean",
                    "description": "Translate into parts/assemblies rather than leaving a blob",
                    "default": True,
                },
                "flattenAssemblies": {
                    "type": "boolean",
                    "description": (
                        "Collapse the file's assembly hierarchy into a single Part "
                        "Studio. Leave true for PCB and other rigid models; setting "
                        "it false creates one assembly tab per subassembly, each "
                        "with meaningless auto-generated mates."
                    ),
                    "default": True,
                },
                "importAppearances": {
                    "type": "boolean",
                    "description": "Keep colours and materials carried by the file",
                    "default": True,
                },
                "extractAssemblyHierarchy": {
                    "type": "boolean",
                    "description": (
                        "Rebuild the source assembly tree as Onshape assemblies. Only "
                        "useful for a genuine multi-body mechanism, not a board."
                    ),
                    "default": False,
                },
                "createComposite": {
                    "type": "boolean",
                    "description": "Create a composite part from the imported solids",
                    "default": False,
                },
                "yAxisIsUp": {
                    "type": "boolean",
                    "description": "Treat +Y as up instead of +Z",
                    "default": False,
                },
                "unit": {
                    "type": "string",
                    "description": "Unit for formats that carry none, e.g. millimeter",
                },
            },
            "required": ["filePath", "documentId", "workspaceId"],
        },
    ),
    Tool(
        name="onshape_export_download",
        description=(
            "Complete an export started by export_part_studio or "
            "export_assembly: poll the translation until it finishes, then "
            "write the resulting file to disk. Without this an export "
            "translation ID can never be turned into a file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "translationId": {
                    "type": "string",
                    "description": "Translation ID returned by an export tool",
                },
                "outputPath": {
                    "type": "string",
                    "description": "Absolute path to write the downloaded file to",
                },
                "timeoutSeconds": {
                    "type": "integer",
                    "description": "How long to wait for the translation",
                    "default": 300,
                },
            },
            "required": ["translationId", "outputPath"],
        },
    ),
]

REST_TOOL_NAMES = {t.name for t in REST_TOOLS}


# --- runtime handlers ---------------------------------------------------

import asyncio
import mimetypes
import time


async def _import_file(client, args: Dict[str, Any]) -> str:
    file_path = os.path.expanduser(args["filePath"])
    if not os.path.isfile(file_path):
        return f"No such file: {file_path}"

    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # flattenAssemblies defaults on: a KiCad board STEP carries a deep
    # component hierarchy, and importing it unflattened produces one assembly
    # tab per subassembly, each full of auto-generated Revolute/Parallel mates
    # that mean nothing for a rigid board.
    form: Dict[str, Any] = {
        "encodedFilename": filename,
        "fileContentLength": str(os.path.getsize(file_path)),
        "translate": str(bool(args.get("translate", True))).lower(),
        "flattenAssemblies": str(bool(args.get("flattenAssemblies", True))).lower(),
        "yAxisIsUp": str(bool(args.get("yAxisIsUp", False))).lower(),
        "importAppearances": str(bool(args.get("importAppearances", True))).lower(),
        "extractAssemblyHierarchy": str(
            bool(args.get("extractAssemblyHierarchy", False))
        ).lower(),
        "createComposite": str(bool(args.get("createComposite", False))).lower(),
        "createDrawingIfPossible": "false",
        "storeInDocument": "true",
    }
    if args.get("unit"):
        form["unit"] = args["unit"]

    path = (
        f"{DEFAULT_API_PREFIX}/translations/d/{args['documentId']}"
        f"/w/{args['workspaceId']}"
    )
    with open(file_path, "rb") as fh:
        files = {"file": (filename, fh.read(), content_type)}
        result = await client.upload_multipart(path, files=files, data=form)

    tid = result.get("id", "unknown")
    state = result.get("requestState", "unknown")
    return (
        f"Import started for {filename}\n"
        f"Translation ID: {tid}\nState: {state}\n"
        f"Document: {args['documentId']} workspace {args['workspaceId']}\n"
        f"Poll with onshape_api_call operationId=getTranslation pathParams={{\"tid\": \"{tid}\"}}"
    )


async def _export_download(client, args: Dict[str, Any]) -> str:
    tid = args["translationId"]
    out_path = os.path.expanduser(args["outputPath"])
    deadline = time.monotonic() + int(args.get("timeoutSeconds", 300))

    status: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = await client.request_json("GET", f"{DEFAULT_API_PREFIX}/translations/{tid}")
        state = status.get("requestState")
        if state == "DONE":
            break
        if state == "FAILED":
            return f"Translation {tid} failed: {status.get('failureReason', 'unknown reason')}"
        await asyncio.sleep(2)
    else:
        return f"Translation {tid} did not finish in time; last state {status.get('requestState')}"

    ext_ids = status.get("resultExternalDataIds") or []
    doc_id = status.get("resultDocumentId") or status.get("documentId")
    if not ext_ids or not doc_id:
        return (
            "Translation finished but returned no downloadable external data. "
            f"Response keys: {sorted(status.keys())}"
        )

    path = f"{DEFAULT_API_PREFIX}/documents/d/{doc_id}/externaldata/{ext_ids[0]}"
    payload = await client.request_binary("GET", path)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(payload)
    return f"Wrote {len(payload)} bytes to {out_path}"


async def handle_rest_tool(name: str, args: Dict[str, Any], client) -> str:
    """Dispatch one of the generic REST tools. Returns display text."""
    if name == "onshape_api_groups":
        rows = list_groups()
        body = "\n".join(f"{count:5d}  {tag}" for tag, count in rows)
        return f"{sum(c for _, c in rows)} operations in {len(rows)} groups:\n{body}"

    if name == "onshape_api_search":
        hits = search_operations(
            query=args.get("query", ""),
            group=args.get("group", ""),
            limit=int(args.get("limit", 40)),
        )
        if not hits:
            return "No matching operations."
        lines = [
            f"{h['method']:6} {h['path']}\n       {h['operationId']}  [{h['group']}]\n"
            f"       {h['summary']}"
            for h in hits
        ]
        return f"{len(hits)} operation(s):\n" + "\n".join(lines)

    if name == "onshape_api_describe":
        try:
            return json.dumps(describe_operation(args["operationId"]), indent=2)
        except KeyError:
            return f"Unknown operationId: {args['operationId']}"

    if name == "onshape_api_call":
        op_id = args["operationId"]
        try:
            method, path = build_path(op_id, args.get("pathParams") or {})
        except KeyError:
            return f"Unknown operationId: {op_id}"
        except ValueError as exc:
            return str(exc)

        result = await client.request_json(
            method, path, params=args.get("query") or None, json_body=args.get("body")
        )
        text = json.dumps(result, indent=2, default=str)
        if len(text) > 40000:
            text = text[:40000] + f"\n... truncated, {len(text)} chars total"
        return f"{method} {path}\n{text}"

    if name == "onshape_import_file":
        return await _import_file(client, args)

    if name == "onshape_export_download":
        return await _export_download(client, args)

    return f"Unhandled REST tool: {name}"
