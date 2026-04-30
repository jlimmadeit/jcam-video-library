#!/usr/bin/env python3
"""
Temporary script to create watermarked versions of existing videos in the database.
Re-downloads from TikTok since Mux MP4s may not be ready and TikTok URLs expire.
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

RAPID_API_KEY = os.getenv("RAPID_API_KEY")
MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SERVICE_ROLE_SECRET")

TIKTOK_MAX_QUALITY_HOST = "tiktok-max-quality.p.rapidapi.com"
TIKTOK_MAX_QUALITY_URL = f"https://{TIKTOK_MAX_QUALITY_HOST}/download"

# Watermark settings
LOGO_PATH = os.path.join(os.path.dirname(__file__), "jcam-logo.png")
WATERMARK_BANNER_HEIGHT = 100
WATERMARK_BANNER_POSITION = 5 / 7
WATERMARK_PADDING = 105
WATERMARK_TARGET_WIDTH = 1080
WATERMARK_TARGET_HEIGHT = 1920
WATERMARK_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Black.ttf"

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
    
    # Get all videos
    videos_response = supabase.table("videos").select("id, tiktok_video_id, tiktok_author_username, duration_seconds").execute()
    
    # Filter to only videos without any logod version (hidden or not)
    videos = [v for v in videos_response.data if v["id"] not in logod_video_ids]
    return videos


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
    except requests.RequestException:
        pass
    
    return None


def download_video(url: str, output_path: str) -> bool:
    """Download video from URL."""
    try:
        response = requests.get(url, stream=True, timeout=120)
        response.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception:
        return False


def add_watermark(input_path: str, output_path: str, author_username: str) -> bool:
    """Add j.cam watermark banner to video."""
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
    """Upload a video file directly to Mux."""
    import time
    
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
    
    with open(file_path, "rb") as f:
        upload_response = requests.put(
            upload_url,
            data=f,
            headers={"Content-Type": "video/mp4"},
        )
        upload_response.raise_for_status()
    
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


def save_logod_video(video_db_id: str, mux_data: dict, duration: float = None, file_size: int = None) -> dict:
    """Save watermarked video record to Supabase."""
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


def process_video(video: dict, index: int) -> dict:
    """Process a single video: download from TikTok, watermark, upload to Mux."""
    video_id = video["id"]
    tiktok_id = video["tiktok_video_id"]
    author = video["tiktok_author_username"] or "unknown"
    duration = video.get("duration_seconds")
    
    result = {"video_id": video_id, "success": False, "error": None}
    
    thread_print(f"{index}. Processing {tiktok_id} by @{author}")
    
    # Get fresh download URL from TikTok
    thread_print(f"   [{tiktok_id}] Fetching download URL...")
    video_url = get_max_quality_url(tiktok_id, author)
    if not video_url:
        result["error"] = "Could not get download URL"
        thread_print(f"   [{tiktok_id}] Could not get download URL")
        return result
    
    temp_dir = tempfile.gettempdir()
    original_path = os.path.join(temp_dir, f"tiktok_{tiktok_id}.mp4")
    watermarked_path = os.path.join(temp_dir, f"tiktok_{tiktok_id}_logod.mp4")
    
    try:
        # Download from TikTok
        thread_print(f"   [{tiktok_id}] Downloading from TikTok...")
        if not download_video(video_url, original_path):
            result["error"] = "Download failed"
            thread_print(f"   [{tiktok_id}] Download failed")
            return result
        
        file_size = os.path.getsize(original_path)
        thread_print(f"   [{tiktok_id}] Downloaded: {file_size / 1024 / 1024:.1f} MB")
        
        # Watermark
        thread_print(f"   [{tiktok_id}] Watermarking...")
        if not add_watermark(original_path, watermarked_path, author):
            result["error"] = "Watermarking failed"
            thread_print(f"   [{tiktok_id}] Watermarking failed")
            return result
        
        watermarked_size = os.path.getsize(watermarked_path)
        thread_print(f"   [{tiktok_id}] Watermarked: {watermarked_size / 1024 / 1024:.1f} MB")
        
        # Upload to Mux
        thread_print(f"   [{tiktok_id}] Uploading to Mux...")
        mux_result = upload_to_mux_direct(
            watermarked_path,
            passthrough=f"tiktok:{tiktok_id}:logod",
        )
        
        logod_mux_asset_id = mux_result.get("data", {}).get("id")
        thread_print(f"   [{tiktok_id}] Logod Mux Asset: {logod_mux_asset_id}")
        
        # Save to DB
        db_record = save_logod_video(video_id, mux_result, duration, watermarked_size)
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


def main(max_workers: int = 2):
    """Process all videos without logod versions."""
    videos = get_videos_without_logod()
    print(f"Found {len(videos)} videos without logod versions\n")
    
    if not videos:
        print("Nothing to process!")
        return
    
    total_success = 0
    total_failed = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_video, video, i): video["id"]
            for i, video in enumerate(videos, 1)
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
    main(max_workers=2)
