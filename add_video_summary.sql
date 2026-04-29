-- Add video_summary column and make it searchable
-- Run this migration against your Supabase database

-- Add the video_summary column
ALTER TABLE videos ADD COLUMN IF NOT EXISTS video_summary TEXT;

COMMENT ON COLUMN videos.video_summary IS 'AI-generated summary: 3-sentence description + search keywords + text/speech transcription keywords';

-- Full text search index on video_summary (matches existing pattern for tiktok_description)
CREATE INDEX IF NOT EXISTS idx_videos_summary_fts
    ON videos USING GIN (to_tsvector('english'::regconfig, COALESCE(video_summary, '')));
