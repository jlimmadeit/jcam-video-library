#!/usr/bin/env python3
"""
Gemini-assisted logo banner Y position for 1080x1920 TikTok-style videos.

Pipeline: stratified frame grab → Gemini pass A (roles + boxes) → validate →
optional pass B (crop refine for conflict-band regions) → cross-frame fusion →
deterministic banner_y optimizer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

WATERMARK_TARGET_WIDTH = 1080
WATERMARK_TARGET_HEIGHT = 1920
WATERMARK_BANNER_HEIGHT = 100
WATERMARK_BANNER_POSITION = 5 / 7
MAX_BANNER_BOTTOM_Y = 1650
BOTTOM_THIRD_TOP_Y = (2 * WATERMARK_TARGET_HEIGHT) // 3
MIN_BANNER_TOP_SLACK_PX = 200
MIN_BANNER_TOP_Y = max(0, BOTTOM_THIRD_TOP_Y - MIN_BANNER_TOP_SLACK_PX)
CAPTION_BOX_INFLATE_PX = 12
CAPTION_BOX_EXTRA_PAD_TOP_PX = 22
# Extra vertical gap (px) between banner bottom and nearest text — overlap test only.
BANNER_TEXT_GAP_PX = 40
# Gemini boxes for TikTok auto-captions are often tight; outline/shadow/ascenders sit above the box.
AUTO_CAPTION_PLACEMENT_PAD_TOP_PX = 52
AUTO_CAPTION_PLACEMENT_PAD_BOTTOM_PX = 20

# Pass B: refine boxes that intersect this vertical band (full-frame Y).
CONFLICT_BAND_Y0 = 980
CONFLICT_BAND_Y1 = 1680
MAX_REFINE_CROPS = 5
CROP_MARGIN = 32

# Sampling: uniform + dense tail (last ~25% of timeline).
NUM_UNIFORM_SAMPLE_FRAMES = 10
TAIL_SAMPLE_TIME_FRACS = (0.76, 0.80, 0.84, 0.88, 0.91, 0.935, 0.96, 0.985)


def total_sample_frame_count() -> int:
    return NUM_UNIFORM_SAMPLE_FRAMES + len(TAIL_SAMPLE_TIME_FRACS)


GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

_gemini_lock = threading.Lock()
_gemini_client: genai.Client | None = None

VALID_ROLES = frozenset({"auto_caption", "creator_overlay", "ui_chrome", "unknown"})
ANCHOR_VALUES = frozenset({"bottom_stack", "mid_frame", "top", "unknown"})


def get_gemini_client() -> genai.Client | None:
    global _gemini_client
    if not GEMINI_API_KEY:
        return None
    with _gemini_lock:
        if _gemini_client is None:
            _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return _gemini_client


def probe_duration_seconds(video_path: str) -> float | None:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        d = float(json.loads(r.stdout)["format"]["duration"])
        return d if d > 0 else None
    except Exception:
        return None


def _scale_pad_vf() -> str:
    return (
        f"scale={WATERMARK_TARGET_WIDTH}:{WATERMARK_TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WATERMARK_TARGET_WIDTH}:{WATERMARK_TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
    )


def _dedupe_sorted_times(times: list[float], hi: float) -> list[float]:
    seen: set[float] = set()
    out: list[float] = []
    for t in sorted(times):
        t = min(max(t, 0.0), hi)
        k = round(t, 2)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def extract_sample_frames(
    video_path: str,
    num_uniform: int = NUM_UNIFORM_SAMPLE_FRAMES,
    tail_fracs: tuple[float, ...] = TAIL_SAMPLE_TIME_FRACS,
    out_dir: str | None = None,
) -> tuple[list[tuple[float, str]], str | None]:
    duration = probe_duration_seconds(video_path)
    if duration is None or duration <= 0:
        return [], "Could not read video duration"

    own_dir = out_dir is None
    tmp = out_dir or tempfile.mkdtemp(prefix="banner_frames_")
    os.makedirs(tmp, exist_ok=True)

    hi = max(duration - 0.05, 0.0)
    times: list[float] = []
    for i in range(num_uniform):
        t = duration * (i + 0.5) / num_uniform
        times.append(min(max(t, 0.0), hi))
    for frac in tail_fracs:
        times.append(min(max(duration * frac, 0.0), hi))
    times = _dedupe_sorted_times(times, hi)

    results: list[tuple[float, str]] = []
    vf = _scale_pad_vf()
    for i, t in enumerate(times):
        out_path = os.path.join(tmp, f"f{i:02d}.png")
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{t:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            vf,
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(out_path):
            if own_dir:
                shutil.rmtree(tmp, ignore_errors=True)
            return [], f"ffmpeg frame extract failed at t={t}: {r.stderr[:500]}"
        results.append((t, out_path))

    return results, None


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


@dataclass
class TextRegion:
    """Single detected text region (pass A or fused)."""

    x: int
    y: int
    width: int
    height: int
    frame_index: int
    role: str = "unknown"
    anchor: str | None = None
    confidence: str | None = None
    text_rough: str | None = None
    refined: bool = field(default=False, repr=False)


def _legacy_kind_to_role(kind: str) -> str:
    k = kind.lower().strip()
    if k == "caption":
        return "auto_caption"
    if k == "title":
        return "creator_overlay"
    return "unknown"


def _parse_regions_payload(data: Any) -> list[TextRegion]:
    out: list[TextRegion] = []
    if not isinstance(data, dict):
        return out
    frames = data.get("frames")
    if not isinstance(frames, list):
        return out
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        try:
            fi = int(fr.get("frame_index", 0))
        except (TypeError, ValueError):
            fi = 0
        fi = max(0, fi)
        regions = fr.get("regions")
        if not isinstance(regions, list):
            continue
        for r in regions:
            if not isinstance(r, dict):
                continue
            role = str(r.get("role", "")).lower().strip()
            if not role or role not in VALID_ROLES:
                role = _legacy_kind_to_role(str(r.get("kind", "")).lower().strip())
            if role not in VALID_ROLES:
                role = "unknown"
            anchor_raw = r.get("anchor")
            anchor: str | None = None
            if anchor_raw is not None and str(anchor_raw).strip():
                av = str(anchor_raw).lower().strip()
                anchor = av if av in ANCHOR_VALUES else "unknown"
            conf = r.get("confidence")
            if conf is not None:
                conf = str(conf).lower().strip()
            tr = r.get("text_rough")
            if tr is not None and not isinstance(tr, str):
                tr = str(tr)[:120]
            try:
                x = int(round(float(r["x"])))
                y = int(round(float(r["y"])))
                w = int(round(float(r["width"])))
                h = int(round(float(r["height"])))
            except (KeyError, TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            x = max(0, min(x, WATERMARK_TARGET_WIDTH - 1))
            y = max(0, min(y, WATERMARK_TARGET_HEIGHT - 1))
            w = min(w, WATERMARK_TARGET_WIDTH - x)
            h = min(h, WATERMARK_TARGET_HEIGHT - y)
            out.append(
                TextRegion(
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    frame_index=fi,
                    role=role,
                    anchor=anchor,
                    confidence=conf,
                    text_rough=tr if isinstance(tr, str) else None,
                )
            )
    return out


def gemini_regions_prompt_pass_a(num_frames: int, num_uniform: int) -> str:
    num_tail = num_frames - num_uniform
    return f"""You are given {num_frames} PNG images in order from a vertical TikTok/Reels video (1080x1920).
Origin is top-left; y increases downward.

Sampling: images 0–{num_uniform - 1} are evenly spaced through the clip. Images {num_uniform}–{num_frames - 1} are from the **last portion** of the clip — pay extra attention to **large stacked text** just above the TikTok UI.

For **every** image, detect on-screen **text-like** regions (burned-in only, not physical objects). For each region output:
- "role": one of:
  - "auto_caption" — karaoke / auto-captions / subtitle bar, usually bottom, smaller uniform style
  - "creator_overlay" — creator hooks, "POV:", episode lines, multi-line headlines, designed text **anywhere**
  - "ui_chrome" — likes, side icons, profile ring, tiny metrics (ignore when standalone)
  - "unknown" — if unsure
- "anchor": one of "bottom_stack" | "mid_frame" | "top" | "unknown"
- "confidence": "high" | "medium" | "low"
- "text_rough": short snippet of visible text (optional, ≤80 chars) or omit
- "x","y","width","height": integers, tight axis-aligned box in **full frame** coordinates

Rules:
- Use **one tall box** for a multi-line stacked block that belongs together. If a **hook** sits above a **second title line** (e.g. episode text or “through your …”), the box must include **both** lines so the bottom pixel covers the lowest line.
- If unsure between auto_caption vs creator_overlay for large centered lines, prefer **creator_overlay**.
- Do **not** box pure UI chrome as text unless it is clearly part of a designed graphic with words.

Output **only** valid JSON (no markdown):
{{
  "frames": [
    {{
      "frame_index": 0,
      "regions": [
        {{
          "role": "creator_overlay",
          "anchor": "mid_frame",
          "confidence": "high",
          "text_rough": "Ep:1 …",
          "x": 0, "y": 0, "width": 100, "height": 40
        }}
      ]
    }}
  ]
}}

The "frames" array must have exactly {num_frames} objects with frame_index 0 … {num_frames - 1} in order. Use "regions": [] when none.
"""


REFINE_PROMPT = """This PNG is a **crop** from a 1080×1920 vertical video frame (origin top-left of the **crop**).

Return **only** valid JSON (no markdown), one object:
{{"x": int, "y": int, "width": int, "height": int}}

The box must tightly wrap the **main text block** visible in this crop, in **crop-relative** pixels (0,0 = top-left of this image).
If there is no readable text, return {{"x": 0, "y": 0, "width": 0, "height": 0}}.
"""


def _rect_intersects_vertical_band(
    x: int, y: int, w: int, h: int, y0: int, y1: int
) -> bool:
    return y < y1 and y + h > y0


def _region_intersects_conflict_band(r: TextRegion) -> bool:
    return _rect_intersects_vertical_band(
        r.x, r.y, r.width, r.height, CONFLICT_BAND_Y0, CONFLICT_BAND_Y1
    )


def validate_regions(regions: list[TextRegion]) -> list[TextRegion]:
    """Drop absurd boxes; clamp. Removes ui_chrome (never used for banner)."""
    out: list[TextRegion] = []
    for r in regions:
        if r.role == "ui_chrome":
            continue
        if r.width < 8 or r.height < 8:
            continue
        if r.width * r.height < 220:
            continue
        # Ultra-wide thin slivers (often garbage)
        if r.width > 1000 and r.height < 14:
            continue
        if r.height > 900 and r.width < 30:
            continue
        out.append(r)
    return out


def _iou_ax(ax: int, ay: int, aw: int, ah: int, bx: int, by: int, bw: int, bh: int) -> float:
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = ix1 - ix0, iy1 - iy0
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _should_merge_regions(a: TextRegion, b: TextRegion) -> bool:
    if a.frame_index == b.frame_index:
        return _iou_ax(a.x, a.y, a.width, a.height, b.x, b.y, b.width, b.height) > 0.35
    iou = _iou_ax(a.x, a.y, a.width, a.height, b.x, b.y, b.width, b.height)
    if iou > 0.08:
        return True
    # Same vertical column, similar width (stacked lines across cuts)
    axc = a.x + a.width / 2
    bxc = b.x + b.width / 2
    if abs(axc - bxc) < 120 and abs(a.y - b.y) < 140:
        h_overlap = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
        if h_overlap > 0.25 * min(a.height, b.height):
            return True
    return False


def fuse_text_regions(regions: list[TextRegion]) -> list[TextRegion]:
    """Merge duplicate detections across frames into conservative envelopes."""
    if not regions:
        return []
    n = len(regions)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if _should_merge_regions(regions[i], regions[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(i)

    fused: list[TextRegion] = []
    role_rank = {"auto_caption": 3, "creator_overlay": 2, "unknown": 1, "ui_chrome": 0}

    for _root, idxs in groups.items():
        xs = [regions[i].x for i in idxs]
        ys = [regions[i].y for i in idxs]
        xe = [regions[i].x + regions[i].width for i in idxs]
        ye = [regions[i].y + regions[i].height for i in idxs]
        x0, y0, x1, y1 = min(xs), min(ys), max(xe), max(ye)
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        x0 = max(0, min(x0, WATERMARK_TARGET_WIDTH - 1))
        y0 = max(0, min(y0, WATERMARK_TARGET_HEIGHT - 1))
        w = min(w, WATERMARK_TARGET_WIDTH - x0)
        h = min(h, WATERMARK_TARGET_HEIGHT - y0)

        best_role = "unknown"
        for i in idxs:
            rr = regions[i].role
            if role_rank.get(rr, 0) > role_rank.get(best_role, 0):
                best_role = rr
        anchors = [regions[i].anchor for i in idxs if regions[i].anchor]
        anchor = "bottom_stack" if "bottom_stack" in anchors else (
            anchors[0] if anchors else "unknown"
        )
        fused.append(
            TextRegion(
                x=x0,
                y=y0,
                width=w,
                height=h,
                frame_index=-1,
                role=best_role,
                anchor=anchor,
                confidence="high",
                text_rough=None,
            )
        )
    return fused


def expand_obstacles_for_lower_titles(regions: list[TextRegion]) -> list[TextRegion]:
    """
    Vertically inflate boxes before overlap placement.
    Creator/unknown: second-line stacks and mid-frame hooks.
    auto_caption: tight boxes omit outline/shadow above glyphs — pad top so the bar clears visually.
    """
    out: list[TextRegion] = []
    for r in regions:
        if r.role == "ui_chrome":
            continue
        cy = r.y + r.height / 2.0
        bottom = r.y + r.height
        pad_l = pad_r = pad_t = pad_b = 0
        if r.role == "creator_overlay":
            if cy > 920 or (r.anchor == "mid_frame" and bottom > 820):
                pad_t, pad_b, pad_l, pad_r = 88, 110, 28, 28
            elif bottom > 1000:
                pad_t, pad_b, pad_l, pad_r = 48, 72, 16, 16
            # Hook stacks: model often boxes only the top lines; second line sits above the banner.
            if r.anchor == "mid_frame" and bottom < 1280:
                target_bot = min(WATERMARK_TARGET_HEIGHT - 1, 1400)
                pad_b = max(pad_b, target_bot - bottom)
        elif r.role == "unknown" and bottom > 980:
            pad_t, pad_b, pad_l, pad_r = 48, 72, 16, 16
        elif r.role == "auto_caption" and bottom > 820:
            pad_t = AUTO_CAPTION_PLACEMENT_PAD_TOP_PX
            pad_b = AUTO_CAPTION_PLACEMENT_PAD_BOTTOM_PX
            pad_l = pad_r = CAPTION_BOX_INFLATE_PX
        if pad_t == 0 and pad_b == 0:
            out.append(r)
            continue
        x0 = max(0, r.x - pad_l)
        y0 = max(0, r.y - pad_t)
        x1 = min(WATERMARK_TARGET_WIDTH, r.x + r.width + pad_r)
        y1 = min(WATERMARK_TARGET_HEIGHT, r.y + r.height + pad_b)
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        out.append(
            TextRegion(
                x=x0,
                y=y0,
                width=w,
                height=h,
                frame_index=r.frame_index,
                role=r.role,
                anchor=r.anchor,
                confidence=r.confidence,
                text_rough=r.text_rough,
                refined=r.refined,
            )
        )
    return out


def _ffmpeg_crop_png(src: str, x: int, y: int, w: int, h: int, dest: str) -> bool:
    vf = f"crop={w}:{h}:{x}:{y}"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vf", vf, "-frames:v", "1", dest],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and os.path.isfile(dest)


def _gemini_json_object(client: genai.Client, prompt: str, image_path: str) -> dict | None:
    with open(image_path, "rb") as f:
        raw = f.read()
    parts = [
        types.Part.from_bytes(data=raw, mime_type="image/png"),
        types.Part.from_text(text=prompt),
    ]
    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=parts)],
            )
            text = (response.text or "").strip()
            if not text:
                continue
            return json.loads(_strip_json_fence(text))
        except Exception:
            continue
    return None


def refine_regions_with_crops(
    frame_paths: list[str],
    regions: list[TextRegion],
) -> tuple[list[TextRegion], int]:
    """
    Pass B: up to MAX_REFINE_CROPS conflict-band regions get a tight re-box on a crop.
    At most one refinement per frame index (largest qualifying box).
    """
    client = get_gemini_client()
    if not client:
        return regions, 0

    candidates: list[tuple[float, TextRegion]] = []
    for r in regions:
        if r.role == "ui_chrome":
            continue
        if not _region_intersects_conflict_band(r):
            continue
        if r.frame_index < 0 or r.frame_index >= len(frame_paths):
            continue
        candidates.append((float(r.width * r.height), r))

    candidates.sort(key=lambda t: -t[0])
    pairs: list[tuple[TextRegion, TextRegion]] = []
    used_frames: set[int] = set()
    crop_idx = 0

    tmp = tempfile.mkdtemp(prefix="banner_refine_")
    try:
        for _area, r in candidates:
            if len(pairs) >= MAX_REFINE_CROPS:
                break
            if r.frame_index in used_frames:
                continue
            fi = r.frame_index
            src = frame_paths[fi]
            cx0 = max(0, r.x - CROP_MARGIN)
            cy0 = max(0, r.y - CROP_MARGIN)
            cx1 = min(WATERMARK_TARGET_WIDTH, r.x + r.width + CROP_MARGIN)
            cy1 = min(WATERMARK_TARGET_HEIGHT, r.y + r.height + CROP_MARGIN)
            cw, ch = cx1 - cx0, cy1 - cy0
            if cw < 40 or ch < 24:
                continue
            crop_path = os.path.join(tmp, f"crop_{fi}_{crop_idx}.png")
            crop_idx += 1
            if not _ffmpeg_crop_png(src, cx0, cy0, cw, ch, crop_path):
                continue
            data = _gemini_json_object(client, REFINE_PROMPT, crop_path)
            if not isinstance(data, dict):
                continue
            try:
                rx = int(round(float(data["x"])))
                ry = int(round(float(data["y"])))
                rw = int(round(float(data["width"])))
                rh = int(round(float(data["height"])))
            except (KeyError, TypeError, ValueError):
                continue
            if rw < 4 or rh < 4:
                continue
            fx = cx0 + rx
            fy = cy0 + ry
            fw = min(rw, WATERMARK_TARGET_WIDTH - max(0, fx))
            fh = min(rh, WATERMARK_TARGET_HEIGHT - max(0, fy))
            fx = max(0, min(fx, WATERMARK_TARGET_WIDTH - 1))
            fy = max(0, min(fy, WATERMARK_TARGET_HEIGHT - 1))
            fw = min(fw, WATERMARK_TARGET_WIDTH - fx)
            fh = min(fh, WATERMARK_TARGET_HEIGHT - fy)
            old_area = float(max(1, r.width * r.height))
            new_area = float(max(1, fw * fh))
            iou_rn = _iou_ax(r.x, r.y, r.width, r.height, fx, fy, fw, fh)
            # Reject refinements that throw away most of the text (common pass-B failure).
            if new_area < 0.22 * old_area and iou_rn < 0.12:
                continue
            nr = TextRegion(
                x=fx,
                y=fy,
                width=fw,
                height=fh,
                frame_index=fi,
                role=r.role,
                anchor=r.anchor,
                confidence="high",
                text_rough=r.text_rough,
                refined=True,
            )
            pairs.append((r, nr))
            used_frames.add(fi)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not pairs:
        return regions, 0

    old_ids = {id(o) for o, _ in pairs}
    out = [r for r in regions if id(r) not in old_ids]
    out.extend(n for _, n in pairs)
    return out, len(pairs)


def analyze_text_regions_pass_a(
    frame_paths: list[str],
    log_prefix: str = "",
) -> tuple[list[TextRegion], str | None]:
    client = get_gemini_client()
    if not client:
        return [], "GEMINI_API_KEY not set"

    n = len(frame_paths)
    if n < 1:
        return [], "No frame images to analyze"

    parts: list[Any] = []
    for i, p in enumerate(frame_paths):
        with open(p, "rb") as f:
            raw = f.read()
        parts.append(types.Part.from_text(text=f"--- Frame {i} (image {i + 1} of {n}) ---"))
        parts.append(types.Part.from_bytes(data=raw, mime_type="image/png"))
    parts.append(
        types.Part.from_text(
            text=gemini_regions_prompt_pass_a(n, NUM_UNIFORM_SAMPLE_FRAMES),
        )
    )

    last_err: str | None = None
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                )
                text = (response.text or "").strip()
                if not text:
                    last_err = "Empty Gemini response"
                    continue
                cleaned = _strip_json_fence(text)
                data = json.loads(cleaned)
                regions = _parse_regions_payload(data)
                return regions, None
            except json.JSONDecodeError as e:
                last_err = f"JSON parse error: {e}"
            except Exception as e:
                last_err = str(e)
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    import time

                    time.sleep(30 * (attempt + 1))
                else:
                    break

    return [], last_err or "Gemini pass A failed"


def _rects_overlap(ax: int, ay: int, aw: int, ah: int, bx: int, by: int, bw: int, bh: int) -> bool:
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def default_banner_y() -> int:
    return int(WATERMARK_TARGET_HEIGHT * WATERMARK_BANNER_POSITION)


def compute_banner_y_avoiding_captions(
    regions: list[TextRegion],
    banner_height: int = WATERMARK_BANNER_HEIGHT,
    max_bottom_y: int = MAX_BANNER_BOTTOM_Y,
    preferred_y: int | None = None,
    min_top_y: int = MIN_BANNER_TOP_Y,
) -> int:
    pref = preferred_y if preferred_y is not None else default_banner_y()
    cap_top = max_bottom_y - banner_height
    if cap_top < 0:
        return max(0, min_top_y)

    min_y_bound = max(0, min(min_top_y, cap_top))
    span_hi = cap_top + banner_height

    rects: list[tuple[int, int, int, int]] = []

    def add_obstacle(x: int, y: int, w: int, h: int) -> None:
        rects.append((x, y, w, h))

    for r in regions:
        if r.role == "ui_chrome":
            continue
        bottom = r.y + r.height
        if r.role == "auto_caption":
            add_obstacle(r.x, r.y, r.width, r.height)
        elif r.role == "creator_overlay":
            if bottom > 480 and r.y < span_hi + 160:
                add_obstacle(r.x, r.y, r.width, r.height)
        elif r.role == "unknown":
            if r.anchor == "bottom_stack" or bottom > 920 or _region_intersects_conflict_band(r):
                add_obstacle(r.x, r.y, r.width, r.height)

    only_caps = [r for r in regions if r.role == "auto_caption"]
    if only_caps:
        deepest = max(r.y + r.height for r in only_caps)
        if deepest < 1150 and len(only_caps) >= 2:
            add_obstacle(0, 1240, WATERMARK_TARGET_WIDTH, 520)

    pad = CAPTION_BOX_INFLATE_PX
    pad_top = pad + CAPTION_BOX_EXTRA_PAD_TOP_PX
    gap = BANNER_TEXT_GAP_PX

    def overlaps(y: int) -> bool:
        for x, yy, w, h in rects:
            bx = max(0, x - pad)
            by = max(0, yy - pad_top - gap)
            bw = min(WATERMARK_TARGET_WIDTH - bx, w + 2 * pad)
            bh = min(WATERMARK_TARGET_HEIGHT - by, h + pad_top + pad + gap)
            if _rects_overlap(0, y, WATERMARK_TARGET_WIDTH, banner_height, bx, by, bw, bh):
                return True
        return False

    min_y = min_y_bound
    y = min(pref, cap_top)
    y = max(y, min_y)
    while y >= min_y and overlaps(y):
        y -= 1
    return max(min_y, y)


def _region_to_meta(r: TextRegion) -> dict[str, Any]:
    return {
        "x": r.x,
        "y": r.y,
        "width": r.width,
        "height": r.height,
        "frame_index": r.frame_index,
        "role": r.role,
        "anchor": r.anchor,
        "confidence": r.confidence,
        "text_rough": r.text_rough,
        "refined": r.refined,
    }


def compute_banner_y_for_logod_video(
    video_path: str,
    log_prefix: str = "",
) -> tuple[int, dict[str, Any]]:
    meta: dict[str, Any] = {
        "frames": [],
        "regions_pass_a": [],
        "regions_fused": [],
        "refine_count": 0,
        "gemini_error": None,
        "ffmpeg_error": None,
        "max_banner_bottom_y": MAX_BANNER_BOTTOM_Y,
        "min_banner_top_y": MIN_BANNER_TOP_Y,
        "bottom_third_top_y": BOTTOM_THIRD_TOP_Y,
        "banner_text_gap_px": BANNER_TEXT_GAP_PX,
    }

    frames, err = extract_sample_frames(video_path)
    if err:
        meta["ffmpeg_error"] = err
        y = default_banner_y()
        meta["banner_y"] = y
        meta["banner_position_fraction"] = round(y / WATERMARK_TARGET_HEIGHT, 4)
        return y, meta

    tmp_dir = os.path.dirname(frames[0][1]) if frames else None
    try:
        meta["sample_frame_count"] = len(frames)
        meta["frames"] = [{"time_sec": t} for t, _p in frames]
        paths = [p for _, p in frames]

        raw, gerr = analyze_text_regions_pass_a(paths, log_prefix)
        meta["regions_pass_a"] = [_region_to_meta(r) for r in raw]
        if gerr:
            meta["gemini_error"] = gerr

        if gerr or not raw:
            y = default_banner_y()
            y = compute_banner_y_avoiding_captions([], preferred_y=y)
            meta["regions"] = []
            meta["regions_fused"] = []
        else:
            vld = validate_regions(raw)
            refined, rc = refine_regions_with_crops(paths, vld)
            meta["refine_count"] = rc
            fused = fuse_text_regions(refined)
            expanded = expand_obstacles_for_lower_titles(fused)
            meta["regions_fused"] = [_region_to_meta(r) for r in expanded]
            meta["regions"] = meta["regions_fused"]
            y = compute_banner_y_avoiding_captions(expanded, preferred_y=default_banner_y())

        meta["banner_y"] = y
        meta["banner_position_fraction"] = round(y / WATERMARK_TARGET_HEIGHT, 4)
        return y, meta
    finally:
        if tmp_dir and os.path.isdir(tmp_dir) and "banner_frames_" in os.path.basename(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# Backwards-compatible name
def analyze_text_regions_with_gemini(
    frame_paths: list[str],
    log_prefix: str = "",
) -> tuple[list[TextRegion], str | None]:
    return analyze_text_regions_pass_a(frame_paths, log_prefix)
