"""One processing job per process; all heavy imports stay out of the HTTP host."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import warnings


class RejectedMedia(Exception):
    pass


def process_image(source: Path, output: Path, backend: str, required: bool) -> dict:
    from PIL import Image, UnidentifiedImageError
    from remove_ai_watermarks import remove_visible_detailed

    max_pixels = int(os.getenv("MAX_IMAGE_PIXELS", "40000000"))
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise RejectedMedia("Only PNG, JPEG and WebP images are supported")
                if image.width * image.height > max_pixels:
                    raise RejectedMedia("Image exceeds MAX_IMAGE_PIXELS")
                if getattr(image, "n_frames", 1) != 1:
                    raise RejectedMedia("Animated images are not supported")
                image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise RejectedMedia("Invalid image or image exceeds the pixel limit") from exc
    result = remove_visible_detailed(source, output, backend=backend, strip_metadata=True, write_noop=True)
    if result.status == "no_watermark" and required:
        raise RejectedMedia("No supported visible watermark detected; use require_watermark=false for passthrough")
    if result.status in {"partial", "unvalidated"}:
        raise RejectedMedia(f"Visible removal is {result.status}; no file returned")
    return {"status": result.status, "marks": [mark.key for mark in result.marks], "synthid_processed": False}


def process_video(source: Path, output: Path, backend: str, required: bool) -> dict:
    from remove_ai_watermarks import remove_video_all

    # Preflight before the library scans/retains per-frame detections.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
        capture_output=True, timeout=30, check=False,
    )
    if probe.returncode != 0:
        raise RejectedMedia("Cannot decode video")
    info = json.loads(probe.stdout)
    streams = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]
    if len(streams) != 1 or streams[0].get("disposition", {}).get("attached_pic"):
        raise RejectedMedia("Expected exactly one video stream, without attached cover art")
    stream = streams[0]
    duration = float(info.get("format", {}).get("duration") or stream.get("duration") or 0)
    if not math.isfinite(duration) or not 0 < duration <= float(os.getenv("MAX_VIDEO_SECONDS", "300")):
        raise RejectedMedia("Video duration is missing, invalid, or exceeds MAX_VIDEO_SECONDS")
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if min(width, height) <= 0 or width * height > int(os.getenv("MAX_VIDEO_PIXELS", "8294400")):
        raise RejectedMedia("Video dimensions are invalid or exceed MAX_VIDEO_PIXELS")
    result = remove_video_all(
        source, output, mark="veo", backend=backend, temporal_consistency=True, include_invisible=False,
    )
    if result.remaining_metadata:
        raise RuntimeError("AI metadata remains after processing")
    if not result.visible_removed_frames and required:
        raise RejectedMedia("No stable visible Veo watermark detected; use require_watermark=false for passthrough")
    return {
        # Video has no independent post-removal visual validator in this path.
        "status": "processed" if result.visible_removed_frames else "no_watermark",
        "marks": [result.visible_mark] if result.visible_mark else [],
        "total_frames": result.total_frames,
        "detected_frames": result.visible_detected_frames,
        "processed_frames": result.visible_removed_frames,
        "synthid_processed": False,
    }


def main() -> int:
    kind, source, output, report, backend, mode = sys.argv[1:]
    logging.basicConfig(level=logging.INFO)
    try:
        processor = process_image if kind == "image" else process_video
        result = processor(Path(source), Path(output), backend, mode == "required")
        code = 0
    except (RejectedMedia, ValueError) as exc:
        result = {"error": str(exc), "http_status": 422}
        code = 1
    except Exception:
        logging.exception("Processing failed")
        result = {"error": "Processing failed; inspect server logs", "http_status": 500}
        code = 1
    Path(report).write_text(json.dumps(result), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
