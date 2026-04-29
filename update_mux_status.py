#!/usr/bin/env python3
"""
Update Mux asset status for videos and logod_videos tables.
Polls Mux API and updates status when assets become ready.
"""

import os
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def get_mux_asset_status(asset_id: str) -> dict | None:
    """Get asset details from Mux API."""
    try:
        response = requests.get(
            f"https://api.mux.com/video/v1/assets/{asset_id}",
            auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
        )
        if response.status_code == 200:
            return response.json().get("data", {})
    except Exception as e:
        print(f"  Error fetching {asset_id}: {e}")
    return None


def update_videos_table():
    """Update status for videos table."""
    print("=== Updating videos table ===")
    
    # Get videos that are not ready
    response = supabase.table("videos").select(
        "id, tiktok_video_id, mux_asset_id, mux_status"
    ).neq("mux_status", "ready").not_.is_("mux_asset_id", "null").execute()
    
    videos = response.data
    print(f"Found {len(videos)} videos to check\n")
    
    updated = 0
    for video in videos:
        asset_id = video["mux_asset_id"]
        tiktok_id = video["tiktok_video_id"]
        current_status = video["mux_status"]
        
        asset = get_mux_asset_status(asset_id)
        if not asset:
            continue
        
        new_status = asset.get("status")
        playback_ids = asset.get("playback_ids", [])
        playback_id = playback_ids[0]["id"] if playback_ids else None
        
        if new_status != current_status:
            print(f"  {tiktok_id}: {current_status} -> {new_status}")
            
            update_data = {"mux_status": new_status}
            if playback_id:
                update_data["mux_playback_id"] = playback_id
            if new_status == "ready":
                update_data["status"] = "ready"
            
            supabase.table("videos").update(update_data).eq("id", video["id"]).execute()
            updated += 1
    
    print(f"\nUpdated {updated} videos\n")


def update_logod_videos_table():
    """Update status for logod_videos table."""
    print("=== Updating logod_videos table ===")
    
    # Get logod_videos that are not ready
    response = supabase.table("logod_videos").select(
        "id, video_id, mux_asset_id, mux_status"
    ).neq("mux_status", "ready").not_.is_("mux_asset_id", "null").execute()
    
    logod_videos = response.data
    print(f"Found {len(logod_videos)} logod_videos to check\n")
    
    updated = 0
    for logod in logod_videos:
        asset_id = logod["mux_asset_id"]
        current_status = logod["mux_status"]
        
        asset = get_mux_asset_status(asset_id)
        if not asset:
            continue
        
        new_status = asset.get("status")
        playback_ids = asset.get("playback_ids", [])
        playback_id = playback_ids[0]["id"] if playback_ids else None
        
        if new_status != current_status:
            print(f"  {logod['id'][:8]}...: {current_status} -> {new_status}")
            
            update_data = {"mux_status": new_status}
            if playback_id:
                update_data["mux_playback_id"] = playback_id
            if new_status == "ready":
                update_data["status"] = "ready"
            
            supabase.table("logod_videos").update(update_data).eq("id", logod["id"]).execute()
            updated += 1
    
    print(f"\nUpdated {updated} logod_videos\n")


def main():
    update_videos_table()
    update_logod_videos_table()
    print("Done!")


if __name__ == "__main__":
    main()
