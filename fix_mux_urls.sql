-- Update Mux URL columns
-- MP4 downloads only work for assets created with mp4_support enabled
-- For existing assets, use HLS streaming with max_resolution parameter

-- Drop MP4 columns if they exist (they won't work for old assets)
ALTER TABLE videos DROP COLUMN IF EXISTS mux_mp4_url;
ALTER TABLE videos DROP COLUMN IF EXISTS mux_mp4_medium_url;

-- Update playback URL to force 1080p
ALTER TABLE videos DROP COLUMN IF EXISTS mux_playback_url;
ALTER TABLE videos ADD COLUMN mux_playback_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://stream.mux.com/' || mux_playback_id || '.m3u8?max_resolution=1080p'
    ELSE NULL END
) STORED;

-- Update thumbnail to HD
ALTER TABLE videos DROP COLUMN IF EXISTS mux_thumbnail_url;
ALTER TABLE videos ADD COLUMN mux_thumbnail_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://image.mux.com/' || mux_playback_id || '/thumbnail.jpg?width=1080'
    ELSE NULL END
) STORED;

-- Keep gif URL
ALTER TABLE videos DROP COLUMN IF EXISTS mux_gif_url;
ALTER TABLE videos ADD COLUMN mux_gif_url TEXT GENERATED ALWAYS AS (
    CASE WHEN mux_playback_id IS NOT NULL 
    THEN 'https://image.mux.com/' || mux_playback_id || '/animated.gif?width=480'
    ELSE NULL END
) STORED;

-- Verify
SELECT 
    tiktok_video_id,
    width || 'x' || height as resolution,
    mux_playback_url,
    mux_thumbnail_url
FROM videos
LIMIT 3;
