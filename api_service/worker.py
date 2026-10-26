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
    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report


def process_image(source: Path, output: Path, backend: str, required: bool, options: dict) -> dict:
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
    if options.get("region") is not None or options.get("detection_mode", "gemini") == "gemini":
        return process_gemini_image(source, output, backend, required, options)
    result = remove_visible_detailed(source, output, backend=backend, strip_metadata=True, write_noop=True)
    if result.status == "no_watermark" and required:
        raise RejectedMedia(
            "No supported visible watermark detected; try detection_mode=gemini or supply region=x,y,width,height",
            {"status": result.status, "detection_mode": "auto"},
        )
    if result.status in {"partial", "unvalidated"}:
        raise RejectedMedia(f"Visible removal is {result.status}; no file returned", {
            "status": result.status,
            "marks": [{"key": mark.key, "region": mark.region, "status": mark.status,
                       "confidence_before": mark.confidence_before,
                       "confidence_after": mark.confidence_after} for mark in result.marks],
        })
    return {"status": result.status, "marks": [mark.key for mark in result.marks], "synthid_processed": False}


def process_gemini_image(source: Path, output: Path, backend: str, required: bool, options: dict) -> dict:
    """Scope detection to Gemini; permit a caller-specified box for missed marks.

    The registry's provenance=True flag selects its provider-confirmed gate. Here
    the evidence is explicitly the caller's Gemini mode, not fabricated metadata.
    No standard-corner force fallback is used on a negative detection.
    """
    from remove_ai_watermarks import image_io
    from remove_ai_watermarks.metadata import strip_written_output
    from remove_ai_watermarks.region_eraser import boxes_to_mask
    from remove_ai_watermarks.watermark_registry import fill, get_mark

    image, alpha = image_io.read_bgr_and_alpha(source)
    if image is None:
        raise RejectedMedia("Cannot decode image")
    height, width = image.shape[:2]
    mark = get_mark("gemini")
    before = mark.detect(image, provenance=True)
    report = {
        "status": "no_watermark", "marks": [], "synthid_processed": False,
        "detection_mode": "gemini", "provider_hint": "caller",
        "confidence_before": before.confidence,
        "candidate_region": list(before.region),
        "image_size": [width, height], "backend": backend,
    }
    explicit_region = options.get("region")
    if explicit_region is not None:
        x, y, w, h = explicit_region
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
            raise RejectedMedia("region must fit within the decoded image dimensions", report)
        box = (x, y, w, h)
        mask = boxes_to_mask((height, width), [box], dilate=0)
        report["detection_mode"] = "manual_region"
        report["provider_hint"] = "not_used"
    elif before.detected:
        localization = mark.localize(image, provenance=True, detection=before)
        mask = localization.mask
        box = before.region
    else:
        if required:
            raise RejectedMedia(
                "Gemini sparkle not detected. Supply region=x,y,width,height for an explicit erase. "
                "require_watermark=false only strips metadata; it does not force removal.", report,
            )
        mask = None
        box = None

    result = image
    if box is not None:
        if mask is None or not mask.any():
            raise RejectedMedia("Detector did not produce a usable erase mask", report)
        result = fill(image, mask, backend=backend)
        after = mark.detect(result, provenance=True)
        report.update({"region": list(box), "confidence_after": after.confidence,
                       "marks": ["gemini"] if explicit_region is None else [],
                       "status": "cleaned" if explicit_region is None else "processed"})
        ax, ay, aw, ah = after.region
        x, y, w, h = box
        if after.detected and ax < x + w and x < ax + aw and ay < y + h and y < ay + ah:
            report["status"] = "partial"
            raise RejectedMedia("Gemini sparkle is still detected in the erased region; no file returned", report)

    if not image_io.write_bgr_with_alpha(
        output, result, alpha, display_tags_from=source, orientation_applied=False,
    ):
        raise RuntimeError("Failed to write processed image")
    if strip_written_output(output):
        raise RuntimeError("AI metadata remains after processing")
    return report


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
    kind, source, output, report, backend, mode, options_json = sys.argv[1:]
    logging.basicConfig(level=logging.INFO)
    try:
        if kind == "image":
            result = process_image(Path(source), Path(output), backend, mode == "required", json.loads(options_json))
        else:
            result = process_video(Path(source), Path(output), backend, mode == "required")
        code = 0
    except RejectedMedia as exc:
        result = {"error": str(exc), "http_status": 422}
        if exc.report is not None:
            result["report"] = exc.report
        code = 1
    except ValueError as exc:
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
