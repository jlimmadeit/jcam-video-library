-- Create table for storing TikTok videos uploaded to Mux
CREATE TABLE videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- TikTok metadata
    tiktok_video_id TEXT NOT NULL UNIQUE,
    tiktok_url TEXT NOT NULL,
    tiktok_author_id TEXT,
    tiktok_author_username TEXT,
    tiktok_description TEXT,
    tiktok_hashtags TEXT[],
    tiktok_music_title TEXT,
    tiktok_music_author TEXT,
    tiktok_like_count INTEGER,
    tiktok_comment_count INTEGER,
    tiktok_share_count INTEGER,
    tiktok_view_count INTEGER,
    tiktok_created_at TIMESTAMPTZ,
    
    -- Search/categorization
    search_keyword TEXT NOT NULL,
    category TEXT,
    
    -- Mux metadata
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
    
    -- Video properties
    width INTEGER,
    height INTEGER,
    duration_seconds NUMERIC(10, 2),
    aspect_ratio TEXT GENERATED ALWAYS AS (
        CASE 
            WHEN width IS NOT NULL AND height IS NOT NULL AND height > 0
            THEN ROUND(width::NUMERIC / height::NUMERIC, 2)::TEXT
            ELSE NULL 
        END
    ) STORED,
    file_size_bytes BIGINT,
    
    -- Processing status
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'deleted')),
    error_message TEXT,
    
    -- AI-generated summary (Gemini 2.5 Flash)
    video_summary TEXT,

    -- Moderation/curation
    is_approved BOOLEAN DEFAULT FALSE,
    is_featured BOOLEAN DEFAULT FALSE,
    is_hidden BOOLEAN DEFAULT FALSE,
    moderation_notes TEXT,
    
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    
    -- Indexes for common queries
    CONSTRAINT valid_dimensions CHECK (
        (width IS NULL AND height IS NULL) OR 
        (width > 0 AND height > 0)
    )
);

-- Indexes for performance
CREATE INDEX idx_videos_search_keyword ON videos(search_keyword);
CREATE INDEX idx_videos_category ON videos(category);
CREATE INDEX idx_videos_status ON videos(status);
CREATE INDEX idx_videos_mux_asset_id ON videos(mux_asset_id);
CREATE INDEX idx_videos_created_at ON videos(created_at DESC);
CREATE INDEX idx_videos_is_approved ON videos(is_approved) WHERE is_approved = TRUE;
CREATE INDEX idx_videos_tiktok_author ON videos(tiktok_author_username);

-- Full text search on description
CREATE INDEX idx_videos_description_fts ON videos USING GIN (to_tsvector('english'::regconfig, COALESCE(tiktok_description, '')));

-- Full text search on video_summary
CREATE INDEX idx_videos_summary_fts ON videos USING GIN (to_tsvector('english'::regconfig, COALESCE(video_summary, '')));

-- Trigger to auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER videos_updated_at
    BEFORE UPDATE ON videos
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Enable Row Level Security (optional, uncomment if needed)
-- ALTER TABLE videos ENABLE ROW LEVEL SECURITY;

-- Example RLS policy for public read access
-- CREATE POLICY "Public videos are viewable by everyone"
--     ON videos FOR SELECT
--     USING (is_approved = TRUE AND is_hidden = FALSE);

COMMENT ON TABLE videos IS 'TikTok videos discovered via search and uploaded to Mux';
COMMENT ON COLUMN videos.tiktok_video_id IS 'Unique video ID from TikTok';
COMMENT ON COLUMN videos.mux_playback_id IS 'Mux playback ID for streaming';
COMMENT ON COLUMN videos.mux_status IS 'Mux asset status: preparing, ready, errored';
COMMENT ON COLUMN videos.status IS 'Our processing status: pending, processing, ready, failed, deleted';
