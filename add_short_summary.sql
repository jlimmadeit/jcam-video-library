-- Short card title derived from Gemini video_summary (4–8 words)
ALTER TABLE videos ADD COLUMN IF NOT EXISTS short_summary TEXT;

COMMENT ON COLUMN videos.short_summary IS 'Very short display label (4–8 words) distilled from video_summary; UI falls back to search_keyword';

-- Output row shape changed; drop old signature if present (float = real in some catalogs)
DROP FUNCTION IF EXISTS search_videos_by_embedding(vector(768), double precision, integer);
DROP FUNCTION IF EXISTS search_videos_by_embedding(vector(768), real, integer);

-- Embedding search: expose short_summary for grid / modal
CREATE OR REPLACE FUNCTION search_videos_by_embedding(
    query_embedding vector(768),
    match_threshold float DEFAULT 0.3,
    match_count int DEFAULT 50
)
RETURNS TABLE (
    id uuid,
    mux_playback_id text,
    mux_thumbnail_url text,
    mux_playback_url text,
    mux_gif_url text,
    status text,
    video_id uuid,
    duration_seconds numeric,
    similarity float,
    tiktok_description text,
    tiktok_hashtags text[],
    search_keyword text,
    short_summary text,
    category text,
    video_summary text,
    tiktok_view_count integer,
    tiktok_like_count integer,
    tiktok_comment_count integer,
    tiktok_share_count integer
)
LANGUAGE sql STABLE
AS $$
    SELECT
        lv.id,
        lv.mux_playback_id,
        lv.mux_thumbnail_url,
        lv.mux_playback_url,
        lv.mux_gif_url,
        lv.status,
        lv.video_id,
        lv.duration_seconds,
        1 - (v.summary_embedding <=> query_embedding) AS similarity,
        v.tiktok_description,
        v.tiktok_hashtags,
        v.search_keyword,
        v.short_summary,
        v.category,
        v.video_summary,
        v.tiktok_view_count,
        v.tiktok_like_count,
        v.tiktok_comment_count,
        v.tiktok_share_count
    FROM logod_videos lv
    JOIN videos v ON v.id = lv.video_id
    WHERE lv.status = 'ready'
      AND lv.downloaded_at IS NULL
      AND v.summary_embedding IS NOT NULL
      AND 1 - (v.summary_embedding <=> query_embedding) > match_threshold
    ORDER BY v.summary_embedding <=> query_embedding
    LIMIT match_count;
$$;
