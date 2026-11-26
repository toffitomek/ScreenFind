"""Long-lived OCR subprocess.

Protocol: read one JSON object per line from stdin:
    {"path": "/abs/path.png", "lang": true, "level": "accurate"}

Reply one JSON object per line on stdout:
    {"ok": true,  "path": "...", "ocr_text_raw": "...", "boxes": [...]}
    {"ok": false, "path": "...", "error": "..."}

A line `{"cmd": "quit"}` causes a clean exit. EOF on stdin also exits cleanly.

This script is invoked as `python -m screenshot_search.ocr_worker_proc`.
"""

import json
import re
import sys


def _run_vision_ocr(path: str, use_language_correction: bool, recognition_level: str) -> dict:
    import Vision
    import Quartz
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise RuntimeError("cannot open image source")

    cg_image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg_image is None:
        raise RuntimeError("cannot decode image")

    request = Vision.VNRecognizeTextRequest.alloc().init()
    if recognition_level == "fast":
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
    else:
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(use_language_correction)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {})
    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError(f"Vision OCR error: {error}")

    lines = []
    boxes = []
    for obs in (request.results() or []):
        if not hasattr(obs, "topCandidates_"):
            continue
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        line_text = candidate.string()
        if not line_text:
            continue
        lines.append(line_text)

        for m in re.finditer(r"\S+", line_text):
            word = m.group()
            ns_range = (m.start(), len(word))
            try:
                # Returns (VNRectangleObservation, error); the rect is on .boundingBox().
                obs_rect, _ = candidate.boundingBoxForRange_error_(ns_range, None)
                if obs_rect is not None:
                    box_rect = obs_rect.boundingBox()
                    boxes.append({
                        "text": word,
                        "x": float(box_rect.origin.x),
                        "y": float(box_rect.origin.y),
                        "w": float(box_rect.size.width),
                        "h": float(box_rect.size.height),
                    })
            except Exception:
                pass

    return {"ocr_text_raw": "\n".join(lines), "boxes": boxes}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps({"ok": False, "path": "", "error": f"bad json: {exc}"}) + "\n")
            sys.stdout.flush()
            continue

        if msg.get("cmd") == "quit":
            return

        path = msg.get("path", "")
        try:
            result = _run_vision_ocr(
                path,
                bool(msg.get("lang", True)),
                str(msg.get("level", "accurate")),
            )
            out = {"ok": True, "path": path, **result}
        except Exception as exc:
            out = {"ok": False, "path": path, "error": str(exc)}

        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
