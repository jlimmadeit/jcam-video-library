#!/usr/bin/env python3
"""
Backfill summary_embedding for videos that have a video_summary but no embedding.
Fast — no video downloads needed, just calls Gemini embedding API on existing text.
"""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google import genai
from google.genai import types
from supabase import create_client, Client

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768

print_lock = threading.Lock()
_thread_local = threading.local()


def thread_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def get_supabase() -> Client:
    if not hasattr(_thread_local, "supabase"):
        _thread_local.supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _thread_local.supabase


def get_gemini_client() -> genai.Client:
    if not hasattr(_thread_local, "gemini"):
        _thread_local.gemini = genai.Client(api_key=GEMINI_API_KEY)
    return _thread_local.gemini


def get_videos_needing_embeddings(limit: int = None) -> list[dict]:
    """Fetch videos that have a summary but no embedding."""
    supabase = get_supabase()
    query = (
        supabase.table("videos")
        .select("id, tiktok_video_id, tiktok_author_username, video_summary")
        .not_.is_("video_summary", "null")
        .is_("summary_embedding", "null")
        .order("created_at", desc=False)
    )
    if limit:
        query = query.limit(limit)
    response = query.execute()
    return response.data


def generate_embedding(text: str, video_id: str) -> list[float] | None:
    """Generate a 768d vector embedding for the given text."""
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


def update_embedding(video_id: str, embedding: list[float]):
    """Write the embedding back to Supabase."""
    supabase = get_supabase()
    supabase.table("videos").update({"summary_embedding": embedding}).eq("id", video_id).execute()


def process_video(video: dict, index: int, total: int) -> bool:
    """Generate embedding for a single video's summary."""
    vid = video["tiktok_video_id"]
    author = video.get("tiktok_author_username") or "unknown"
    summary = video["video_summary"]

    thread_print(f"[{index}/{total}] {vid} by @{author}")

    embedding = generate_embedding(summary, vid)
    if embedding:
        update_embedding(video["id"], embedding)
        thread_print(f"   [{vid}] Embedding: {EMBEDDING_DIMENSIONS}d vector stored")
        return True

    return False


def main(max_workers: int = 5):
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    videos = get_videos_needing_embeddings(limit=limit)
    total = len(videos)
    print(f"Found {total} videos with summaries but no embeddings")
    print(f"Using {max_workers} worker threads")
    print(f"Embedding model: {EMBEDDING_MODEL} ({EMBEDDING_DIMENSIONS}d)\n")

    if not videos:
        print("Nothing to do!")
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
    main(max_workers=5)
