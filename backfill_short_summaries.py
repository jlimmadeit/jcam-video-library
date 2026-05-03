#!/usr/bin/env python3
"""
Backfill videos.short_summary from existing video_summary using Gemini (text only).
Produces a 4–8 word label per video; no MP4 download.
"""

import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from google import genai
from supabase import create_client, Client

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SERVICE_ROLE_SECRET")

GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
]

SHORT_SUMMARY_INSTRUCTION = """You are given an AI analysis of a short social video (description, keywords, and notes).

Reply with ONE line only: a readable label of 4 to 8 words (inclusive) that captures what the video is mainly about.
Rules:
- Plain words only, no leading/trailing punctuation, no quotation marks.
- Do not include hashtags or @mentions.
- Count words carefully: minimum 4 words, maximum 8 words."""

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


def get_videos_needing_short_summary(limit: int | None = None) -> list[dict]:
    supabase = get_supabase()
    query = (
        supabase.table("videos")
        .select("id, tiktok_video_id, tiktok_author_username, video_summary")
        .not_.is_("video_summary", "null")
        .is_("short_summary", "null")
        .order("created_at", desc=False)
    )
    if limit:
        query = query.limit(limit)
    return query.execute().data or []


def _clean_model_line(text: str) -> str:
    s = (text or "").strip()
    s = s.split("\n")[0].strip()
    s = s.strip(" \t\"'«»`")
    s = re.sub(r"^[\d#\-*.]+\s*", "", s)
    return s.strip()


def _word_count(s: str) -> int:
    return len(s.split()) if s else 0


def coerce_label(raw: str) -> str | None:
    s = _clean_model_line(raw)
    if not s:
        return None
    words = s.split()
    if len(words) > 8:
        s = " ".join(words[:8])
    if _word_count(s) < 4:
        return None
    return s


def generate_short_summary_text(video_summary: str, label: str) -> str | None:
    client = get_gemini_client()
    base = f"{SHORT_SUMMARY_INSTRUCTION}\n\n---\n\n{video_summary.strip()}"
    retry_extra = (
        "\n\nYour last line was not 4–8 words. Reply with exactly one line, 4 to 8 words, plain text only."
    )
    last_error = None
    for model in GEMINI_MODELS:
        for attempt in range(3):
            body = base if attempt == 0 else base + retry_extra
            try:
                response = client.models.generate_content(model=model, contents=body)
                text = (response.text or "").strip()
                thread_print(f"   [{label}] model={model} attempt={attempt + 1}")
                out = coerce_label(text)
                if out:
                    return out
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait = 20 * (attempt + 1)
                    thread_print(f"   [{label}] {model} rate limited, sleep {wait}s…")
                    time.sleep(wait)
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    thread_print(f"   [{label}] {model} unavailable, next model…")
                    break
                else:
                    thread_print(f"   [{label}] {model} error: {e}")
                    break
    thread_print(f"   [{label}] short summary failed: {last_error}")
    return None


def update_short_summary(video_id: str, short_summary: str):
    get_supabase().table("videos").update({"short_summary": short_summary}).eq("id", video_id).execute()


def process_row(row: dict, index: int, total: int) -> bool:
    vid = row["tiktok_video_id"]
    author = row.get("tiktok_author_username") or "unknown"
    thread_print(f"[{index}/{total}] {vid} (@{author})")
    short = generate_short_summary_text(row["video_summary"], vid)
    if not short:
        return False
    update_short_summary(row["id"], short)
    thread_print(f"   [{vid}] short_summary: {short!r}")
    return True


def main(max_workers: int = 4):
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = get_videos_needing_short_summary(limit=limit)
    total = len(rows)
    print(f"Found {total} videos with video_summary and no short_summary")
    print(f"Workers: {max_workers}, models: {', '.join(GEMINI_MODELS)}\n")
    if not rows:
        print("Nothing to do.")
        return
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(process_row, r, i, total): r["tiktok_video_id"] for i, r in enumerate(rows, 1)}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                if fut.result():
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                thread_print(f"   [{tid}] thread error: {e}")
                fail += 1
    print(f"\nDone. OK: {ok}, failed: {fail}")


if __name__ == "__main__":
    main(max_workers=4)
