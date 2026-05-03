#!/usr/bin/env python3
"""
Create or refresh jcam logod (banner) versions in Supabase + Mux.

Default: videos with no logod row yet — Mux MP4 first, TikTok fallback, then
`banner_placement.compute_banner_y_for_logod_video` + watermark + Mux upload.

Re-backfill: `python backfill_logod_videos.py --rebackfill-all` — every existing
`logod_videos` row is re-built from source (same download order), placement
re-run with the current algorithm, new Mux asset, and the same DB row updated.
After a partial run, skip rows that already updated:  
`--rebackfill-all --skip-logod-ids-file=rebackfill_skip_logod_ids.txt`  
(one logod_videos.id UUID per line; lines starting with `#` ignored).
Optional: `--workers=N` (default: a few threads, capped at 8; raise/lower if Gemini rate-limits).
"""

import base64
import json
import os
import subprocess
import tempfile
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

from banner_placement import compute_banner_y_for_logod_video
from jcam_mux_upload import upload_to_mux_direct

RAPID_API_KEY = os.getenv("RAPID_API_KEY")
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SERVICE_ROLE_SECRET")

TIKTOK_MAX_QUALITY_HOST = "tiktok-max-quality.p.rapidapi.com"
TIKTOK_MAX_QUALITY_URL = f"https://{TIKTOK_MAX_QUALITY_HOST}/download"

# Watermark settings
LOGO_PATH = os.path.join(os.path.dirname(__file__), "j cam logo black.png")
WATERMARK_BANNER_HEIGHT = 100
WATERMARK_BANNER_POSITION = 5 / 7
WATERMARK_PADDING = 105
WATERMARK_TARGET_WIDTH = 1080
WATERMARK_TARGET_HEIGHT = 1920
WATERMARK_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Black.ttf"


def default_backfill_workers() -> int:
    """Parallel workers for download / Gemini / ffmpeg / Mux (bounded to limit API pressure)."""
    n = os.cpu_count() or 4
    return min(8, max(3, n))


print_lock = threading.Lock()
_thread_local = threading.local()


def get_supabase() -> Client:
    if not hasattr(_thread_local, "supabase"):
        _thread_local.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _thread_local.supabase


def thread_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def get_videos_without_logod() -> list[dict]:
    """Get all videos that don't have a logod version yet (including hidden ones)."""
    supabase = get_supabase()
    
    # Get all video IDs that already have logod versions (including hidden)
    logod_response = supabase.table("logod_videos").select("video_id").execute()
    logod_video_ids = {row["video_id"] for row in logod_response.data}
    
    # Get all videos (include mux_playback_id for MP4 fallback)
    videos_response = supabase.table("videos").select("id, tiktok_video_id, tiktok_author_username, duration_seconds, mux_playback_id").execute()
    
    # Filter to only videos without any logod version (hidden or not)
    videos = [v for v in videos_response.data if v["id"] not in logod_video_ids]
    return videos


def _paginate_logod_video_ids(supabase: Client) -> list[dict]:
    """All logod_videos rows (id + video_id), paginated."""
    rows: list[dict] = []
    offset = 0
    page = 500
    while True:
        r = (
            supabase.table("logod_videos")
            .select("id, video_id")
            .range(offset, offset + page - 1)
            .execute()
        )
        batch = r.data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def load_skip_logod_ids_file(path: str) -> set[str]:
    """UUIDs of logod_videos rows to skip (e.g. already re-built in a prior run)."""
    out: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def get_videos_for_logod_rebackfill(skip_logod_ids: set[str] | None = None) -> list[tuple[dict, str]]:
    """
    (video row, logod_row_id) for every logod_videos row that still has a parent video.
    Multiple logod rows per video_id are each listed separately.
    If ``skip_logod_ids`` is set, rows whose ``logod_videos.id`` is in the set are omitted.
    """
    supabase = get_supabase()
    logod_rows = _paginate_logod_video_ids(supabase)
    if not logod_rows:
        return []

    if skip_logod_ids:
        before = len(logod_rows)
        logod_rows = [lr for lr in logod_rows if lr["id"] not in skip_logod_ids]
        print(f"Skipping {before - len(logod_rows)} logod row(s) listed in skip file ({len(logod_rows)} remaining)\n")

    video_ids = list({row["video_id"] for row in logod_rows})
    by_vid: dict[str, dict] = {}
    chunk = 100
    for i in range(0, len(video_ids), chunk):
        part = video_ids[i : i + chunk]
        vr = (
            supabase.table("videos")
            .select("id, tiktok_video_id, tiktok_author_username, duration_seconds, mux_playback_id")
            .in_("id", part)
            .execute()
        )
        for v in vr.data or []:
            by_vid[v["id"]] = v

    pairs: list[tuple[dict, str]] = []
    for lr in logod_rows:
        v = by_vid.get(lr["video_id"])
        if not v:
            thread_print(f"[skip] logod id={lr['id']}: missing parent video {lr['video_id']}")
            continue
        pairs.append((v, lr["id"]))
    return pairs


def get_max_quality_url(video_id: str, author_username: str) -> str | None:
    """Fetch fresh download URL from TikTok."""
    headers = {
        "x-rapidapi-key": RAPID_API_KEY,
        "x-rapidapi-host": TIKTOK_MAX_QUALITY_HOST,
    }
    tiktok_url = f"https://www.tiktok.com/@{author_username}/video/{video_id}"

    try:
        response = requests.get(TIKTOK_MAX_QUALITY_URL, headers=headers, params={"url": tiktok_url})
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") == "success":
            for url_key in ["off_url", "download_url"]:
                url = data.get(url_key)
                if url and "token=" in url:
                    try:
                        token = url.split("token=")[1]
                        payload = token.split(".")[1]
                        payload += "=" * (4 - len(payload) % 4)
                        decoded = json.loads(base64.urlsafe_b64decode(payload))
                        direct_url = decoded.get("url")
                        if direct_url:
                            return direct_url
                    except Exception:
                        continue
                elif url:
                    return url
            thread_print(f"   [{video_id}] TikTok API: success but no usable URL in response")
        else:
            status = data.get("status", "unknown")
            msg = data.get("message", "")
            thread_print(f"   [{video_id}] TikTok API: status={status} message={msg}")
    except requests.RequestException as e:
        thread_print(f"   [{video_id}] TikTok API request error: {e}")
    
    return None


def download_video(url: str, output_path: str, tiktok_id: str = "") -> tuple[bool, str]:
    """Download video from URL. Returns (success, error_message)."""
    try:
        response = requests.get(url, stream=True, timeout=120)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "video" not in content_type and "octet-stream" not in content_type:
            return False, f"Unexpected Content-Type: {content_type} (HTTP {response.status_code})"
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        file_size = os.path.getsize(output_path)
        if file_size < 1024:
            os.remove(output_path)
            return False, f"File too small ({file_size} bytes), likely not a video"
        return True, ""
    except requests.HTTPError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.reason}"
    except Exception as e:
        return False, str(e)


def add_watermark(
    input_path: str,
    output_path: str,
    author_username: str,
    banner_y: int | None = None,
) -> bool:
    """Add j.cam watermark banner to video."""
    if banner_y is None:
        banner_y = int(WATERMARK_TARGET_HEIGHT * WATERMARK_BANNER_POSITION)
    max_y = WATERMARK_TARGET_HEIGHT - WATERMARK_BANNER_HEIGHT
    banner_y = max(0, min(int(banner_y), max_y))
    logo_height = WATERMARK_BANNER_HEIGHT - 20
    url_text = f"jcam.app/{author_username.upper()}"
    
    filter_complex = (
        f"[0:v]scale={WATERMARK_TARGET_WIDTH}:{WATERMARK_TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WATERMARK_TARGET_WIDTH}:{WATERMARK_TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"drawbox=x=0:y={banner_y}:w={WATERMARK_TARGET_WIDTH}:h={WATERMARK_BANNER_HEIGHT}:color=white:t=fill,"
        f"drawtext=text={url_text}:fontsize=40:fontcolor=black:"
        f"x=w-tw-{WATERMARK_PADDING}:y={banner_y}+({WATERMARK_BANNER_HEIGHT}-th)/2:fontfile={WATERMARK_FONT_PATH}[bg];"
        f"[1:v]scale=-1:{logo_height}[logo];"
        f"[bg][logo]overlay={WATERMARK_PADDING}:{banner_y}+(({WATERMARK_BANNER_HEIGHT}-h)/2)"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", LOGO_PATH,
        "-filter_complex", filter_complex,
        "-c:a", "copy",
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def save_logod_video(
    video_db_id: str,
    mux_data: dict,
    duration: float = None,
    file_size: int = None,
    banner_position_fraction: float | None = None,
) -> dict:
    """Save watermarked video record to Supabase."""
    supabase = get_supabase()
    mux_asset = mux_data.get("data", {})
    playback_ids = mux_asset.get("playback_ids", [])
    playback_id = playback_ids[0]["id"] if playback_ids else None

    bp = (
        banner_position_fraction
        if banner_position_fraction is not None
        else WATERMARK_BANNER_POSITION
    )

    record = {
        "video_id": video_db_id,
        "mux_asset_id": mux_asset.get("id"),
        "mux_playback_id": playback_id,
        "mux_status": mux_asset.get("status"),
        "watermark_type": "jcam_banner",
        "banner_height": WATERMARK_BANNER_HEIGHT,
        "banner_position": round(bp, 4),
        "logo_padding": WATERMARK_PADDING,
        "width": WATERMARK_TARGET_WIDTH,
        "height": WATERMARK_TARGET_HEIGHT,
        "duration_seconds": duration,
        "file_size_bytes": file_size,
        "status": "processing",
    }

    response = supabase.table("logod_videos").insert(record).execute()
    return response.data[0] if response.data else None


def update_logod_video_record(
    logod_db_id: str,
    mux_data: dict,
    duration: float | None,
    file_size: int | None,
    banner_position_fraction: float | None,
) -> None:
    """Replace Mux asset pointers and metadata on an existing logod_videos row."""
    supabase = get_supabase()
    mux_asset = mux_data.get("data", {})
    playback_ids = mux_asset.get("playback_ids", [])
    playback_id = playback_ids[0]["id"] if playback_ids else None

    bp = (
        banner_position_fraction
        if banner_position_fraction is not None
        else WATERMARK_BANNER_POSITION
    )

    update: dict = {
        "mux_asset_id": mux_asset.get("id"),
        "mux_playback_id": playback_id,
        "mux_status": mux_asset.get("status"),
        "banner_position": round(bp, 4),
        "duration_seconds": duration,
        "file_size_bytes": file_size,
        "status": "processing",
    }
    supabase.table("logod_videos").update(update).eq("id", logod_db_id).execute()


def get_mux_mp4_url(playback_id: str) -> str | None:
    """Get a working MP4 download URL from Mux.
    Tries the static rendition paths in order of quality.
    """
    if not playback_id:
        return None
    # Mux static renditions: capped-1080p is the usual name when mp4_support is capped-1080p
    for quality in ["capped-1080p", "high", "medium", "low"]:
        url = f"https://stream.mux.com/{playback_id}/{quality}.mp4"
        try:
            r = requests.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                return url
        except requests.RequestException:
            continue
    return None


def reupload_original_to_mux(file_path: str, video_db_id: str, tiktok_id: str, category: str = "") -> str | None:
    """Re-upload the original video to Mux and update the videos table. Returns new playback_id."""
    supabase = get_supabase()
    mux_result = upload_to_mux_direct(file_path, passthrough=f"tiktok:{tiktok_id}:{category}")
    mux_asset = mux_result.get("data", {})
    playback_ids = mux_asset.get("playback_ids", [])
    playback_id = playback_ids[0]["id"] if playback_ids else None

    update = {
        "mux_asset_id": mux_asset.get("id"),
        "mux_playback_id": playback_id,
        "mux_status": mux_asset.get("status"),
        "status": "processing",
    }
    supabase.table("videos").update(update).eq("id", video_db_id).execute()
    return playback_id


def process_video(
    video: dict,
    index: int,
    existing_logod_id: str | None = None,
) -> dict:
    """Process a single video: download, watermark, upload to Mux.
    Tries Mux MP4 first. If corrupt/too small, falls back to TikTok
    and re-uploads the original to Mux.

    If ``existing_logod_id`` is set, updates that logod row instead of inserting.
    """
    video_id = video["id"]
    tiktok_id = video["tiktok_video_id"]
    author = video["tiktok_author_username"] or "unknown"
    duration = video.get("duration_seconds")
    playback_id = video.get("mux_playback_id")
    
    result = {"video_id": video_id, "success": False, "error": None}
    
    thread_print(f"{index}. Processing {tiktok_id} by @{author}")
    
    temp_dir = tempfile.gettempdir()
    original_path = os.path.join(temp_dir, f"tiktok_{tiktok_id}.mp4")
    watermarked_path = os.path.join(temp_dir, f"tiktok_{tiktok_id}_logod.mp4")

    mux_url = get_mux_mp4_url(playback_id)
    
    try:
        downloaded = False
        needs_reupload = False

        # 1) Try Mux MP4
        if mux_url:
            thread_print(f"   [{tiktok_id}] Downloading from Mux...")
            success, dl_error = download_video(mux_url, original_path, tiktok_id)
            if success:
                file_size = os.path.getsize(original_path)
                thread_print(f"   [{tiktok_id}] Downloaded from Mux: {file_size / 1024 / 1024:.1f} MB")
                downloaded = True
            else:
                thread_print(f"   [{tiktok_id}] Mux failed: {dl_error}")

        # 2) Mux failed or unavailable — fall back to TikTok
        if not downloaded:
            thread_print(f"   [{tiktok_id}] Fetching TikTok download URL...")
            tiktok_url = get_max_quality_url(tiktok_id, author)
            if tiktok_url:
                thread_print(f"   [{tiktok_id}] Downloading from TikTok...")
                success, dl_error = download_video(tiktok_url, original_path, tiktok_id)
                if success:
                    file_size = os.path.getsize(original_path)
                    thread_print(f"   [{tiktok_id}] Downloaded from TikTok: {file_size / 1024 / 1024:.1f} MB")
                    downloaded = True
                    needs_reupload = True
                else:
                    thread_print(f"   [{tiktok_id}] TikTok failed: {dl_error}")
            else:
                thread_print(f"   [{tiktok_id}] TikTok URL not available")

        if not downloaded:
            result["error"] = "All download sources failed"
            thread_print(f"   [{tiktok_id}] All download sources failed")
            return result

        # Re-upload original to Mux if we had to fall back to TikTok
        if needs_reupload:
            thread_print(f"   [{tiktok_id}] Re-uploading original to Mux...")
            new_playback_id = reupload_original_to_mux(original_path, video_id, tiktok_id)
            thread_print(f"   [{tiktok_id}] New Mux playback ID: {new_playback_id}")

        # Watermark (banner Y from Gemini caption boxes on 10 sample frames)
        thread_print(f"   [{tiktok_id}] Watermarking...")
        banner_y, placement_meta = compute_banner_y_for_logod_video(
            original_path, log_prefix=tiktok_id
        )
        if placement_meta.get("gemini_error"):
            thread_print(
                f"   [{tiktok_id}] Banner Gemini: {placement_meta['gemini_error']} (fallback position)"
            )
        else:
            regs = placement_meta.get("regions", [])
            ncap = sum(1 for r in regs if r.get("role") == "auto_caption")
            nov = sum(1 for r in regs if r.get("role") == "creator_overlay")
            thread_print(
                f"   [{tiktok_id}] Banner placement: y={banner_y} (auto_caption={ncap}, creator_overlay={nov}, refine={placement_meta.get('refine_count', 0)})"
            )
        if not add_watermark(original_path, watermarked_path, author, banner_y=banner_y):
            result["error"] = "Watermarking failed"
            thread_print(f"   [{tiktok_id}] Watermarking failed")
            return result
        
        # Original no longer needed after watermarking
        if os.path.exists(original_path):
            os.remove(original_path)
        
        watermarked_size = os.path.getsize(watermarked_path)
        thread_print(f"   [{tiktok_id}] Watermarked: {watermarked_size / 1024 / 1024:.1f} MB")
        
        # Upload watermarked to Mux
        thread_print(f"   [{tiktok_id}] Uploading logod to Mux...")
        mux_result = upload_to_mux_direct(
            watermarked_path,
            passthrough=f"tiktok:{tiktok_id}:logod",
        )
        
        # Watermarked file no longer needed after upload
        if os.path.exists(watermarked_path):
            os.remove(watermarked_path)
        
        logod_mux_asset_id = mux_result.get("data", {}).get("id")
        thread_print(f"   [{tiktok_id}] Logod Mux Asset: {logod_mux_asset_id}")
        
        # Save or update DB
        if existing_logod_id:
            update_logod_video_record(
                existing_logod_id,
                mux_result,
                duration,
                watermarked_size,
                placement_meta.get("banner_position_fraction"),
            )
            thread_print(f"   [{tiktok_id}] Updated logod row {existing_logod_id}")
        else:
            db_record = save_logod_video(
                video_id,
                mux_result,
                duration,
                watermarked_size,
                banner_position_fraction=placement_meta.get("banner_position_fraction"),
            )
            thread_print(f"   [{tiktok_id}] Saved to DB: {db_record['id']}")
        
        result["success"] = True
        
    except Exception as e:
        result["error"] = str(e)
        thread_print(f"   [{tiktok_id}] Error: {e}")
    finally:
        if os.path.exists(original_path):
            os.remove(original_path)
        if os.path.exists(watermarked_path):
            os.remove(watermarked_path)
    
    return result


def main(
    max_workers: int | None = None,
    rebackfill_all: bool = False,
    skip_logod_ids_file: str | None = None,
):
    """Process videos without logod, or re-build every existing logod row."""
    mw = max_workers if max_workers is not None else default_backfill_workers()
    if rebackfill_all:
        skip_ids: set[str] | None = None
        if skip_logod_ids_file:
            skip_ids = load_skip_logod_ids_file(skip_logod_ids_file)
            print(f"Loaded {len(skip_ids)} logod id(s) to skip from {skip_logod_ids_file}\n")
        pairs = get_videos_for_logod_rebackfill(skip_logod_ids=skip_ids)
        print(f"Re-backfill: {len(pairs)} logod row(s) (Mux → TikTok, new placement, update DB)\n")
        work_list: list[tuple[dict, int, str | None]] = [
            (video, i, logod_id) for i, (video, logod_id) in enumerate(pairs, 1)
        ]
    else:
        videos = get_videos_without_logod()
        print(f"Found {len(videos)} videos without logod versions\n")
        work_list = [(v, i, None) for i, v in enumerate(videos, 1)]

    if not work_list:
        print("Nothing to process!")
        return

    print(f"Using {mw} worker thread(s)\n")
    total_success = 0
    total_failed = 0

    with ThreadPoolExecutor(max_workers=mw) as executor:
        futures = {
            executor.submit(process_video, video, i, logod_id): video["id"]
            for video, i, logod_id in work_list
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                if result["success"]:
                    total_success += 1
                else:
                    total_failed += 1
            except Exception as e:
                print(f"Thread error: {e}")
                total_failed += 1
    
    print(f"\n{'='*50}")
    print(f"=== Summary ===")
    print(f"Success: {total_success}")
    print(f"Failed: {total_failed}")


if __name__ == "__main__":
    import sys

    rebackfill_all = "--rebackfill-all" in sys.argv
    max_workers = default_backfill_workers()
    skip_logod_ids_file: str | None = None
    for arg in sys.argv:
        if arg.startswith("--workers="):
            max_workers = max(1, int(arg.partition("=")[2]))
        elif arg.startswith("--skip-logod-ids-file="):
            skip_logod_ids_file = arg.partition("=")[2].strip() or None
    main(
        max_workers=max_workers,
        rebackfill_all=rebackfill_all,
        skip_logod_ids_file=skip_logod_ids_file,
    )
