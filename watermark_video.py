#!/usr/bin/env python3
"""
Download a TikTok video and add a j.cam watermark with white banner.
"""

import os
import subprocess
import tempfile
import requests
from dotenv import load_dotenv

load_dotenv()

RAPID_API_KEY = os.getenv("RAPID_API_KEY")
TIKTOK_MAX_QUALITY_HOST = "tiktok-max-quality.p.rapidapi.com"
TIKTOK_MAX_QUALITY_URL = f"https://{TIKTOK_MAX_QUALITY_HOST}/download"

LOGO_PATH = os.path.join(os.path.dirname(__file__), "j.cam 400x160.png")


def get_video_url(tiktok_url: str) -> str | None:
    """Get the download URL for a TikTok video."""
    headers = {
        "x-rapidapi-key": RAPID_API_KEY,
        "x-rapidapi-host": TIKTOK_MAX_QUALITY_HOST,
    }
    
    response = requests.get(TIKTOK_MAX_QUALITY_URL, headers=headers, params={"url": tiktok_url})
    response.raise_for_status()
    data = response.json()
    
    if data.get("status") == "success":
        # Try off_url first (original quality), then download_url
        for key in ["off_url", "download_url"]:
            url = data.get(key)
            if url:
                return url
    return None


def download_video(url: str, output_path: str) -> bool:
    """Download video from URL."""
    print(f"Downloading video...")
    response = requests.get(url, stream=True, timeout=120)
    response.raise_for_status()
    
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"Downloaded: {size_mb:.1f} MB")
    return True


def add_watermark(input_path: str, output_path: str, logo_path: str, banner_height: int = 100) -> bool:
    """
    Add a white banner with logo watermark to video.
    Banner is positioned at 3/5 (60%) down the video.
    Logo is centered on the banner and scaled to fit.
    """
    # First, get video dimensions
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        input_path
    ]
    result = subprocess.run(probe_cmd, capture_output=True, text=True)
    width, height = map(int, result.stdout.strip().split(","))
    print(f"Video dimensions: {width}x{height}")
    
    # Calculate banner position (5/7 down from top)
    banner_y = int(height * 5 / 7)
    
    # Scale logo to fit banner (leave some padding)
    logo_height = banner_height - 20  # 10px padding top and bottom
    
    # FFmpeg filter to:
    # 1. Draw white rectangle (banner) at 3/5 down
    # 2. Overlay scaled logo centered on the banner
    filter_complex = (
        f"[0:v]drawbox=x=0:y={banner_y}:w={width}:h={banner_height}:color=white:t=fill[bg];"
        f"[1:v]scale=-1:{logo_height}[logo];"
        f"[bg][logo]overlay=(W-w)/2:{banner_y}+(({banner_height}-h)/2)"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", logo_path,
        "-filter_complex", filter_complex,
        "-c:a", "copy",
        output_path
    ]
    
    print(f"Adding watermark...")
    print(f"  Banner at y={banner_y} (3/5 of {height})")
    print(f"  Banner height: {banner_height}px")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"FFmpeg error: {result.stderr}")
        return False
    
    print(f"Watermarked video saved to: {output_path}")
    return True


def process_tiktok(tiktok_url: str, output_path: str = "watermarked_video.mp4", banner_height: int = 200):
    """Download a TikTok video and add watermark."""
    print(f"Processing: {tiktok_url}")
    
    # Get download URL
    video_url = get_video_url(tiktok_url)
    if not video_url:
        print("Failed to get video URL")
        return False
    
    # Download to temp file
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        temp_path = tmp.name
    
    try:
        download_video(video_url, temp_path)
        add_watermark(temp_path, output_path, LOGO_PATH, banner_height)
        return True
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python watermark_video.py <tiktok_url> [output_path] [banner_height]")
        print("Example: python watermark_video.py https://www.tiktok.com/@user/video/123 output.mp4 200")
        sys.exit(1)
    
    tiktok_url = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "watermarked_video.mp4"
    banner_height = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    
    process_tiktok(tiktok_url, output_path, banner_height)
