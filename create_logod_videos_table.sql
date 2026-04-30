-- Create table for storing watermarked/logo'd versions of videos
CREATE TABLE logod_videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Foreign key to original video
    video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    
    -- Mux metadata for the watermarked version
    mux_asset_id TEXT UNIQUE,
    mux_playback_id TEXT,
    mux_playback_url TEXT GENERATED ALWAYS AS (
        CASE WHEN mux_playback_id IS NOT NULL 
        THEN 'https://stream.mux.com/' || mux_playback_id || '.m3u8'
        ELSE NULL END
    ) STORED,
    mux_thumbnail_url TEXT GENERATED ALWAYS AS (
        CASE WHEN mux_playback_id IS NOT NULL 
        THEN 'https://image.mux.com/' || mux_playback_id || '/thumbnail.jpg?width=400&height=710&fit_mode=smartcrop'
        ELSE NULL END
    ) STORED,
    mux_gif_url TEXT GENERATED ALWAYS AS (
        CASE WHEN mux_playback_id IS NOT NULL 
        THEN 'https://image.mux.com/' || mux_playback_id || '/animated.webp?width=320&fps=10&end=4'
        ELSE NULL END
    ) STORED,
    mux_status TEXT DEFAULT 'preparing',
    
    -- Watermark settings used
    watermark_type TEXT DEFAULT 'jcam_banner',
    banner_height INTEGER DEFAULT 100,
    banner_position NUMERIC(4, 3) DEFAULT 0.714,  -- 5/7 = ~0.714
    logo_padding INTEGER DEFAULT 105,
    
    -- Output video properties
    width INTEGER DEFAULT 1080,
    height INTEGER DEFAULT 1920,
    duration_seconds NUMERIC(10, 2),
    file_size_bytes BIGINT,
    
    -- Processing status
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    error_message TEXT,
    
    -- Soft-delete
    is_hidden BOOLEAN DEFAULT FALSE,
    
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

-- Indexes for performance
CREATE INDEX idx_logod_videos_video_id ON logod_videos(video_id);
CREATE INDEX idx_logod_videos_status ON logod_videos(status);
CREATE INDEX idx_logod_videos_mux_asset_id ON logod_videos(mux_asset_id);
CREATE INDEX idx_logod_videos_created_at ON logod_videos(created_at DESC);
CREATE INDEX idx_logod_videos_is_hidden ON logod_videos(is_hidden) WHERE is_hidden = TRUE;

-- Trigger to auto-update updated_at
CREATE TRIGGER logod_videos_updated_at
    BEFORE UPDATE ON logod_videos
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE logod_videos IS 'Watermarked versions of TikTok videos with j.cam branding';
COMMENT ON COLUMN logod_videos.video_id IS 'Reference to the original video in videos table';
COMMENT ON COLUMN logod_videos.watermark_type IS 'Type of watermark applied (e.g., jcam_banner)';
COMMENT ON COLUMN logod_videos.banner_position IS 'Vertical position of banner as fraction (5/7 = 0.714)';
