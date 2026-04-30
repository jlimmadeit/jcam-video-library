#!/usr/bin/env python3
"""
Backfill video_summary for all videos using Gemini 2.5 Flash.
Downloads each video's MP4 from Mux, sends to Gemini for analysis,
and stores the summary in the video_summary column.
Uses multithreading for parallel processing.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google import genai
from supabase import create_client, Client

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SERVICE_ROLE_SECRET")

GEMINI_MODELS = [
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
]
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768

PROMPT = """Analyze this video and return the following in plain text (no markdown formatting):

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

print_lock = threading.Lock()
_thread_local = threading.local()


def thread_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def get_supabase() -> Client:
    if not hasattr(_thread_local, "supabase"):
        _thread_local.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _thread_local.supabase


def get_gemini_client() -> genai.Client:
    if not hasattr(_thread_local, "gemini"):
        _thread_local.gemini = genai.Client(api_key=GEMINI_API_KEY)
    return _thread_local.gemini


def get_videos_needing_summary(limit: int = None) -> list[dict]:
    """Fetch videos that need a summary or embedding."""
    supabase = get_supabase()
    query = (
        supabase.table("videos")
        .select("id, tiktok_video_id, tiktok_author_username, mux_playback_id, tiktok_description, video_summary")
        .not_.is_("mux_playback_id", "null")
        .or_("video_summary.is.null,summary_embedding.is.null")
        .order("created_at", desc=False)
    )
    if limit:
        query = query.limit(limit)
    response = query.execute()
    return response.data


def download_mp4(playback_id: str, video_id: str) -> str | None:
    """Download video from Mux HLS stream via ffmpeg."""
    hls_url = f"https://stream.mux.com/{playback_id}.m3u8"
    temp_path = os.path.join(tempfile.gettempdir(), f"gemini_{video_id}.mp4")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", hls_url, "-c", "copy", "-t", "120", temp_path],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not os.path.exists(temp_path):
            thread_print(f"   ffmpeg failed: {result.stderr[:200]}")
            return None
        size_mb = os.path.getsize(temp_path) / 1024 / 1024
        thread_print(f"   Downloaded {size_mb:.1f} MB via HLS")
        return temp_path
    except Exception as e:
        thread_print(f"   Download failed: {e}")
        return None


def analyze_video(file_path: str, video_id: str) -> str | None:
    """Upload video to Gemini and get the summary. Tries fallback models on failure."""
    client = get_gemini_client()
    try:
        uploaded = client.files.upload(file=file_path)
        for _ in range(60):
            status = client.files.get(name=uploaded.name)
            if status.state.name == "ACTIVE":
                break
            time.sleep(2)
        else:
            thread_print(f"   [{video_id}] File processing timed out")
            return None
    except Exception as e:
        thread_print(f"   [{video_id}] Upload error: {e}")
        return None

    last_error = None
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[uploaded, PROMPT],
                )
                thread_print(f"   [{video_id}] Used model: {model}")
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    pass
                return response.text
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait = 30 * (attempt + 1)
                    thread_print(f"   [{video_id}] {model} rate limited, waiting {wait}s (attempt {attempt + 1}/3)...")
                    time.sleep(wait)
                else:
                    thread_print(f"   [{video_id}] {model} failed: {e}")
                    break

    thread_print(f"   [{video_id}] All models failed: {last_error}")
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass
    return None


def generate_embedding(text: str, video_id: str) -> list[float] | None:
    """Generate a vector embedding for the given text."""
    from google.genai import types
    client = get_gemini_client()
    try:
        result = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIMENSIONS),
        )
        return result.embeddings[0].values
    except Exception as e:
        thread_print(f"   [{video_id}] Embedding error: {e}")
        return None


def update_video(video_id: str, summary: str, embedding: list[float] | None):
    """Write summary and embedding back to Supabase."""
    supabase = get_supabase()
    update_data = {"video_summary": summary}
    if embedding:
        update_data["summary_embedding"] = embedding
    supabase.table("videos").update(update_data).eq("id", video_id).execute()


def process_video(video: dict, index: int, total: int) -> bool:
    """Process a single video: download, analyze, embed, update DB."""
    vid = video["tiktok_video_id"]
    author = video.get("tiktok_author_username") or "unknown"
    playback_id = video["mux_playback_id"]
    existing_summary = video.get("video_summary")
    thread_print(f"[{index}/{total}] {vid} by @{author}")

    if existing_summary:
        thread_print(f"   [{vid}] Summary exists, generating embedding only...")
        embedding = generate_embedding(existing_summary, vid)
        if embedding:
            update_video(video["id"], existing_summary, embedding)
            thread_print(f"   [{vid}] Embedding: {EMBEDDING_DIMENSIONS}d vector generated")
            return True
        return False

    mp4_path = download_mp4(playback_id, vid)
    if not mp4_path:
        return False

    try:
        summary = analyze_video(mp4_path, vid)
        if summary:
            embedding = generate_embedding(summary, vid)
            update_video(video["id"], summary, embedding)
            preview = summary[:120].replace("\n", " ")
            thread_print(f"   [{vid}] Summary: {preview}...")
            if embedding:
                thread_print(f"   [{vid}] Embedding: {EMBEDDING_DIMENSIONS}d vector generated")
            return True
        else:
            thread_print(f"   [{vid}] No summary returned")
            return False
    finally:
        if os.path.exists(mp4_path):
            os.remove(mp4_path)


def main(max_workers: int = 3):
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    videos = get_videos_needing_summary(limit=limit)
    total = len(videos)
    print(f"Found {total} videos needing summaries")
    print(f"Using {max_workers} worker threads")
    print(f"Models (in priority order): {', '.join(GEMINI_MODELS)}\n")

    if not videos:
        return

    success = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_video, video, i, total): video["tiktok_video_id"]
            for i, video in enumerate(videos, 1)
        }

        for future in as_completed(futures):
            vid = futures[future]
            try:
                if future.result():
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                thread_print(f"   [{vid}] Thread error: {e}")
                failed += 1

    print(f"\n{'='*50}")
    print(f"Done. Success: {success}, Failed: {failed}")


if __name__ == "__main__":
    main(max_workers=3)
