#!/usr/bin/env python3
"""Download videos from Supabase (Mux MP4), apply logo banner, save to Desktop.

Default: 10 videos, excluding TikTok IDs already used in earlier demo runs.
Override: python demo_three_logod.py <count>  → that many videos, default excludes kept
  python demo_three_logod.py [skip] [count]  → skip/count from newest, no default excludes
  python demo_three_logod.py --problem-examples  → fixed set of regression / stress clips
  python demo_three_logod.py --ids id1,id2,...  → explicit TikTok video IDs (DB + Mux required)
  e.g. demo_three_logod.py 5; demo_three_logod.py 0 3; demo_three_logod.py --ids 7575767888204713230
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

from backfill_logod_videos import (  # noqa: E402
    add_watermark,
    download_video,
    get_mux_mp4_url,
)
from banner_placement import compute_banner_y_for_logod_video  # noqa: E402

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SERVICE_ROLE_SECRET")

# TikTok IDs already used in earlier desktop demo batches (skip for fresh tests).
EXCLUDE_TIKTOK_IDS_DEFAULT = (
    "7632890084408249614",
    "7618031569151741214",
    "7572786469056679188",
    "7627942863397014798",
    "7617286279742508318",
    "7594922150868356382",
    "7623558917519412494",
    "7612116029136751885",
    "7623928271062732045",
    "7629928356456254750",
    "7474280811425025302",
    "7351148086296726816",
    "7621440554668150030",
    "7628378916478078228",
    "7630668821078052114",
    "7514827570572070152",
    "7628971590235016462",
    "7521526402470923534",
    "7606796573846031638",
    "7518222355131387166",
    "7616152515234565389",
    "7634886086979816717",
    "7615638204825292062",
    "7575767888204713230",
    "7627211860210633997",
    "7379866284974001451",
    "7629422353868426527",
)

# Re-run with: python demo_three_logod.py --problem-examples
# (TikTok IDs; must exist in `videos` with mux_playback_id.)
PROBLEM_EXAMPLE_TIKTOK_IDS = (
    "7575767888204713230",  # @jackbanana — caption vs banner spacing (thread screenshots)
    "7632890084408249614",  # early placement / fusion stress clip
    "7621440554668150030",
    "7630668821078052114",
)


def path_to_file_uri(p: str | Path) -> str:
    """file:///... URL for click-to-open in Cursor, many terminals, and chat paste."""
    return Path(p).resolve().as_uri()


def print_osc8_link(url: str, label: str) -> None:
    """Terminal hyperlink (iTerm2, WezTerm, recent Terminal.app); harmless if ignored."""
    sys.stdout.write(f"\033]8;;{url}\033\\{label}\033]8;;\033\\\n")


def _rows_for_tiktok_ids(supabase, ids: tuple[str, ...]) -> list[dict]:
    """Fetch videos rows in the same order as `ids`; skip missing or no Mux."""
    if not ids:
        return []
    r = (
        supabase.table("videos")
        .select("id, tiktok_video_id, tiktok_author_username, mux_playback_id")
        .in_("tiktok_video_id", list(ids))
        .execute()
    )
    by_id = {str(row["tiktok_video_id"]): row for row in (r.data or [])}
    out: list[dict] = []
    for tid in ids:
        row = by_id.get(tid)
        if not row:
            print(f"[missing] No DB row for tiktok_video_id={tid}")
            continue
        if not row.get("mux_playback_id"):
            print(f"[missing] No mux_playback_id for {tid}")
            continue
        out.append(row)
    return out


def main() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE_URL and SERVICE_ROLE_SECRET must be set in .env")
        sys.exit(1)

    explicit_ids: tuple[str, ...] | None = None
    # argv: --problem-examples | --ids a,b,c | <count> | [skip] [count]
    if len(sys.argv) >= 2 and sys.argv[1] == "--problem-examples":
        explicit_ids = PROBLEM_EXAMPLE_TIKTOK_IDS
    elif len(sys.argv) >= 2 and sys.argv[1] == "--ids":
        if len(sys.argv) < 3:
            print("Usage: demo_three_logod.py --ids tiktok_id1,tiktok_id2,...")
            sys.exit(1)
        explicit_ids = tuple(s.strip() for s in sys.argv[2].split(",") if s.strip())
        if not explicit_ids:
            print("Usage: demo_three_logod.py --ids tiktok_id1,tiktok_id2,...")
            sys.exit(1)
    elif len(sys.argv) == 2 and sys.argv[1].isdigit():
        skip = 0
        need = int(sys.argv[1])
        exclude = EXCLUDE_TIKTOK_IDS_DEFAULT
    elif len(sys.argv) >= 3:
        skip = int(sys.argv[1])
        need = int(sys.argv[2])
        exclude = ()
    else:
        skip = 0
        need = 10
        exclude = EXCLUDE_TIKTOK_IDS_DEFAULT

    if explicit_ids is None:
        pass  # skip, need, exclude set above
    else:
        skip = 0
        need = len(explicit_ids)
        exclude = ()

    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        print(f"Desktop not found at {desktop}; create it or set HOME.")
        sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = desktop / f"jcam-logod-demo-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    if explicit_ids is not None:
        r_data = _rows_for_tiktok_ids(supabase, explicit_ids)
    else:
        q = (
            supabase.table("videos")
            .select("id, tiktok_video_id, tiktok_author_username, mux_playback_id")
            .not_.is_("mux_playback_id", "null")
            .order("created_at", desc=True)
        )
        if exclude:
            q = q.not_.in_("tiktok_video_id", list(exclude))
        pool = max(need * 25, 80)
        end = skip + pool - 1
        r = q.range(skip, end).execute()
        r_data = r.data or []

    if not r_data:
        print("No rows in videos with mux_playback_id (after filters).")
        sys.exit(1)

    folder_uri = path_to_file_uri(out_dir)
    print(f"Saving to Desktop: {out_dir}")
    if explicit_ids is not None:
        print(f"Explicit TikTok ID run ({len(r_data)} row(s) with Mux).\n")
    elif exclude:
        print(f"Excluding {len(exclude)} TikTok IDs from prior demo runs.\n")
    print("Folder (click file:// link in supported views):")
    print(f"  {folder_uri}\n")
    print_osc8_link(folder_uri, f"Open folder: {out_dir.name}")

    done = 0
    logod_paths: list[Path] = []
    for v in r_data:
        if explicit_ids is None and done >= need:
            break
        tid = v["tiktok_video_id"]
        auth = (v.get("tiktok_author_username") or "unknown").strip()
        pid = v["mux_playback_id"]
        idx = done + 1
        orig = out_dir / f"{idx}_original_{tid}.mp4"
        logod = out_dir / f"{idx}_logod_{tid}.mp4"

        url = get_mux_mp4_url(pid)
        if not url:
            print(f"[skip] {tid}: no Mux static MP4 URL")
            continue
        print(f"[{idx}] Downloading @{auth} / {tid} …")
        out_dir.mkdir(parents=True, exist_ok=True)
        ok, err = download_video(url, str(orig), str(tid))
        if not ok:
            print(f"[skip] {tid}: {err}")
            continue
        banner_y, meta = compute_banner_y_for_logod_video(str(orig), log_prefix=str(tid))
        ge = meta.get("gemini_error")
        if ge:
            print(f"      Gemini: {ge}")
        print(
            f"      banner_y={banner_y}, fused_regions={len(meta.get('regions', []))}, "
            f"refine_pass_b={meta.get('refine_count', 0)}"
        )
        if not add_watermark(str(orig), str(logod), auth, banner_y=banner_y):
            print(f"[skip] {tid}: watermark ffmpeg failed")
            continue
        u = path_to_file_uri(logod)
        print(f"      file: {u}")
        print_osc8_link(u, f"Open video {idx}: {logod.name}")
        print()
        logod_paths.append(logod)
        done += 1

    if done == 0:
        print("No videos were successfully processed.")
        sys.exit(1)

    if done < need:
        print(f"Warning: only {done}/{need} succeeded (pool exhausted or failures).\n")

    subprocess.run(["open", str(out_dir)], check=False)
    print("Opened this folder in Finder.")
    print("\nPaths (copy-paste or cmd-click):")
    print(f"  {out_dir}")
    for p in logod_paths:
        print(f"  {p}")


if __name__ == "__main__":
    main()
