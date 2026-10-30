from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from clouddrive.db import engine
from clouddrive.listing import decode_cursor
from clouddrive.service import query
from helpers import begin, send_part


def test_cursor_rejects_malformed_and_foreign_contexts():
    for value in ("!", "null", "W10", "e30"):
        with pytest.raises(HTTPException):
            decode_cursor(value, uuid4(), {"search": "", "view": "all"})


@pytest.mark.integration
def test_cursor_pages_ties_without_duplicates_and_searches_beyond_200(client, auth):
    workspace = client.get("/api/workspace", headers=auth).json()["id"]
    ids = [str(uuid4()) for _ in range(215)]
    with engine().begin() as conn:
        for index, id in enumerate(ids):
            query(
                conn,
                """INSERT INTO files(id,workspace_id,request_id,name,size,media_type,expected_sha256,part_size,object_key,status,created_at)
            VALUES (:id,:ws,:request,:name,1,'text/plain',:sha,8388608,:key,'EXPIRED',:created)""",
                id=id,
                ws=workspace,
                request=uuid4(),
                name=f"archive-{index:03d}.txt",
                sha="0" * 64,
                key=f"{workspace}/{id}",
                created=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
    seen = []
    cursor = None
    for _ in range(5):
        params = {"limit": 50, "compact": True}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/files", headers=auth, params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert all("parts" not in f and f["result"] is None for f in result["files"])
        seen.extend(f["id"] for f in result["files"])
        cursor = result["next_cursor"]
    assert cursor is None and len(seen) == len(set(seen)) == 215
    assert set(seen) == set(ids)
    found = client.get("/api/files", headers=auth, params={"search": "archive-000"}).json()["files"]
    assert len(found) == 1 and found[0]["id"] == ids[0]
    assert client.get("/api/files?search=%25", headers=auth).json()["files"] == []
    first = client.get("/api/files?limit=1", headers=auth).json()
    other = {"Authorization": "Bearer " + client.post("/api/workspaces").json()["access_key"]}
    for headers, params in [
        (other, {}),
        (auth, {"search": "changed"}),
        (auth, {"view": "uploads"}),
    ]:
        assert (
            client.get(
                "/api/files", headers=headers, params={"cursor": first["next_cursor"], **params}
            ).status_code
            == 400
        )
    for params in ({"limit": 0}, {"limit": 201}, {"view": "invalid"}):
        assert client.get("/api/files", headers=auth, params=params).status_code == 422


@pytest.mark.integration
def test_compact_listing_keeps_receipts_in_authenticated_detail(client, auth):
    file, _ = begin(client, auth, b"part bytes")
    send_part(client, auth, file, 1, b"part bytes")
    summary = client.get("/api/files?compact=true", headers=auth).json()["files"][0]
    assert summary["confirmed_parts"] == 1 and "parts" not in summary
    detail = client.get(f"/api/uploads/{file['id']}", headers=auth).json()
    assert len(detail["parts"]) == 1 and detail["parts"][0]["etag"]


@pytest.mark.integration
def test_long_unicode_search_roundtrips_a_bounded_cursor(client, auth):
    term = "🗂" * 220
    for _ in range(2):
        begin(client, auth, b"bytes", name=term)
    result = client.get("/api/files", headers=auth, params={"limit": 1, "search": term}).json()
    assert len(result["next_cursor"]) <= 2048
    second = client.get(
        "/api/files",
        headers=auth,
        params={"limit": 1, "search": term, "cursor": result["next_cursor"]},
    )
    assert second.status_code == 200 and len(second.json()["files"]) == 1
    assert second.json()["files"][0]["id"] != result["files"][0]["id"]


@pytest.mark.integration
@pytest.mark.parametrize("sort", ["newest", "name", "size"])
def test_whole_workspace_sorting_keeps_stable_cursors_and_ties(client, auth, sort):
    files = [
        begin(client, auth, b"x" * size, name=name)[0]
        for name, size in [
            ("z-last", 3),
            ("a-first", 5),
            ("A-first", 5),
            ("m-middle", 1),
            ("🗂" * 220, 2),
            ("🗃" * 220, 1),
        ]
    ]
    with engine().begin() as conn:
        query(conn, "UPDATE files SET created_at='2026-01-01T00:00:00Z'")
    key = (
        (lambda f: (f["name"].lower(), f["id"]))
        if sort == "name"
        else ((lambda f: (f["size"], f["id"])) if sort == "size" else (lambda f: f["id"]))
    )
    expected = [f["id"] for f in sorted(files, key=key, reverse=sort != "name")]
    cursor, seen = None, []
    for page in range(6):
        result = client.get(
            "/api/files",
            headers=auth,
            params={"limit": 1, "sort": sort, **({"cursor": cursor} if cursor else {})},
        )
        assert result.status_code == 200, result.text
        seen.extend(f["id"] for f in result.json()["files"])
        cursor = result.json()["next_cursor"]
        if page == 0:
            assert len(cursor) <= 2048
            changed = "name" if sort != "name" else "size"
            assert (
                client.get(
                    "/api/files", headers=auth, params={"cursor": cursor, "sort": changed}
                ).status_code
                == 400
            )
    assert cursor is None and seen == expected
