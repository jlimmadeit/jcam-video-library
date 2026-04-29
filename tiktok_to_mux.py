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

load_dotenv()

RAPID_API_KEY = os.getenv("RAPID_API_KEY")
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TIKTOK_API_HOST = "tiktok-api23.p.rapidapi.com"
TIKTOK_SEARCH_URL = f"https://{TIKTOK_API_HOST}/api/search/general"
TIKTOK_MAX_QUALITY_HOST = "tiktok-max-quality.p.rapidapi.com"
TIKTOK_MAX_QUALITY_URL = f"https://{TIKTOK_MAX_QUALITY_HOST}/download"

# Watermark settings
LOGO_PATH = os.path.join(os.path.dirname(__file__), "j.cam 400x160.png")
WATERMARK_BANNER_HEIGHT = 100
WATERMARK_BANNER_POSITION = 5 / 7  # 5/7 down the video
WATERMARK_PADDING = 105
WATERMARK_TARGET_WIDTH = 1080
WATERMARK_TARGET_HEIGHT = 1920
WATERMARK_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Black.ttf"

# Thread-safe print lock
print_lock = threading.Lock()

# Create supabase client per-thread to avoid connection issues
_thread_local = threading.local()


def get_supabase() -> Client:
    """Get thread-local Supabase client."""
    if not hasattr(_thread_local, "supabase"):
        _thread_local.supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
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
    """Search TikTok for videos matching the keyword."""
    headers = {
        "x-rapidapi-key": RAPID_API_KEY,
        "x-rapidapi-host": TIKTOK_API_HOST,
    }
    params = {
        "keyword": keyword,
        "count": str(count),
        "cursor": "0",
    }

    response = requests.get(TIKTOK_SEARCH_URL, headers=headers, params=params)
    response.raise_for_status()

    data = response.json()
    videos = []

    item_list = data.get("item_list") or data.get("data", {}).get("item_list", [])

    for item in item_list[:count]:
        video = item.get("video", {})
        author = item.get("author", {})
        stats = item.get("stats", {})
        music = item.get("music", {})

        # Get fallback URL from search results
        play_url = video.get("downloadAddr") or video.get("playAddr")

        created_timestamp = item.get("createTime")
        created_at = None
        if created_timestamp:
            created_at = datetime.fromtimestamp(int(created_timestamp), tz=timezone.utc).isoformat()

        description = item.get("desc") or item.get("title", "")

        video_info = {
            "id": item.get("id") or item.get("video_id") or item.get("aweme_id"),
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

    return videos


def download_video(url: str, video_id: str) -> str | None:
    """Download video to a temporary file and return the file path."""
    try:
        response = requests.get(url, stream=True, timeout=120)
        response.raise_for_status()
        
        # Create temp file with .mp4 extension
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"tiktok_{video_id}.mp4")
        
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        file_size = os.path.getsize(temp_path)
        return temp_path, file_size
    except Exception as e:
        return None, str(e)


def add_watermark(input_path: str, output_path: str, author_username: str) -> bool:
    """
    Add j.cam watermark banner to video.
    Scales video to 1080x1920, adds white banner at 5/7 down with logo and URL.
    """
    banner_y = int(WATERMARK_TARGET_HEIGHT * WATERMARK_BANNER_POSITION)
    logo_height = WATERMARK_BANNER_HEIGHT - 20
    url_text = f"J.CAM/{author_username.upper()}"
    
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


def save_logod_video_to_supabase(video_db_id: str, mux_data: dict, duration: float = None, file_size: int = None) -> dict:
    """Save watermarked video record to Supabase logod_videos table."""
    supabase = get_supabase()
    mux_asset = mux_data.get("data", {})
    playback_ids = mux_asset.get("playback_ids", [])
    playback_id = playback_ids[0]["id"] if playback_ids else None

    record = {
        "video_id": video_db_id,
        "mux_asset_id": mux_asset.get("id"),
        "mux_playback_id": playback_id,
        "mux_status": mux_asset.get("status"),
        "watermark_type": "jcam_banner",
        "banner_height": WATERMARK_BANNER_HEIGHT,
        "banner_position": round(WATERMARK_BANNER_POSITION, 3),
        "logo_padding": WATERMARK_PADDING,
        "width": WATERMARK_TARGET_WIDTH,
        "height": WATERMARK_TARGET_HEIGHT,
        "duration_seconds": duration,
        "file_size_bytes": file_size,
        "status": "processing",
    }

    response = supabase.table("logod_videos").insert(record).execute()
    return response.data[0] if response.data else None


GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
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


def generate_video_summary(file_path: str, video_id: str) -> str | None:
    """Upload video to Gemini and get an AI summary. Tries fallback models on failure."""
    try:
        client = get_gemini_client()
        uploaded = client.files.upload(file=file_path)
        for _ in range(60):
            status = client.files.get(name=uploaded.name)
            if status.state.name == "ACTIVE":
                break
            time.sleep(2)
        else:
            thread_print(f"   [{video_id}] Gemini file processing timed out")
            return None
    except Exception as e:
        thread_print(f"   [{video_id}] Gemini upload error: {e}")
        return None

    last_error = None
    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[uploaded, GEMINI_PROMPT],
            )
            thread_print(f"   [{video_id}] Used model: {model}")
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
            return response.text
        except Exception as e:
            last_error = e
            thread_print(f"   [{video_id}] {model} failed: {e}")
            time.sleep(1)

    thread_print(f"   [{video_id}] All models failed: {last_error}")
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass
    return None


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
            
            thread_print(f"   [{video_id}] Creating watermarked version...")
            if add_watermark(local_path, watermarked_path, author):
                watermarked_size = os.path.getsize(watermarked_path)
                thread_print(f"   [{video_id}] Watermarked: {watermarked_size / 1024 / 1024:.1f} MB")
                
                # Upload watermarked version to Mux
                logod_mux_result = upload_to_mux_direct(
                    watermarked_path,
                    passthrough=f"tiktok:{video_id}:{category}:logod",
                )
                
                logod_mux_asset_id = logod_mux_result.get("data", {}).get("id")
                thread_print(f"   [{video_id}] Logod Mux Asset: {logod_mux_asset_id}")
                
                # Save watermarked version to Supabase
                logod_db_record = save_logod_video_to_supabase(
                    db_record["id"],
                    logod_mux_result,
                    duration=video.get("duration"),
                    file_size=watermarked_size,
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


def main(count: int = 10, reprocess_existing: bool = False, max_workers: int = 4):
    """Main function with multithreading support."""
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
    main(count=25, reprocess_existing=False, max_workers=4)
