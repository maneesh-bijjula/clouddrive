import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from . import observability, service
from .config import settings
from .db import engine, migrate
from .models import NewUpload, PartChecksum, PartReceipt
from .recovery_faults import checkpoint
from .storage import bootstrap, s3

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    migrate()
    bootstrap()
    yield
    engine().dispose()


app = FastAPI(title="CloudDrive", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Retry-After"],
)
bearer = HTTPBearer(auto_error=False)


def authenticated(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    ws = service.workspace(credentials.credentials if credentials else None)
    request.state.workspace_id = str(ws["id"])
    return ws


Workspace = Annotated[dict, Depends(authenticated)]


async def infrastructure_error(request: Request, exc: Exception):
    if isinstance(exc, ClientError) and exc.response["Error"]["Code"] == "NoSuchUpload":
        return JSONResponse(
            {"detail": "This upload has expired in storage. Delete it and upload the file again."},
            status_code=409,
        )
    observability.event("infrastructure_error", error_type=type(exc).__name__)
    return JSONResponse(
        {"detail": "A storage service is temporarily unavailable. Your upload can be retried."},
        status_code=503,
        headers={"Retry-After": "1"},
    )


for exception in (ClientError, BotoCoreError, SQLAlchemyError):
    app.add_exception_handler(exception, infrastructure_error)


@app.middleware("http")
async def security_headers(request, call_next):
    started = time.monotonic()
    identifier = observability.request_id(request.headers.get("X-Request-ID"))
    context = observability.correlation.set(identifier)
    counted = request.url.path.startswith("/api/") and request.url.path != "/api/health"
    admitted = not counted or observability.enter_request(settings().max_api_inflight)
    try:
        if not admitted:
            response = JSONResponse(
                {"detail": {"code": "api_busy", "message": "The service is busy. Retry shortly."}},
                status_code=429,
                headers={"Retry-After": "1"},
            )
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                observability.event("api_unexpected_error", error_type=type(exc).__name__)
                response = JSONResponse(
                    {"detail": "An internal service error occurred. Retry this request."},
                    status_code=500,
                )
        elapsed = (time.monotonic() - started) * 1000
        observability.observe(
            getattr(request.state, "workspace_id", None), response.status_code, elapsed
        )
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if request.url.path.startswith("/api/"):
            observability.event(
                "api_request",
                method=request.method,
                route=route,
                status=response.status_code,
                duration_ms=round(elapsed, 3),
            )
    finally:
        if counted and admitted:
            observability.leave_request()
        observability.correlation.reset(context)
    response.headers["X-Request-ID"] = identifier
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    public_storage = urlsplit(settings().s3_public_endpoint)
    storage_origin = f"{public_storage.scheme}://{public_storage.netloc}"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {storage_origin}; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    with engine().connect() as conn:
        service.query(conn, "SELECT 1")
    s3().head_bucket(Bucket=settings().s3_bucket)
    return {"status": "ok"}


@app.post("/api/workspaces", status_code=201)
def new_workspace():
    return service.create_workspace()


@app.get("/api/workspace")
def current_workspace(ws: Workspace):
    config = settings()
    return {
        "id": ws["id"],
        "created_at": ws["created_at"],
        "max_file_bytes": settings().max_file_bytes,
        "part_bytes": settings().part_bytes,
        "upload_limits": {
            "active_files": min(
                config.browser_active_files,
                config.max_active_uploads_per_workspace,
                config.browser_global_parts,
            ),
            "hash_workers": config.browser_hash_workers,
            "global_parts": config.browser_global_parts,
            "parts_per_file": config.browser_parts_per_file,
        },
    }


@app.get("/api/metrics")
def metrics(ws: Workspace):
    with engine().connect() as conn:
        states = (
            service.query(
                conn,
                "SELECT status,count(*) AS count FROM files WHERE workspace_id=:workspace GROUP BY status",
                workspace=ws["id"],
            )
            .mappings()
            .all()
        )
        pending = (
            service.query(
                conn,
                """SELECT count(*) AS count,
            coalesce(max(extract(epoch FROM (now()-o.created_at))),0) AS oldest_age_seconds
            FROM processing_outbox o JOIN files f ON f.id=o.file_id WHERE f.workspace_id=:workspace""",
                workspace=ws["id"],
            )
            .mappings()
            .one()
        )
        claims = (
            service.query(
                conn,
                """SELECT count(*) FILTER (WHERE claim IS NOT NULL AND lease_until>now()) AS active,
            count(*) FILTER (WHERE claim IS NOT NULL AND lease_until<=now()) AS expired
            FROM files WHERE workspace_id=:workspace AND status='PROCESSING'""",
                workspace=ws["id"],
            )
            .mappings()
            .one()
        )
        verification = (
            service.query(
                conn,
                """SELECT count(*) AS count,
            avg((result->>'stream_verification_seconds')::double precision) AS mean_seconds,
            max((result->>'stream_verification_seconds')::double precision) AS max_seconds
            FROM files WHERE workspace_id=:workspace AND status='READY'
            AND result ? 'stream_verification_seconds'""",
                workspace=ws["id"],
            )
            .mappings()
            .one()
        )
    return {
        "scope": "This authenticated workspace only",
        "workspace_id": ws["id"],
        "files_by_status": {row["status"]: row["count"] for row in states},
        "outbox": dict(pending),
        "claims": dict(claims),
        "verification": dict(verification),
        "api": observability.snapshot(str(ws["id"])),
    }


@app.get("/api/ops")
def operations(request: Request):
    configured = settings().metrics_key
    supplied = request.headers.get("X-Metrics-Key", "")
    if not configured or not secrets.compare_digest(configured.encode(), supplied.encode()):
        raise HTTPException(404, "Not found")
    pool = engine().pool
    with engine().connect() as conn:
        state = (
            service.query(
                conn,
                """SELECT count(*) FILTER (WHERE status IN ('UPLOADING','FINALIZING','PROCESSING')) AS active_uploads,
            count(*) FILTER (WHERE status='PROCESSING' AND claim IS NOT NULL AND lease_until>now()) AS active_claims,
            count(*) FILTER (WHERE status='PROCESSING' AND claim IS NOT NULL AND lease_until<=now()) AS expired_claims
            FROM files""",
            )
            .mappings()
            .one()
        )
        outbox = (
            service.query(
                conn,
                "SELECT count(*) AS count,coalesce(max(extract(epoch FROM(now()-created_at))),0) AS oldest_age_seconds FROM processing_outbox",
            )
            .mappings()
            .one()
        )
    return {
        "scope": "Operational key; this API process and database",
        "api_inflight": observability.inflight(),
        "api_inflight_limit": settings().max_api_inflight,
        "db_pool": {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": max(0, pool.overflow()),
            "capacity": settings().db_pool_size + settings().db_max_overflow,
        },
        "uploads": dict(state),
        "outbox": dict(outbox),
    }


@app.get("/api/files")
def files(ws: Workspace):
    with engine().connect() as conn:
        rows = (
            service.query(
                conn,
                "SELECT * FROM files WHERE workspace_id=:workspace AND status NOT IN ('DELETING','DELETED') ORDER BY created_at DESC LIMIT 200",
                workspace=ws["id"],
            )
            .mappings()
            .all()
        )
        stats = (
            service.query(
                conn,
                """SELECT count(*) AS total,
            count(*) FILTER (WHERE status='READY') AS ready,
            coalesce(sum(size) FILTER (WHERE status='READY'),0) AS bytes,
            count(*) FILTER (WHERE status IN ('UPLOADING','FINALIZING','PROCESSING')) AS pending
            FROM files WHERE workspace_id=:workspace AND status NOT IN ('DELETING','DELETED')""",
                workspace=ws["id"],
            )
            .mappings()
            .one()
        )
        parts_by_file = {}
        if rows:
            for part in service.query(
                conn,
                "SELECT file_id,number,checksum,etag FROM upload_parts WHERE file_id=ANY(:ids) ORDER BY file_id,number",
                ids=[row["id"] for row in rows],
            ).mappings():
                parts_by_file.setdefault(part["file_id"], []).append(
                    {key: part[key] for key in ("number", "checksum", "etag")}
                )
        return {
            "files": [
                service.describe(conn, row, parts_by_file.get(row["id"], [])) for row in rows
            ],
            "limit": 200,
            "stats": dict(stats),
        }


@app.post("/api/uploads", status_code=201)
def initiate(data: NewUpload, ws: Workspace):
    return service.initiate(ws["id"], data)


@app.get("/api/uploads/{id}")
def upload(id: UUID, ws: Workspace):
    with engine().connect() as conn:
        return service.describe(conn, service.get_file(conn, ws["id"], id))


@app.post("/api/uploads/{id}/parts/{number}/sign")
def sign(id: UUID, number: int, data: PartChecksum, ws: Workspace):
    return service.sign_part(ws["id"], id, number, data.checksum)


@app.put("/api/uploads/{id}/parts/{number}")
def acknowledge(id: UUID, number: int, data: PartReceipt, ws: Workspace):
    return service.acknowledge(ws["id"], id, number, data)


@app.post("/api/uploads/{id}/complete", status_code=202)
def complete(id: UUID, ws: Workspace):
    result = service.finalize(ws["id"], id)
    checkpoint("completion_committed", id)
    observability.event("upload_completed", file_id=str(id), status=result["status"])
    return result


@app.delete("/api/files/{id}", status_code=204)
def delete(id: UUID, ws: Workspace):
    service.delete_file(ws["id"], id)
    return Response(status_code=204)


@app.post("/api/files/{id}/retry", status_code=202)
def retry(id: UUID, ws: Workspace):
    return service.retry_processing(ws["id"], id)


@app.get("/api/files/{id}/download")
def download(id: UUID, ws: Workspace):
    return service.download(ws["id"], id)


dist = Path(__file__).resolve().parent.parent / "dist"
if dist.exists():
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
