#!/usr/bin/env python3
"""
Cron job script to search TikTok videos, upload to Mux, and store in Supabase.
Reads keywords from keywords.csv and searches for videos.
Skips duplicates based on tiktok_video_id.
Uses multithreading for parallel downloads and uploads.
"""

import base64
import csv
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dotenv import load_dotenv
from google import genai
from supabase import create_client, Client

from banner_placement import compute_banner_y_for_logod_video

load_dotenv()

RAPID_API_KEY = os.getenv("RAPID_API_KEY")
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SERVICE_ROLE_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TIKTOK_API_HOST = "tiktok-api23.p.rapidapi.com"
TIKTOK_SEARCH_URL = f"https://{TIKTOK_API_HOST}/api/search/general"
TIKTOK_MAX_QUALITY_HOST = "tiktok-max-quality.p.rapidapi.com"
TIKTOK_MAX_QUALITY_URL = f"https://{TIKTOK_MAX_QUALITY_HOST}/download"

# Watermark settings
LOGO_PATH = os.path.join(os.path.dirname(__file__), "j cam logo black.png")
WATERMARK_BANNER_HEIGHT = 100
WATERMARK_BANNER_POSITION = 5 / 7  # 5/7 down the video
WATERMARK_PADDING = 105
WATERMARK_TARGET_WIDTH = 1080
WATERMARK_TARGET_HEIGHT = 1920
WATERMARK_FONT_PATH = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf"
    if platform.system() == "Darwin"
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)

MAX_DOWNLOAD_SIZE = 500 * 1024 * 1024  # 500 MB
MAX_RUNTIME_SECONDS = 45 * 60  # 45 minutes
_start_time = time.time()

# Thread-safe print lock
print_lock = threading.Lock()

# Create supabase client per-thread to avoid connection issues
_thread_local = threading.local()


def get_supabase() -> Client:
    """Get thread-local Supabase client."""
    if not hasattr(_thread_local, "supabase"):
        _thread_local.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _thread_local.supabase


def thread_print(*args, **kwargs):
    """Thread-safe print."""
    with print_lock:
        print(*args, **kwargs)


def load_keywords(csv_path: str) -> list[dict]:
    """Load keywords from CSV file."""
    keywords = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keywords.append(row)
    return keywords


def get_existing_video_ids() -> set[str]:
    """Get all existing TikTok video IDs from Supabase."""
    supabase = get_supabase()
    response = supabase.table("videos").select("tiktok_video_id").execute()
    return {row["tiktok_video_id"] for row in response.data}


def extract_hashtags(description: str) -> list[str]:
    """Extract hashtags from description."""
    return re.findall(r"#(\w+)", description)


def get_max_quality_url(video_id: str, author_username: str) -> dict | None:
    """Fetch max quality video URL using TikTok Max Quality API.
    Uses off_url for original quality (often 1080p), falls back to download_url.
    """
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
            # Prefer off_url (original quality, often 1080p) over download_url (720p)
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
                            quality_label = "Original" if url_key == "off_url" else "Standard"
                            return {
                                "url": direct_url,
                                "quality": f"{quality_label} ({data.get('quality', 'HD')})",
                                "message": data.get("message"),
                                "source": url_key,
                            }
                    except Exception:
                        continue
            
            # Fallback to raw URLs if JWT decoding fails
            for url_key in ["off_url", "download_url"]:
                url = data.get(url_key)
                if url:
                    return {
                        "url": url,
                        "quality": data.get("quality"),
                        "message": data.get("message"),
                        "source": url_key,
                    }
    except requests.RequestException:
        pass
    
    return None


def search_tiktok(keyword: str, count: int = 10) -> list[dict]:
    """Search TikTok for videos matching the keyword. Paginates through all available results."""
    headers = {
        "x-rapidapi-key": RAPID_API_KEY,
        "x-rapidapi-host": TIKTOK_API_HOST,
    }

    videos = []
    cursor = "0"
    search_id = "0"
    page_size = min(count, 30)
    seen_ids = set()
    bad_pages = 0

    page = 0
    while True:
        page += 1
        params = {
            "keyword": keyword,
            "count": str(page_size),
            "cursor": cursor,
            "search_id": search_id,
        }

        try:
            response = requests.get(TIKTOK_SEARCH_URL, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            print(f"   Page {page} request failed: {e}")
            bad_pages += 1
            if bad_pages >= 5:
                print(f"   5 consecutive bad pages, stopping")
                break
            time.sleep(1)
            continue

        item_list = data.get("item_list") or data.get("data", {}).get("item_list", [])

        prev_count = len(videos)

        for item in item_list:
            video = item.get("video", {})
            author = item.get("author", {})
            stats = item.get("stats", {})
            music = item.get("music", {})

            vid_id = item.get("id") or item.get("video_id") or item.get("aweme_id")
            if not vid_id or vid_id in seen_ids:
                continue
            seen_ids.add(vid_id)

            play_url = video.get("downloadAddr") or video.get("playAddr")

            created_timestamp = item.get("createTime")
            created_at = None
            if created_timestamp:
                created_at = datetime.fromtimestamp(int(created_timestamp), tz=timezone.utc).isoformat()

            description = item.get("desc") or item.get("title", "")

            video_info = {
                "id": vid_id,
                "url": play_url,
                "author_id": author.get("id"),
                "author_username": author.get("uniqueId") or author.get("unique_id") or author.get("nickname", "unknown"),
                "description": description,
                "hashtags": extract_hashtags(description),
                "music_title": music.get("title"),
                "music_author": music.get("authorName"),
                "like_count": stats.get("diggCount") or stats.get("likeCount"),
                "comment_count": stats.get("commentCount"),
                "share_count": stats.get("shareCount"),
                "view_count": stats.get("playCount") or stats.get("viewCount"),
                "width": video.get("width"),
                "height": video.get("height"),
                "duration": video.get("duration"),
                "created_at": created_at,
            }
            videos.append(video_info)

        print(f"   Page {page}: got {len(item_list)} videos (total unique: {len(videos)})")

        if len(videos) == prev_count:
            bad_pages += 1
            if bad_pages >= 5:
                print(f"   5 consecutive bad pages, stopping")
                break
        else:
            bad_pages = 0

        next_cursor = data.get("cursor") or data.get("data", {}).get("cursor")

        # Extract search_id from log_pb.impr_id for pagination
        log_pb = data.get("log_pb") or data.get("data", {}).get("log_pb") or {}
        next_search_id = log_pb.get("impr_id")
        if next_search_id:
            search_id = str(next_search_id)

        if not next_cursor or str(next_cursor) == cursor:
            break

        cursor = str(next_cursor)
        time.sleep(0.5)

    return videos


def download_video(url: str, video_id: str) -> str | None:
    """Download video to a temporary file and return the file path."""
    try:
        response = requests.get(url, stream=True, timeout=120)
        response.raise_for_status()
        
        total = int(response.headers.get("content-length", 0))
        if total > MAX_DOWNLOAD_SIZE:
            thread_print(f"   [{video_id}] Skipping: {total / 1024 / 1024:.0f} MB exceeds {MAX_DOWNLOAD_SIZE // 1024 // 1024} MB limit")
            return None, "File too large"
        
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"tiktok_{video_id}.mp4")
        
        downloaded = 0
        last_log = time.time()
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total and time.time() - last_log >= 10:
                    pct = downloaded * 100 // total
                    mb = downloaded / 1024 / 1024
                    thread_print(f"   [{video_id}] Downloading... {mb:.0f} MB ({pct}%)")
                    last_log = time.time()
        
        file_size = os.path.getsize(temp_path)
        return temp_path, file_size
    except Exception as e:
        return None, str(e)


def add_watermark(
    input_path: str,
    output_path: str,
    author_username: str,
    banner_y: int | None = None,
) -> bool:
    """
    Add j.cam watermark banner to video (same ffmpeg layout as backfill_logod_videos.add_watermark).
    Scales/pads to 1080×1920. ``banner_y`` should come from
    ``banner_placement.compute_banner_y_for_logod_video`` when producing logod assets.
    If ``banner_y`` is None, uses WATERMARK_BANNER_POSITION (clamped to frame).
    """
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


def upload_to_mux_direct(file_path: str, passthrough: str = None) -> dict:
    """Upload a video file directly to Mux using direct upload."""
    import time
    
    # Step 1: Create a direct upload URL
    create_url = "https://api.mux.com/video/v1/uploads"
    headers = {"Content-Type": "application/json"}
    payload = {
        "new_asset_settings": {
            "playback_policy": ["public"],
            "mp4_support": "capped-1080p",
        },
        "cors_origin": "*",
    }
    if passthrough:
        payload["new_asset_settings"]["passthrough"] = passthrough

    response = requests.post(
        create_url,
        json=payload,
        headers=headers,
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
    )
    response.raise_for_status()
    upload_data = response.json()
    
    upload_url = upload_data["data"]["url"]
    upload_id = upload_data["data"]["id"]
    
    # Step 2: Upload the file directly to the upload URL
    with open(file_path, "rb") as f:
        upload_response = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": "video/mp4"},
        )
        upload_response.raise_for_status()
    
    # Step 3: Poll for the asset to be created
    for _ in range(30):
        check_response = requests.get(
            f"https://api.mux.com/video/v1/uploads/{upload_id}",
            auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
        )
        check_data = check_response.json()
        status = check_data["data"]["status"]
        
        if status == "asset_created":
            asset_id = check_data["data"]["asset_id"]
            asset_response = requests.get(
                f"https://api.mux.com/video/v1/assets/{asset_id}",
                auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
            )
            return asset_response.json()
        elif status == "errored":
            raise Exception(f"Upload failed: {check_data['data'].get('error', {}).get('message', 'Unknown error')}")
        
        time.sleep(1)
    
    return {"data": {"id": check_data["data"].get("asset_id"), "status": "preparing"}}


def wait_for_mux_ready(asset_id: str, max_wait: int = 120) -> dict | None:
    """Poll Mux until asset is ready or timeout."""
    import time
    for i in range(max_wait):
        try:
            response = requests.get(
                f"https://api.mux.com/video/v1/assets/{asset_id}",
                auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
            )
            if response.status_code == 200:
                data = response.json().get("data", {})
                status = data.get("status")
                if status == "ready":
                    return data
                elif status == "errored":
                    return None
        except Exception:
            pass
        time.sleep(1)
    return None


def update_db_status(table: str, id_column: str, id_value: str, mux_data: dict):
    """Update database record with ready status."""
    supabase = get_supabase()
    playback_ids = mux_data.get("playback_ids", [])
    playback_id = playback_ids[0]["id"] if playback_ids else None
    
    update_data = {
        "mux_status": "ready",
        "status": "ready",
    }
    if playback_id:
        update_data["mux_playback_id"] = playback_id
    
    supabase.table(table).update(update_data).eq(id_column, id_value).execute()


def save_to_supabase(video: dict, keyword: str, category: str, mux_data: dict, upsert: bool = False, video_summary: str = None, summary_embedding: list[float] = None) -> dict:
    """Save video record to Supabase. If upsert=True, updates existing record."""
    supabase = get_supabase()
    mux_asset = mux_data.get("data", {})
    playback_ids = mux_asset.get("playback_ids", [])
    playback_id = playback_ids[0]["id"] if playback_ids else None

    record = {
        "tiktok_video_id": video["id"],
        "tiktok_url": video["url"],
        "tiktok_author_id": video.get("author_id"),
        "tiktok_author_username": video.get("author_username"),
        "tiktok_description": video.get("description"),
        "tiktok_hashtags": video.get("hashtags"),
        "tiktok_music_title": video.get("music_title"),
        "tiktok_music_author": video.get("music_author"),
        "tiktok_like_count": video.get("like_count"),
        "tiktok_comment_count": video.get("comment_count"),
        "tiktok_share_count": video.get("share_count"),
        "tiktok_view_count": video.get("view_count"),
        "tiktok_created_at": video.get("created_at"),
        "search_keyword": keyword,
        "category": category,
        "mux_asset_id": mux_asset.get("id"),
        "mux_playback_id": playback_id,
        "mux_status": mux_asset.get("status"),
        "width": video.get("width"),
        "height": video.get("height"),
        "duration_seconds": video.get("duration"),
        "status": "processing",
    }

    if video_summary:
        record["video_summary"] = video_summary
    if summary_embedding:
        record["summary_embedding"] = summary_embedding

    if upsert:
        response = supabase.table("videos").upsert(record, on_conflict="tiktok_video_id").execute()
    else:
        response = supabase.table("videos").insert(record).execute()
    return response.data[0] if response.data else None


def save_logod_video_to_supabase(
    video_db_id: str,
    mux_data: dict,
    duration: float = None,
    file_size: int = None,
    banner_position_fraction: float | None = None,
) -> dict:
    """Insert logod_videos row (schema aligned with backfill_logod_videos.save_logod_video)."""
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


GEMINI_MODELS = [
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
]
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
VIDEO_CATEGORIES = [
    "pov_glasses",
    "selfie",
    "news_clip",
    "screen_recording",
    "handheld_phone",
    "professional_edit",
    "talking_head",
    "gaming",
    "slideshow_or_text",
    "animation",
    "other",
]

GEMINI_CLASSIFY_PROMPT = """Watch this video carefully and classify it into EXACTLY ONE of these categories:

pov_glasses — First-person POV filmed from streaming glasses or a head-mounted wearable camera (Ray-Ban Meta, Spectacles, etc.). The camera sits at eye level and moves naturally with the wearer's head. You see the world from their perspective as they walk, look around, or interact. Often has subtle head-bob motion. Hands may appear in frame without holding anything.

selfie — Front-facing camera pointed at the person filming. The subject is looking into the camera, often at arm's length or on a selfie stick. Includes "talking to camera" and GRWM-style videos.

news_clip — Broadcast news footage, TV screen recordings of news, or journalist-style reporting. Professional graphics, chyrons, or news tickers visible.

screen_recording — Recording of a phone screen, computer screen, or app interface. Shows UI elements, notifications, or app content.

handheld_phone — Filmed on a phone held in someone's hand (rear camera). You can often tell by the framing, stabilization style, or the fact that the camera moves differently from head-mounted footage. Includes tripod shots.

professional_edit — Professionally shot or heavily edited content. Cinematic angles, multiple camera cuts, color grading, transitions, or polished production.

talking_head — Someone speaking to camera from a fixed position (podcast-style, commentary, reaction). Camera is stationary on a desk/tripod.

gaming — Video game footage, gameplay recordings, or gaming streams.

slideshow_or_text — Primarily text overlays, photo slideshows, or static images with music.

animation — Animated content, cartoons, or CGI.

other — Anything that doesn't fit the above categories.

Respond with ONLY the category name, nothing else."""

GEMINI_PROMPT = """Analyze this video and return the following in plain text (no markdown formatting):

SUMMARY:
Write exactly 3 sentences describing what is happening in the video. Be specific about actions, people, settings, and context.

VIBE AND MOOD:
Describe why this video is happy, funny, satisfying, or engaging. What emotion does it evoke and why? What makes it shareable?

TREND TYPE:
Identify what type of TikTok trend this is (e.g. POV skit, dance challenge, storytime, GRWM, satisfying video, prank, reaction, transition, day in my life, mukbang, duet, tutorial, thirst trap, aesthetic, unboxing, etc.). If it fits multiple trends, list all of them.

TIKTOK SEARCH TERMS:
List comma-separated words and phrases that TikTok users would actually type into TikTok search to find this type of content. Think like a TikTok user — include slang, trending phrases, niche community terms, sounds, and hashtag-style phrases.

SEARCH KEYWORDS:
List comma-separated keywords/phrases for general search. Include topics, actions, moods, styles, settings, objects, and any notable visual elements.

TEXT AND SPEECH:
List every word or phrase of text that appears visually in the video (overlays, captions, signs, watermarks, etc.) AND any spoken words or lyrics you can identify. Separate with commas. If none, write "none".
"""

_gemini_client_lock = threading.Lock()
_gemini_client = None


def get_gemini_client() -> genai.Client:
    global _gemini_client
    with _gemini_client_lock:
        if _gemini_client is None:
            _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return _gemini_client


def _upload_to_gemini(file_path: str, video_id: str):
    """Upload video to Gemini and wait for it to be active. Returns (client, uploaded_file) or (None, None)."""
    try:
        client = get_gemini_client()
        thread_print(f"   [{video_id}] Uploading to Gemini...")
        uploaded = client.files.upload(file=file_path)
        for i in range(60):
            status = client.files.get(name=uploaded.name)
            if status.state.name == "ACTIVE":
                return client, uploaded
            if i > 0 and i % 10 == 0:
                thread_print(f"   [{video_id}] Gemini processing... ({i * 2}s)")
            time.sleep(2)
        thread_print(f"   [{video_id}] Gemini file processing timed out")
        return None, None
    except Exception as e:
        thread_print(f"   [{video_id}] Gemini upload error: {e}")
        return None, None


def _gemini_generate(client, uploaded, prompt: str, video_id: str) -> str | None:
    """Run a prompt against an uploaded Gemini file. Tries all models with retries."""
    last_error = None
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[uploaded, prompt],
                )
                thread_print(f"   [{video_id}] Used model: {model}")
                return response.text
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait = 30 * (attempt + 1)
                    thread_print(f"   [{video_id}] {model} rate limited, waiting {wait}s (attempt {attempt + 1}/3)...")
                    time.sleep(wait)
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    thread_print(f"   [{video_id}] {model} unavailable, trying next model...")
                    break
                else:
                    thread_print(f"   [{video_id}] {model} failed: {e}")
                    break

    thread_print(f"   [{video_id}] All models failed: {last_error}")
    return None


def classify_video(file_path: str, video_id: str) -> str:
    """Classify a video into a category using Gemini.
    Returns the category string (e.g. 'pov_glasses', 'selfie', 'news_clip', etc.)
    or 'unknown' on failure.
    """
    client, uploaded = _upload_to_gemini(file_path, video_id)
    if not client or not uploaded:
        thread_print(f"   [{video_id}] Classification: upload failed")
        return "unknown"

    result = _gemini_generate(client, uploaded, GEMINI_CLASSIFY_PROMPT, video_id)

    if not result:
        thread_print(f"   [{video_id}] Classification: generation failed")
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
        return "unknown"

    category = result.strip().lower().replace(" ", "_")
    if category not in VIDEO_CATEGORIES:
        thread_print(f"   [{video_id}] Classification: unrecognized category {category!r}, treating as 'other'")
        category = "other"
    else:
        thread_print(f"   [{video_id}] Classification: {category}")

    if category != "pov_glasses":
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

    return category


def generate_video_summary(file_path: str, video_id: str) -> str | None:
    """Upload video to Gemini and get an AI summary. Retries on rate limits, falls back to other models."""
    client, uploaded = _upload_to_gemini(file_path, video_id)
    if not client or not uploaded:
        return None

    result = _gemini_generate(client, uploaded, GEMINI_PROMPT, video_id)

    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    return result


def generate_embedding(text: str, video_id: str) -> list[float] | None:
    """Generate a vector embedding for the given text."""
    from google.genai import types
    try:
        client = get_gemini_client()
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIMENSIONS),
        )
        return result.embeddings[0].values
    except Exception as e:
        thread_print(f"   [{video_id}] Embedding error: {e}")
        return None


def process_single_video(video: dict, keyword: str, category: str, is_existing: bool, index: int) -> dict:
    """Process a single video: download, upload to Mux, create watermarked version, save to Supabase."""
    video_id = video.get("id")
    author = video.get("author_username")
    description = (video.get("description") or "")[:50]
    
    result = {
        "video_id": video_id,
        "success": False,
        "error": None,
        "mux_asset_id": None,
        "logod_mux_asset_id": None,
    }
    
    action = "REPROCESS" if is_existing else "NEW"
    thread_print(f"{index}. [{action}] {video_id} by @{author}")
    
    # Get max quality URL
    max_quality = get_max_quality_url(video_id, author)
    if max_quality:
        video_url = max_quality["url"]
        thread_print(f"   [{video_id}] Quality: {max_quality.get('quality')}")
    else:
        video_url = video.get("url")
        thread_print(f"   [{video_id}] Quality: Fallback")
    
    video["url"] = video_url
    
    if not video_url:
        result["error"] = "No video URL"
        return result
    
    local_path = None
    watermarked_path = None
    try:
        # Download video locally
        download_result = download_video(video_url, video_id)
        if download_result[0]:
            local_path, file_size = download_result
            thread_print(f"   [{video_id}] Downloaded: {file_size / 1024 / 1024:.1f} MB")
            
            # Classify video type
            thread_print(f"   [{video_id}] Classifying video...")
            video_category = classify_video(local_path, video_id)
            if video_category != "pov_glasses":
                thread_print(f"   [{video_id}] Not POV glasses (classified as '{video_category}'), skipping")
                result["error"] = f"Classified as {video_category}"
                return result
            
            # Generate AI summary with Gemini
            thread_print(f"   [{video_id}] Generating AI summary...")
            video_summary = generate_video_summary(local_path, video_id)
            summary_embedding = None
            if video_summary:
                preview = video_summary[:80].replace("\n", " ")
                thread_print(f"   [{video_id}] Summary: {preview}...")
                summary_embedding = generate_embedding(video_summary, video_id)
                if summary_embedding:
                    thread_print(f"   [{video_id}] Embedding: {EMBEDDING_DIMENSIONS}d vector generated")
            else:
                thread_print(f"   [{video_id}] Summary generation failed, continuing...")
            
            # Upload original to Mux
            mux_result = upload_to_mux_direct(
                local_path,
                passthrough=f"tiktok:{video_id}:{category}",
            )
            
            mux_asset_id = mux_result.get("data", {}).get("id")
            thread_print(f"   [{video_id}] Mux Asset: {mux_asset_id}")
            
            # Save original to Supabase
            db_record = save_to_supabase(video, keyword, category, mux_result, upsert=is_existing, video_summary=video_summary, summary_embedding=summary_embedding)
            thread_print(f"   [{video_id}] Saved to DB: {db_record['id']}")
            
            result["success"] = True
            result["mux_asset_id"] = mux_asset_id
            
            # Create watermarked version
            temp_dir = tempfile.gettempdir()
            watermarked_path = os.path.join(temp_dir, f"tiktok_{video_id}_logod.mp4")
            
            thread_print(f"   [{video_id}] Creating watermarked version (banner_placement + ffmpeg)…")
            banner_y, placement_meta = compute_banner_y_for_logod_video(
                local_path, log_prefix=video_id
            )
            if placement_meta.get("gemini_error"):
                thread_print(
                    f"   [{video_id}] Banner Gemini: {placement_meta['gemini_error']} (using default slot if needed)"
                )
            else:
                regs = placement_meta.get("regions", [])
                ncap = sum(1 for r in regs if r.get("role") == "auto_caption")
                nov = sum(1 for r in regs if r.get("role") == "creator_overlay")
                thread_print(
                    f"   [{video_id}] Banner placement: y={banner_y} (auto_caption={ncap}, creator_overlay={nov}, refine={placement_meta.get('refine_count', 0)})"
                )
            if add_watermark(local_path, watermarked_path, author, banner_y=banner_y):
                # Original no longer needed after watermarking
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
                    local_path = None
                
                watermarked_size = os.path.getsize(watermarked_path)
                thread_print(f"   [{video_id}] Watermarked: {watermarked_size / 1024 / 1024:.1f} MB")
                
                # Upload watermarked version to Mux
                logod_mux_result = upload_to_mux_direct(
                    watermarked_path,
                    passthrough=f"tiktok:{video_id}:{category}:logod",
                )
                
                # Watermarked file no longer needed after upload
                if watermarked_path and os.path.exists(watermarked_path):
                    os.remove(watermarked_path)
                    watermarked_path = None
                
                logod_mux_asset_id = logod_mux_result.get("data", {}).get("id")
                thread_print(f"   [{video_id}] Logod Mux Asset: {logod_mux_asset_id}")
                
                # Save watermarked version to Supabase
                logod_db_record = save_logod_video_to_supabase(
                    db_record["id"],
                    logod_mux_result,
                    duration=video.get("duration"),
                    file_size=watermarked_size,
                    banner_position_fraction=placement_meta.get("banner_position_fraction"),
                )
                thread_print(f"   [{video_id}] Logod saved to DB: {logod_db_record['id']}")
                
                result["logod_mux_asset_id"] = logod_mux_asset_id
                
                # Wait for both assets to be ready
                thread_print(f"   [{video_id}] Waiting for Mux to process...")
                
                if mux_asset_id:
                    mux_ready = wait_for_mux_ready(mux_asset_id)
                    if mux_ready:
                        update_db_status("videos", "id", db_record["id"], mux_ready)
                        thread_print(f"   [{video_id}] Original video ready")
                
                if logod_mux_asset_id:
                    logod_ready = wait_for_mux_ready(logod_mux_asset_id)
                    if logod_ready:
                        update_db_status("logod_videos", "id", logod_db_record["id"], logod_ready)
                        thread_print(f"   [{video_id}] Logod video ready")
            else:
                thread_print(f"   [{video_id}] Watermarking failed")
        else:
            result["error"] = f"Download failed: {download_result[1]}"
            thread_print(f"   [{video_id}] Download failed: {download_result[1]}")
            
    except Exception as e:
        result["error"] = str(e)
        thread_print(f"   [{video_id}] Error: {e}")
    finally:
        if local_path and os.path.exists(local_path):
            os.remove(local_path)
        if watermarked_path and os.path.exists(watermarked_path):
            os.remove(watermarked_path)
    
    return result


def run_backfills():
    """Run all backfill scripts before searching for new videos."""
    import subprocess
    script_dir = os.path.dirname(__file__)
    backfills = [
        ("Logod videos", "backfill_logod_videos.py"),
        ("Video summaries", "backfill_video_summaries.py"),
        ("Embeddings", "backfill_embeddings.py"),
    ]
    for name, script in backfills:
        path = os.path.join(script_dir, script)
        if not os.path.exists(path):
            continue
        print(f"\n{'='*50}")
        print(f"=== Backfill: {name} ===")
        print(f"{'='*50}\n")
        result = subprocess.run(
            ["/usr/local/bin/python3", path],
            cwd=script_dir,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        if result.returncode != 0:
            print(f"   Backfill {name} exited with code {result.returncode}")


def cleanup_temp_files():
    """Remove stale video files from temp directory left by interrupted runs."""
    import glob
    temp_dir = tempfile.gettempdir()
    patterns = [
        os.path.join(temp_dir, "tiktok_*.mp4"),
        os.path.join(temp_dir, "gemini_*.mp4"),
    ]
    removed = 0
    freed = 0
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                freed += os.path.getsize(path)
                os.remove(path)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"Cleaned up {removed} stale temp files ({freed / 1024 / 1024:.0f} MB)")


def main(count: int = 10, reprocess_existing: bool = False, max_workers: int = 4):
    """Main function with multithreading support."""
    global _start_time
    _start_time = time.time()
    
    cleanup_temp_files()
    run_backfills()

    print(f"\n{'='*50}")
    print(f"=== Searching for new videos ===")
    print(f"{'='*50}\n")

    csv_path = os.path.join(os.path.dirname(__file__), "keywords.csv")
    keywords = load_keywords(csv_path)

    existing_ids = get_existing_video_ids()
    print(f"Found {len(existing_ids)} existing videos in database")
    if reprocess_existing:
        print("Reprocessing mode: will re-upload and overwrite existing records")
    print(f"Using {max_workers} worker threads\n")

    total_processed = 0
    total_skipped = 0
    total_uploaded = 0
    total_failed = 0

    for entry in keywords:
        elapsed = time.time() - _start_time
        if elapsed > MAX_RUNTIME_SECONDS:
            print(f"\n=== Runtime limit reached ({elapsed / 60:.0f}m), stopping gracefully ===")
            break

        keyword = entry.get("keywords", "")
        category = entry.get("category", "")

        if not keyword:
            continue

        print(f"=== Searching for: '{keyword}' (category: {category}) ===\n")

        try:
            videos = search_tiktok(keyword, count=count)

            if not videos:
                print(f"No videos found for '{keyword}'")
                continue

            # Filter videos to process
            videos_to_process = []
            for i, video in enumerate(videos, 1):
                video_id = video.get("id")
                is_existing = video_id in existing_ids
                
                if is_existing and not reprocess_existing:
                    author = video.get("author_username")
                    print(f"{i}. SKIP (duplicate): {video_id} by @{author}")
                    total_skipped += 1
                    continue
                
                videos_to_process.append((video, is_existing, i))
                total_processed += 1

            if not videos_to_process:
                continue

            # Process videos in parallel
            print(f"\nProcessing {len(videos_to_process)} videos in parallel...\n")
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        process_single_video, 
                        video, keyword, category, is_existing, index
                    ): video["id"]
                    for video, is_existing, index in videos_to_process
                }
                
                for future in as_completed(futures):
                    video_id = futures[future]
                    try:
                        result = future.result()
                        if result["success"]:
                            existing_ids.add(video_id)
                            total_uploaded += 1
                        else:
                            total_failed += 1
                    except Exception as e:
                        print(f"   [{video_id}] Thread error: {e}")
                        total_failed += 1

        except requests.RequestException as e:
            print(f"Error searching TikTok: {e}")

    print(f"\n{'='*50}")
    print(f"=== Summary ===")
    print(f"Total processed: {total_processed}")
    print(f"Skipped (duplicates): {total_skipped}")
    print(f"Uploaded & saved: {total_uploaded}")
    print(f"Failed: {total_failed}")


if __name__ == "__main__":
    main(count=25, reprocess_existing=False, max_workers=5)
