-- Track each user download of a watermarked (logod) video.
-- download_count on logod_videos is maintained by trigger for fast reads in the app.

ALTER TABLE logod_videos
    ADD COLUMN IF NOT EXISTS download_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS logod_video_downloads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    logod_video_id UUID NOT NULL REFERENCES logod_videos (id) ON DELETE CASCADE,
    user_id UUID REFERENCES auth.users (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_logod_video_downloads_logod_video_id
    ON logod_video_downloads (logod_video_id);

CREATE INDEX IF NOT EXISTS idx_logod_video_downloads_created_at
    ON logod_video_downloads (created_at DESC);

CREATE OR REPLACE FUNCTION bump_logod_video_download_count()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE logod_videos
    SET download_count = COALESCE(download_count, 0) + 1
    WHERE id = NEW.logod_video_id;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS logod_video_downloads_bump_count ON logod_video_downloads;
CREATE TRIGGER logod_video_downloads_bump_count
    AFTER INSERT ON logod_video_downloads
    FOR EACH ROW
    EXECUTE FUNCTION bump_logod_video_download_count();

COMMENT ON TABLE logod_video_downloads IS 'One row per completed in-app MP4 download (authenticated users)';
COMMENT ON COLUMN logod_video_downloads.user_id IS 'Supabase auth user who downloaded; NULL if you ever allow anonymous logging';

-- Row Level Security (Supabase): authenticated users insert their own download events.
ALTER TABLE logod_video_downloads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can record their own downloads" ON logod_video_downloads;
CREATE POLICY "Users can record their own downloads"
    ON logod_video_downloads
    FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

-- Optional: restrict reading raw events to service role only (default: no SELECT policy for anon/authenticated).
