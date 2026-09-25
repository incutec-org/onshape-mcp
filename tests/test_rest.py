"""Unit tests for the generic REST API discovery/invocation module.

onshape_mcp/rest.py talks to the bundled OpenAPI document, not the network, so
these tests swap in a small synthetic spec (via monkeypatching the module's
spec/index cache) to exercise discovery and path building deterministically,
then mock the OnshapeClient transport (request_json/request_binary/
upload_multipart) for the call-dispatch and file import/export helpers. No
network access is used anywhere in this file.
"""

import json

import pytest
from unittest.mock import AsyncMock

from onshape_mcp import rest


# --- synthetic OpenAPI spec ----------------------------------------------

SYNTHETIC_SPEC = {
    "paths": {
        "/foo/{id}/bar/{sub}": {
            "get": {
                "operationId": "getFooBar",
                "tags": ["Foo"],
                "summary": "Get a foo's bar",
                "description": "x" * 900,
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Foo id",
                    },
                    {
                        "name": "sub",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "q",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "query term",
                    },
                ],
                "responses": {"200": {}, "404": {}},
            }
        },
        "/foo": {
            "post": {
                "operationId": "createFoo",
                "tags": ["Foo"],
                "summary": "Create a foo",
                "parameters": [
                    {
                        "name": "X-Trace",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "items": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Item"},
                                    },
                                    "nested": {"$ref": "#/components/schemas/Item"},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {}},
            }
        },
        "/bulk": {
            "post": {
                "operationId": "bulkCreateFoo",
                "tags": ["Foo"],
                "summary": "Create many foos",
                "parameters": [],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Item"},
                            }
                        }
                    }
                },
                "responses": {"200": {}},
            }
        },
        "/raw": {
            "post": {
                "operationId": "rawTextOp",
                "tags": ["Foo"],
                "summary": "Accept raw text",
                "parameters": [],
                "requestBody": {
                    "content": {"text/plain": {"schema": {"type": "string"}}}
                },
                "responses": {"200": {}},
            }
        },
        "/bar/{id}": {
            "delete": {
                "operationId": "deleteBar",
                "tags": ["Bar"],
                "summary": "Delete a bar",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {}},
            }
        },
        "/search": {
            "get": {
                "operationId": "searchThings",
                "tags": ["Bar"],
                "summary": "Search for things",
                "parameters": [],
                "responses": {"200": {}},
            }
        },
        "/leftover/{a}/{b}": {
            "get": {
                "operationId": "leftoverOp",
                "tags": ["Foo"],
                "summary": "Has an undeclared path parameter",
                "parameters": [
                    {"name": "a", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {}},
            }
        },
        "/untagged": {
            "get": {
                "operationId": "untaggedOp",
                "summary": "No tags at all",
                "parameters": [],
                "responses": {"200": {}},
            }
        },
    },
    "components": {
        "schemas": {
            "Item": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"$ref": "#/components/schemas/Item"},
                },
            }
        }
    },
}


@pytest.fixture
def spec(monkeypatch):
    """Install the synthetic spec as the module's cache and reset the index."""
    monkeypatch.setattr(rest, "_spec_cache", SYNTHETIC_SPEC)
    monkeypatch.setattr(rest, "_index_cache", None)
    return SYNTHETIC_SPEC


@pytest.fixture
def rest_client():
    """An AsyncMock standing in for OnshapeClient's async transport methods."""
    client = AsyncMock()
    return client


# --- load_spec / operation index ------------------------------------------


def test_load_spec_reads_and_memoises_bundled_file(monkeypatch):
    monkeypatch.setattr(rest, "_spec_cache", None)
    first = rest.load_spec()
    assert "paths" in first
    assert isinstance(first["paths"], dict)

    second = rest.load_spec()
    assert second is first  # cached, not re-read from disk


def test_operation_index_is_memoised(spec, monkeypatch):
    index = rest._operation_index()
    assert "getFooBar" in index
    assert index["getFooBar"]["method"] == "get"
    assert index["getFooBar"]["path"] == "/foo/{id}/bar/{sub}"

    # Mutating the spec afterwards must not change the memoised index.
    monkeypatch.setitem(spec["paths"], "/new", {"get": {"operationId": "newOp"}})
    assert "newOp" not in rest._operation_index()


# --- list_groups -----------------------------------------------------------


def test_list_groups_counts_tags_and_sorts_by_count_then_name(spec):
    rows = rest.list_groups()
    assert rows[0] == ("Foo", 5)  # getFooBar, createFoo, bulkCreateFoo, rawTextOp, leftoverOp
    assert ("Bar", 2) in rows
    assert ("(untagged)", 1) in rows


# --- search_operations -------------------------------------------------


def test_search_operations_matches_all_terms_across_fields(spec):
    hits = rest.search_operations(query="foo bar")
    ids = {h["operationId"] for h in hits}
    assert ids == {"getFooBar"}


def test_search_operations_filters_by_group_case_insensitive(spec):
    hits = rest.search_operations(group="bar")
    ids = {h["operationId"] for h in hits}
    assert ids == {"deleteBar", "searchThings"}


def test_search_operations_respects_limit(spec):
    hits = rest.search_operations(query="", limit=1)
    assert len(hits) == 1


def test_search_operations_no_match_returns_empty(spec):
    assert rest.search_operations(query="nonexistentterm") == []


def test_search_operations_result_shape(spec):
    hits = rest.search_operations(query="getfoobar")
    assert hits == [
        {
            "operationId": "getFooBar",
            "method": "GET",
            "path": "/foo/{id}/bar/{sub}",
            "group": "Foo",
            "summary": "Get a foo's bar",
        }
    ]


# --- describe_operation --------------------------------------------------


def test_describe_operation_unknown_raises_keyerror(spec):
    with pytest.raises(KeyError):
        rest.describe_operation("noSuchOp")


def test_describe_operation_splits_params_by_location_and_truncates_description(spec):
    info = rest.describe_operation("getFooBar")
    assert info["operationId"] == "getFooBar"
    assert info["method"] == "GET"
    assert info["url"] == rest.DEFAULT_API_PREFIX + "/foo/{id}/bar/{sub}"
    assert info["group"] == "Foo"
    assert [p["name"] for p in info["parameters"]["path"]] == ["id", "sub"]
    assert [p["name"] for p in info["parameters"]["query"]] == ["q"]
    assert "header" not in info["parameters"]
    assert len(info["description"]) == 800
    assert info["requestBody"] is None
    assert info["responses"] == ["200", "404"]


def test_describe_operation_request_body_object_with_refs(spec):
    info = rest.describe_operation("createFoo")
    assert info["parameters"]["header"][0]["name"] == "X-Trace"
    body = info["requestBody"]
    assert body["contentType"] == "application/json"
    assert body["required"] is True
    assert body["schema"] == {"name": "string", "items": "array<object>", "nested": "Item"}


def test_describe_operation_request_body_top_level_array_of_ref(spec):
    info = rest.describe_operation("bulkCreateFoo")
    # Top-level array schema recurses into _schema_summary for its items,
    # which at depth>=1 short-circuits a $ref to just its component name.
    assert info["requestBody"]["schema"] == ["Item"]


def test_describe_operation_request_body_bare_scalar_schema(spec):
    info = rest.describe_operation("rawTextOp")
    assert info["requestBody"]["schema"] == "string"


def test_schema_summary_empty_schema_returns_none():
    assert rest._schema_summary({}) is None


def test_resolve_ref_missing_component_returns_empty_dict(spec):
    assert rest._resolve_ref("#/components/schemas/DoesNotExist") == {}


# --- build_path --------------------------------------------------------


def test_build_path_percent_encodes_path_params_with_slashes(spec):
    method, path = rest.build_path("getFooBar", {"id": "normal", "sub": "J/D"})
    assert method == "GET"
    assert path == rest.DEFAULT_API_PREFIX + "/foo/normal/bar/J%2FD"


def test_build_path_uses_custom_api_prefix(spec):
    method, path = rest.build_path(
        "deleteBar", {"id": "abc"}, api_prefix="/api/v99"
    )
    assert method == "DELETE"
    assert path == "/api/v99/bar/abc"


def test_build_path_unknown_operation_raises_keyerror(spec):
    with pytest.raises(KeyError):
        rest.build_path("noSuchOp", {})


def test_build_path_missing_required_param_raises_valueerror(spec):
    with pytest.raises(ValueError, match="sub"):
        rest.build_path("getFooBar", {"id": "only-id"})


def test_build_path_optional_missing_param_is_skipped(spec):
    # searchThings has no path params at all; omitting them is fine.
    method, path = rest.build_path("searchThings", {})
    assert (method, path) == ("GET", rest.DEFAULT_API_PREFIX + "/search")


def test_build_path_leftover_unsubstituted_param_raises_valueerror(spec):
    with pytest.raises(ValueError, match=r"\{b\}"):
        rest.build_path("leftoverOp", {"a": "1"})


# --- handle_rest_tool: discovery dispatch --------------------------------


@pytest.mark.asyncio
async def test_handle_onshape_api_groups(spec, rest_client):
    text = await rest.handle_rest_tool("onshape_api_groups", {}, rest_client)
    assert text.startswith("8 operations in 3 groups:")
    assert "Foo" in text and "Bar" in text


@pytest.mark.asyncio
async def test_handle_onshape_api_search_with_hits(spec, rest_client):
    text = await rest.handle_rest_tool(
        "onshape_api_search", {"query": "delete"}, rest_client
    )
    assert "1 operation(s):" in text
    assert "deleteBar" in text


@pytest.mark.asyncio
async def test_handle_onshape_api_search_no_hits(spec, rest_client):
    text = await rest.handle_rest_tool(
        "onshape_api_search", {"query": "zzz-nothing"}, rest_client
    )
    assert text == "No matching operations."


@pytest.mark.asyncio
async def test_handle_onshape_api_describe_success(spec, rest_client):
    text = await rest.handle_rest_tool(
        "onshape_api_describe", {"operationId": "deleteBar"}, rest_client
    )
    parsed = json.loads(text)
    assert parsed["operationId"] == "deleteBar"


@pytest.mark.asyncio
async def test_handle_onshape_api_describe_unknown(spec, rest_client):
    text = await rest.handle_rest_tool(
        "onshape_api_describe", {"operationId": "noSuchOp"}, rest_client
    )
    assert text == "Unknown operationId: noSuchOp"


# --- handle_rest_tool: onshape_api_call ----------------------------------


@pytest.mark.asyncio
async def test_handle_onshape_api_call_success(spec, rest_client):
    rest_client.request_json = AsyncMock(return_value={"ok": True})
    text = await rest.handle_rest_tool(
        "onshape_api_call",
        {
            "operationId": "getFooBar",
            "pathParams": {"id": "1", "sub": "2"},
            "query": {"q": "x"},
            "body": {"k": "v"},
        },
        rest_client,
    )
    assert text.startswith(f"GET {rest.DEFAULT_API_PREFIX}/foo/1/bar/2\n")
    assert json.loads(text.split("\n", 1)[1]) == {"ok": True}
    rest_client.request_json.assert_awaited_once_with(
        "GET",
        rest.DEFAULT_API_PREFIX + "/foo/1/bar/2",
        params={"q": "x"},
        json_body={"k": "v"},
    )


@pytest.mark.asyncio
async def test_handle_onshape_api_call_unknown_operation(spec, rest_client):
    text = await rest.handle_rest_tool(
        "onshape_api_call", {"operationId": "noSuchOp"}, rest_client
    )
    assert text == "Unknown operationId: noSuchOp"
    rest_client.request_json.assert_not_called()


@pytest.mark.asyncio
async def test_handle_onshape_api_call_missing_required_param(spec, rest_client):
    text = await rest.handle_rest_tool(
        "onshape_api_call",
        {"operationId": "getFooBar", "pathParams": {"id": "1"}},
        rest_client,
    )
    assert "requires path parameters" in text
    assert "sub" in text
    rest_client.request_json.assert_not_called()


@pytest.mark.asyncio
async def test_handle_onshape_api_call_truncates_long_response(spec, rest_client):
    rest_client.request_json = AsyncMock(return_value={"data": "x" * 50000})
    text = await rest.handle_rest_tool(
        "onshape_api_call", {"operationId": "searchThings"}, rest_client
    )
    assert "... truncated," in text
    assert text.endswith("chars total")


# --- handle_rest_tool: unhandled name -------------------------------------


@pytest.mark.asyncio
async def test_handle_rest_tool_unknown_name_is_reported(spec, rest_client):
    text = await rest.handle_rest_tool("onshape_totally_made_up", {}, rest_client)
    assert text == "Unhandled REST tool: onshape_totally_made_up"


# --- REST_TOOLS / REST_TOOL_NAMES -----------------------------------------


def test_rest_tool_names_matches_declared_tools():
    assert rest.REST_TOOL_NAMES == {t.name for t in rest.REST_TOOLS}
    assert rest.REST_TOOL_NAMES == {
        "onshape_api_groups",
        "onshape_api_search",
        "onshape_api_describe",
        "onshape_api_call",
        "onshape_import_file",
        "onshape_export_download",
    }


# --- _import_file ----------------------------------------------------------


@pytest.mark.asyncio
async def test_import_file_missing_file_returns_message(rest_client):
    text = await rest._import_file(
        rest_client, {"filePath": "/no/such/file.step", "documentId": "d", "workspaceId": "w"}
    )
    assert text == "No such file: /no/such/file.step"
    rest_client.upload_multipart.assert_not_called()


@pytest.mark.asyncio
async def test_import_file_success_default_form_fields(tmp_path, rest_client):
    src = tmp_path / "board.step"
    src.write_bytes(b"step-file-bytes")
    rest_client.upload_multipart = AsyncMock(
        return_value={"id": "tid-1", "requestState": "ACTIVE"}
    )

    text = await rest._import_file(
        rest_client,
        {"filePath": str(src), "documentId": "docABC", "workspaceId": "wsXYZ"},
    )

    assert "Import started for board.step" in text
    assert "Translation ID: tid-1" in text
    assert "State: ACTIVE" in text
    assert "Document: docABC workspace wsXYZ" in text
    assert 'pathParams={"tid": "tid-1"}' in text

    rest_client.upload_multipart.assert_awaited_once()
    call = rest_client.upload_multipart.await_args
    path = call.args[0]
    assert path == f"{rest.DEFAULT_API_PREFIX}/translations/d/docABC/w/wsXYZ"
    form = call.kwargs["data"]
    assert form["encodedFilename"] == "board.step"
    assert form["fileContentLength"] == str(len(b"step-file-bytes"))
    assert form["translate"] == "true"
    assert form["flattenAssemblies"] == "true"
    assert form["yAxisIsUp"] == "false"
    assert form["importAppearances"] == "true"
    assert form["extractAssemblyHierarchy"] == "false"
    assert form["createComposite"] == "false"
    assert form["createDrawingIfPossible"] == "false"
    assert form["storeInDocument"] == "true"
    assert "unit" not in form

    files = call.kwargs["files"]
    filename, payload, content_type = files["file"]
    assert filename == "board.step"
    assert payload == b"step-file-bytes"
    assert content_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_import_file_overridden_flags_and_unit(tmp_path, rest_client):
    src = tmp_path / "part.txt"
    src.write_bytes(b"hello")
    rest_client.upload_multipart = AsyncMock(return_value={"id": "tid-2"})

    await rest._import_file(
        rest_client,
        {
            "filePath": str(src),
            "documentId": "d",
            "workspaceId": "w",
            "translate": False,
            "flattenAssemblies": False,
            "yAxisIsUp": True,
            "importAppearances": False,
            "extractAssemblyHierarchy": True,
            "createComposite": True,
            "unit": "millimeter",
        },
    )

    form = rest_client.upload_multipart.await_args.kwargs["data"]
    assert form["translate"] == "false"
    assert form["flattenAssemblies"] == "false"
    assert form["yAxisIsUp"] == "true"
    assert form["importAppearances"] == "false"
    assert form["extractAssemblyHierarchy"] == "true"
    assert form["createComposite"] == "true"
    assert form["unit"] == "millimeter"

    files = rest_client.upload_multipart.await_args.kwargs["files"]
    _, _, content_type = files["file"]
    assert content_type == "text/plain"


@pytest.mark.asyncio
async def test_import_file_dispatch_via_handle_rest_tool(tmp_path, spec, rest_client):
    src = tmp_path / "thing.step"
    src.write_bytes(b"abc")
    rest_client.upload_multipart = AsyncMock(return_value={"id": "tid-3", "requestState": "DONE"})

    text = await rest.handle_rest_tool(
        "onshape_import_file",
        {"filePath": str(src), "documentId": "d", "workspaceId": "w"},
        rest_client,
    )
    assert "Import started for thing.step" in text


# --- _export_download --------------------------------------------------


@pytest.mark.asyncio
async def test_export_download_success_after_polling(tmp_path, rest_client, monkeypatch):
    monkeypatch.setattr(rest.asyncio, "sleep", AsyncMock())
    rest_client.request_json = AsyncMock(
        side_effect=[
            {"requestState": "ACTIVE"},
            {
                "requestState": "DONE",
                "resultExternalDataIds": ["ext1"],
                "resultDocumentId": "doc1",
            },
        ]
    )
    rest_client.request_binary = AsyncMock(return_value=b"downloaded-bytes")

    out_path = tmp_path / "nested" / "out.step"
    text = await rest._export_download(
        rest_client,
        {"translationId": "tid9", "outputPath": str(out_path), "timeoutSeconds": 30},
    )

    assert out_path.read_bytes() == b"downloaded-bytes"
    assert text == f"Wrote {len(b'downloaded-bytes')} bytes to {out_path}"
    rest_client.request_binary.assert_awaited_once_with(
        "GET", f"{rest.DEFAULT_API_PREFIX}/documents/d/doc1/externaldata/ext1"
    )
    assert rest.asyncio.sleep.await_count == 1


@pytest.mark.asyncio
async def test_export_download_resolves_document_id_fallback(tmp_path, rest_client, monkeypatch):
    monkeypatch.setattr(rest.asyncio, "sleep", AsyncMock())
    rest_client.request_json = AsyncMock(
        return_value={
            "requestState": "DONE",
            "resultExternalDataIds": ["ext2"],
            "documentId": "doc-fallback",
        }
    )
    rest_client.request_binary = AsyncMock(return_value=b"x")

    out_path = tmp_path / "out2.step"
    text = await rest._export_download(
        rest_client,
        {"translationId": "tid10", "outputPath": str(out_path), "timeoutSeconds": 30},
    )

    assert "Wrote 1 bytes" in text
    rest_client.request_binary.assert_awaited_once_with(
        "GET", f"{rest.DEFAULT_API_PREFIX}/documents/d/doc-fallback/externaldata/ext2"
    )


@pytest.mark.asyncio
async def test_export_download_failed_with_reason(rest_client):
    rest_client.request_json = AsyncMock(
        return_value={"requestState": "FAILED", "failureReason": "bad geometry"}
    )
    text = await rest._export_download(
        rest_client, {"translationId": "tid-f", "outputPath": "/tmp/whatever.step"}
    )
    assert text == "Translation tid-f failed: bad geometry"


@pytest.mark.asyncio
async def test_export_download_failed_without_reason_defaults(rest_client):
    rest_client.request_json = AsyncMock(return_value={"requestState": "FAILED"})
    text = await rest._export_download(
        rest_client, {"translationId": "tid-f2", "outputPath": "/tmp/whatever.step"}
    )
    assert text == "Translation tid-f2 failed: unknown reason"


@pytest.mark.asyncio
async def test_export_download_times_out(rest_client):
    text = await rest._export_download(
        rest_client,
        {"translationId": "tid-slow", "outputPath": "/tmp/whatever.step", "timeoutSeconds": 0},
    )
    assert text == "Translation tid-slow did not finish in time; last state None"
    rest_client.request_json.assert_not_called()


@pytest.mark.asyncio
async def test_export_download_done_without_downloadable_data(rest_client):
    rest_client.request_json = AsyncMock(return_value={"requestState": "DONE"})
    text = await rest._export_download(
        rest_client, {"translationId": "tid-empty", "outputPath": "/tmp/whatever.step"}
    )
    assert "Translation finished but returned no downloadable external data." in text
    assert "['requestState']" in text


@pytest.mark.asyncio
async def test_export_download_dispatch_via_handle_rest_tool(tmp_path, spec, rest_client, monkeypatch):
    monkeypatch.setattr(rest.asyncio, "sleep", AsyncMock())
    rest_client.request_json = AsyncMock(
        return_value={
            "requestState": "DONE",
            "resultExternalDataIds": ["e"],
            "resultDocumentId": "d",
        }
    )
    rest_client.request_binary = AsyncMock(return_value=b"z")
    out_path = tmp_path / "dispatched.step"

    text = await rest.handle_rest_tool(
        "onshape_export_download",
        {"translationId": "t", "outputPath": str(out_path)},
        rest_client,
    )
    assert "Wrote 1 bytes" in text
