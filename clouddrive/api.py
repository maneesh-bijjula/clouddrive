import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from . import service
from .config import settings
from .db import engine, migrate
from .models import NewUpload, PartChecksum, PartReceipt
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
    allow_headers=["Authorization", "Content-Type"],
)
bearer = HTTPBearer(auto_error=False)


def authenticated(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    return service.workspace(credentials.credentials if credentials else None)


Workspace = Annotated[dict, Depends(authenticated)]


async def infrastructure_error(request: Request, exc: Exception):
    if isinstance(exc, ClientError) and exc.response["Error"]["Code"] == "NoSuchUpload":
        return JSONResponse(
            {"detail": "This upload has expired in storage. Delete it and upload the file again."},
            status_code=409,
        )
    logger.exception("Infrastructure request failed", exc_info=exc)
    return JSONResponse(
        {"detail": "A storage service is temporarily unavailable. Your upload can be retried."},
        status_code=503,
    )


for exception in (ClientError, BotoCoreError, SQLAlchemyError):
    app.add_exception_handler(exception, infrastructure_error)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
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
    return {
        "id": ws["id"],
        "created_at": ws["created_at"],
        "max_file_bytes": settings().max_file_bytes,
        "part_bytes": settings().part_bytes,
    }


@app.get("/api/files")
def files(ws: Workspace):
    with engine().connect() as conn:
        rows = (
            service.query(
                conn,
                "SELECT * FROM files WHERE workspace_id=:workspace AND status != 'DELETED' ORDER BY created_at DESC LIMIT 200",
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
            FROM files WHERE workspace_id=:workspace AND status != 'DELETED'""",
                workspace=ws["id"],
            )
            .mappings()
            .one()
        )
        return {
            "files": [service.describe(conn, row) for row in rows],
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
    return service.finalize(ws["id"], id)


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
