#!/usr/bin/env python3
"""Enable MP4 support on all existing Mux assets."""

import os
import requests
import time
from dotenv import load_dotenv

load_dotenv()

MUX_TOKEN_ID = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")


def enable_mp4_on_all_assets():
    """Enable MP4 support on all assets that don't have it."""
    # Get all assets
    response = requests.get(
        "https://api.mux.com/video/v1/assets",
        auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
        params={"limit": 100},
    )
    assets = response.json()["data"]
    
    print(f"Found {len(assets)} assets")
    
    updated = 0
    skipped = 0
    failed = 0
    
    for asset in assets:
        asset_id = asset["id"]
        mp4_support = asset.get("mp4_support", "none")
        
        if mp4_support != "none":
            print(f"  {asset_id}: Already has MP4 support ({mp4_support})")
            skipped += 1
            continue
        
        # Enable MP4 support
        update_response = requests.patch(
            f"https://api.mux.com/video/v1/assets/{asset_id}",
            json={"mp4_support": "capped-1080p"},
            auth=(MUX_TOKEN_ID, MUX_TOKEN_SECRET),
        )
        
        if update_response.status_code == 200:
            print(f"  {asset_id}: Enabled MP4 support")
            updated += 1
        else:
            print(f"  {asset_id}: Failed - {update_response.text[:100]}")
            failed += 1
        
        time.sleep(0.1)  # Rate limiting
    
    print(f"\nSummary:")
    print(f"  Updated: {updated}")
    print(f"  Skipped: {skipped}")
    print(f"  Failed: {failed}")


if __name__ == "__main__":
    enable_mp4_on_all_assets()
