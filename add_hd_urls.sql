-- Add HD URL columns to videos table
-- Run this in Supabase SQL editor

-- Add HD streaming URL (forces max 1080p quality)
ALTER TABLE videos 
DROP COLUMN IF EXISTS mux_playback_url;

ALTER TABLE videos 
ADD COLUMN mux_playback_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://stream.mux.com/' || mux_playback_id || '.m3u8?max_resolution=1080p'
    ELSE NULL END
) STORED;

-- Add direct MP4 download URL (highest quality)
ALTER TABLE videos 
ADD COLUMN IF NOT EXISTS mux_mp4_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://stream.mux.com/' || mux_playback_id || '/high.mp4'
    ELSE NULL END
) STORED;

-- Add medium quality MP4 URL (for mobile/lower bandwidth)
ALTER TABLE videos 
ADD COLUMN IF NOT EXISTS mux_mp4_medium_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://stream.mux.com/' || mux_playback_id || '/medium.mp4'
    ELSE NULL END
) STORED;

-- Update thumbnail to use higher quality
ALTER TABLE videos 
DROP COLUMN IF EXISTS mux_thumbnail_url;

ALTER TABLE videos 
ADD COLUMN mux_thumbnail_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://image.mux.com/' || mux_playback_id || '/thumbnail.jpg?width=1080'
    ELSE NULL END
) STORED;

-- Verify the changes
SELECT 
    tiktok_video_id,
    width,
    height,
    mux_playback_url,
    mux_mp4_url,
    mux_thumbnail_url
FROM videos
LIMIT 3;
