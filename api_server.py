from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import main as batch_main

ARTIFACT_NAMES = [
    "summary",
    "resolutions",
    "escalations",
    "dead_letter_queue",
    "audit_log",
]


@dataclass(frozen=True)
class AppSettings:
    host: str
    port: int
    log_level: str
    backend_api_key: str
    allowed_hosts: list[str]
    cors_origins: list[str]
    max_parallel_jobs: int


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=10, ge=1, le=50)
    confidence_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    model: str = Field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-1.5-flash"))
    write_latest_outputs: bool = True
    tickets: list[dict[str, Any]] | None = None


class JobSummaryResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    ticket_count: int


class JobDetailResponse(JobSummaryResponse):
    config: dict[str, Any]
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    timestamp: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_csv_env(value: str, default: str) -> list[str]:
    raw = (value or default).strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_settings() -> AppSettings:
    port_raw = os.getenv("API_PORT", "8011").strip()
    workers_raw = os.getenv("MAX_PARALLEL_JOBS", "2").strip()

    try:
        port = int(port_raw)
    except ValueError:
        port = 8011

    try:
        max_parallel_jobs = max(1, int(workers_raw))
    except ValueError:
        max_parallel_jobs = 2

    return AppSettings(
        host=os.getenv("API_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=port,
        log_level=(os.getenv("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
        backend_api_key=os.getenv("BACKEND_API_KEY", "").strip(),
        allowed_hosts=parse_csv_env(os.getenv("ALLOWED_HOSTS", "*"), "*"),
        cors_origins=parse_csv_env(os.getenv("CORS_ORIGINS", "*"), "*"),
        max_parallel_jobs=max_parallel_jobs,
    )


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


class JobStore:
    def __init__(self, project_root: Path, max_workers: int):
        self.project_root = project_root
        self.jobs_root = self.project_root / "outputs" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job-worker")
        self._jobs: dict[str, dict[str, Any]] = {}

    def _default_ticket_count(self) -> int:
        tickets_path = self.project_root / "tickets.json"
        if not tickets_path.exists():
            return 0
        with tickets_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return len(loaded) if isinstance(loaded, list) else 0

    def create_job(self, request: JobCreateRequest) -> dict[str, Any]:
        job_id = uuid4().hex
        created_at = now_iso()

        ticket_count = len(request.tickets) if request.tickets is not None else self._default_ticket_count()
        job_dir = self.jobs_root / job_id
        artifacts_dir = job_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        record = {
            "job_id": job_id,
            "status": "queued",
            "created_at": created_at,
            "started_at": None,
            "completed_at": None,
            "ticket_count": ticket_count,
            "config": request.model_dump(),
            "error": None,
            "artifacts_dir": artifacts_dir,
        }

        with self._lock:
            self._jobs[job_id] = record

        self._executor.submit(self._run_job, job_id)
        return self._public_job_view(record)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [self._public_job_view(record) for record in self._jobs.values()]
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise KeyError(job_id)
            return self._public_job_view(record)

    def read_artifact(self, job_id: str, artifact_name: str) -> Any:
        if artifact_name not in ARTIFACT_NAMES and artifact_name != "error":
            raise KeyError(f"Unsupported artifact: {artifact_name}")

        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise KeyError(job_id)
            artifact_path: Path = record["artifacts_dir"] / f"{artifact_name}.json"

        if not artifact_path.exists():
            raise FileNotFoundError(str(artifact_path))

        with artifact_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _run_job(self, job_id: str) -> None:
        logger = logging.getLogger("shopwave.job")

        with self._lock:
            record = self._jobs[job_id]
            record["status"] = "running"
            record["started_at"] = now_iso()
            config = dict(record["config"])
            artifacts_dir: Path = record["artifacts_dir"]

        job_dir = artifacts_dir.parent
        tickets_path = self.project_root / "tickets.json"

        if config.get("tickets") is not None:
            tickets_path = job_dir / "tickets.custom.json"
            with tickets_path.open("w", encoding="utf-8") as handle:
                json.dump(config["tickets"], handle, indent=2, ensure_ascii=False)

        args = argparse.Namespace(
            tickets=str(tickets_path),
            out_dir=str(artifacts_dir),
            model=config.get("model", os.getenv("GEMINI_MODEL", "gemini-1.5-flash")),
            max_concurrency=int(config.get("max_concurrency", 10)),
            confidence_threshold=float(config.get("confidence_threshold", 0.65)),
            api_key=os.getenv("GEMINI_API_KEY"),
        )

        try:
            asyncio.run(batch_main.run(args))

            if bool(config.get("write_latest_outputs", True)):
                latest_root = self.project_root / "outputs"
                latest_root.mkdir(parents=True, exist_ok=True)
                for name in ARTIFACT_NAMES:
                    source = artifacts_dir / f"{name}.json"
                    if source.exists():
                        shutil.copy2(source, latest_root / f"{name}.json")

            with self._lock:
                record = self._jobs[job_id]
                record["status"] = "completed"
                record["completed_at"] = now_iso()

            logger.info("Job %s completed", job_id)

        except Exception as exc:  # pragma: no cover
            logger.exception("Job %s failed", job_id)
            error_payload = {
                "job_id": job_id,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": now_iso(),
            }
            with (artifacts_dir / "error.json").open("w", encoding="utf-8") as handle:
                json.dump(error_payload, handle, indent=2, ensure_ascii=False)

            with self._lock:
                record = self._jobs[job_id]
                record["status"] = "failed"
                record["completed_at"] = now_iso()
                record["error"] = str(exc)

    def _public_job_view(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": record["job_id"],
            "status": record["status"],
            "created_at": record["created_at"],
            "started_at": record["started_at"],
            "completed_at": record["completed_at"],
            "ticket_count": record["ticket_count"],
            "config": record["config"],
            "error": record.get("error"),
        }


def create_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="ShopWave Support Backend API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    if settings.allowed_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.job_store = JobStore(project_root=Path(__file__).resolve().parent, max_workers=settings.max_parallel_jobs)

    logger = logging.getLogger("shopwave.api")

    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id", uuid4().hex)
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:  # pragma: no cover
            logger.exception("Unhandled request error request_id=%s path=%s", request_id, request.url.path)
            raise

        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["x-request-id"] = request_id
        logger.info(
            "request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    async def require_api_key(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
        expected = app.state.settings.backend_api_key
        if not expected:
            return
        if x_api_key != expected:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="shopwave-support-backend",
            version=app.version,
            environment=os.getenv("ENVIRONMENT", "production"),
            timestamp=now_iso(),
        )

    @app.post("/api/v1/jobs", response_model=JobSummaryResponse, dependencies=[Depends(require_api_key)])
    async def create_job(request: JobCreateRequest) -> JobSummaryResponse:
        record = app.state.job_store.create_job(request)
        return JobSummaryResponse(**record)

    @app.get("/api/v1/jobs", response_model=list[JobSummaryResponse], dependencies=[Depends(require_api_key)])
    async def list_jobs() -> list[JobSummaryResponse]:
        jobs = app.state.job_store.list_jobs()
        return [JobSummaryResponse(**job) for job in jobs]

    @app.get("/api/v1/jobs/{job_id}", response_model=JobDetailResponse, dependencies=[Depends(require_api_key)])
    async def get_job(job_id: str) -> JobDetailResponse:
        try:
            record = app.state.job_store.get_job(job_id)
        except KeyError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from None
        return JobDetailResponse(**record)

    @app.get(
        "/api/v1/jobs/{job_id}/artifacts/{artifact_name}",
        dependencies=[Depends(require_api_key)],
    )
    async def get_artifact(job_id: str, artifact_name: str):
        try:
            payload = app.state.job_store.read_artifact(job_id, artifact_name)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Artifact not available yet",
            ) from None

        return JSONResponse(content=payload)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = app.state.settings
    uvicorn.run(
        "api_server:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
