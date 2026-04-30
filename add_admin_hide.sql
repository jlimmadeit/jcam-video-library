-- Add is_hidden column to logod_videos for soft-delete
ALTER TABLE logod_videos ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_logod_videos_is_hidden ON logod_videos(is_hidden) WHERE is_hidden = TRUE;
