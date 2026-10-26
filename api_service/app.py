"""Synchronous file-in/file-out API; processing runs in disposable subprocesses."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from starlette.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("watermark-api")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "512")) * 1024 * 1024
PROCESS_TIMEOUT = int(os.getenv("PROCESS_TIMEOUT_SECONDS", "1800"))
UPLOAD_IDLE_TIMEOUT = int(os.getenv("UPLOAD_IDLE_TIMEOUT_SECONDS", "60"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
WORK_DIR = Path(os.getenv("WORK_DIR", "/work"))
BACKEND = os.getenv("WATERMARK_BACKEND", "cv2")
if min(MAX_UPLOAD_BYTES, PROCESS_TIMEOUT, UPLOAD_IDLE_TIMEOUT, MAX_CONCURRENT) <= 0:
    raise RuntimeError("Upload, timeout and concurrency limits must be positive")
if BACKEND not in {"cv2", "lama", "migan"}:
    raise RuntimeError("WATERMARK_BACKEND must be cv2, lama or migan")
WORK_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}


class UploadGuard:
    """Bound the entire multipart body before it is spooled.

    One semaphore covers upload, processing and download. Use exactly one Uvicorn
    worker: these admission counters are intentionally local to that process.
    """

    def __init__(self, app: ASGIApp):
        self.app = app
        self.slots = asyncio.Semaphore(MAX_CONCURRENT)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
            return
        # Allow a small multipart envelope on top of the file limit.
        body_limit = MAX_UPLOAD_BYTES + 1024 * 1024
        if content_length < 0 or content_length > body_limit:
            await JSONResponse({"detail": "Upload too large"}, status_code=413)(scope, receive, send)
            return
        if self.slots.locked():
            await JSONResponse(
                {"detail": "Server busy; retry later"}, status_code=503, headers={"Retry-After": "10"}
            )(scope, receive, send)
            return
        # No await separates locked() and acquire(), so admission cannot race
        # within the single event loop when a slot is available.
        await self.slots.acquire()
        received = 0
        body_complete = False

        async def limited_receive():
            nonlocal received, body_complete
            if body_complete:
                return await receive()
            try:
                message = await asyncio.wait_for(receive(), timeout=UPLOAD_IDLE_TIMEOUT)
            except TimeoutError as exc:
                raise HTTPException(408, "Upload stalled") from exc
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise HTTPException(413, "Upload too large")
                body_complete = not message.get("more_body", False)
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            self.slots.release()


app = FastAPI(
    title="Gemini / Veo Visible Watermark API",
    version="1.0.0",
    description="Remove visible marks and AI metadata. Hidden SynthID is not processed.",
)
app.add_middleware(UploadGuard)


@app.get("/healthz", tags=["health"])
async def health():
    return {"status": "ok", "backend": BACKEND, "scope": "visible-and-metadata"}


async def run_worker(kind: str, source: Path, output: Path, directory: Path, required: bool) -> dict:
    result_path = directory / "result.json"
    with (directory / "worker.log").open("wb") as worker_log:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "api_service.worker", kind, str(source), str(output),
            str(result_path), BACKEND, "required" if required else "optional",
            stdout=worker_log, stderr=worker_log, start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=PROCESS_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError) as exc:
            # Include ffmpeg/ffprobe descendants, including when the worker exits
            # just before the timeout fires. The container runs Linux.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(504, "Processing timed out") from exc
    if not result_path.is_file():
        log_worker_failure(directory)
        log.error("Worker failed without a result, exit=%s", process.returncode)
        raise HTTPException(500, "Processing failed; inspect server logs and resource limits")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if process.returncode != 0 or "error" in result:
        log_worker_failure(directory)
        log.warning("Processing failed: %s", result)
        raise HTTPException(result.get("http_status", 500), result.get("error", "Processing failed"))
    if not output.is_file() or output.stat().st_size == 0:
        raise HTTPException(500, "Processor did not produce an output file")
    return result


def log_worker_failure(directory: Path) -> None:
    """Expose bounded worker diagnostics before its temporary directory is deleted."""
    with (directory / "worker.log").open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 8192))
        log.error("Worker diagnostics: %s", stream.read().decode("utf-8", errors="replace"))


async def clean_file(file: UploadFile, kind: str, require_watermark: bool) -> FileResponse:
    suffix = Path(file.filename or "").suffix.lower()
    allowed = IMAGE_SUFFIXES if kind == "image" else VIDEO_TYPES.keys()
    if suffix not in allowed:
        await file.close()
        raise HTTPException(415, f"Supported {kind} extensions: {', '.join(sorted(allowed))}")
    # Keep the context alive until FileResponse finishes sending. A background
    # cleanup alone would miss exceptions, cancellations and failed downloads.
    temp = TemporaryDirectory(prefix="request-", dir=WORK_DIR)
    try:
        directory = Path(temp.name)
        source = directory / f"input{suffix}"
        output = directory / ("clean.png" if kind == "image" else f"clean{suffix}")
        size = 0
        with source.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File too large")
                destination.write(chunk)
        await file.close()
        if size == 0:
            raise HTTPException(400, "Empty upload")
        result = await run_worker(kind, source, output, directory, require_watermark)

        class TemporaryFileResponse(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    temp.cleanup()

        return TemporaryFileResponse(
            output, media_type="image/png" if kind == "image" else VIDEO_TYPES[suffix],
            filename=output.name,
            headers={
                "X-Watermark-Status": result["status"],
                "X-Watermark-Report": json.dumps(result, ensure_ascii=True, separators=(",", ":")),
                "X-SynthID-Processed": "false",
                "Cache-Control": "no-store",
            },
        )
    except BaseException:
        temp.cleanup()
        raise
    finally:
        await file.close()


FILE_RESPONSES = {
    200: {
        "description": "Processed media file; inspect X-Watermark-Report for detection results.",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    },
    413: {"description": "Upload too large"},
    415: {"description": "Unsupported extension or content"},
    422: {"description": "Invalid media, no mark detected, or incomplete image removal"},
    503: {"description": "All processing slots are occupied"},
    504: {"description": "Processing timeout"},
}


@app.post("/v1/remove/image", tags=["remove"], responses=FILE_RESPONSES, response_class=FileResponse)
async def remove_image(
    file: Annotated[UploadFile, File(description="Gemini / Nano Banana PNG, JPEG or WebP")],
    require_watermark: Annotated[bool, Form(description="Fail if no known visible mark is found")] = True,
):
    """Return a PNG with known visible marks and AI metadata removed."""
    return await clean_file(file, "image", require_watermark)


@app.post("/v1/remove/video", tags=["remove"], responses=FILE_RESPONSES, response_class=FileResponse)
async def remove_video(
    file: Annotated[UploadFile, File(description="Veo MP4, MOV or M4V")],
    require_watermark: Annotated[bool, Form(description="Fail if no stable Veo mark is found")] = True,
):
    """Return the same video container with visible Veo marks and AI metadata removed."""
    return await clean_file(file, "video", require_watermark)
