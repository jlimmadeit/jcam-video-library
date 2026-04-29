-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Add embedding column (768 dimensions via gemini-embedding-001)
ALTER TABLE videos ADD COLUMN IF NOT EXISTS summary_embedding vector(768);

-- IVFFlat index for fast cosine similarity search
-- Use lists = sqrt(n) as a starting point; 10 is fine for < 1000 videos
CREATE INDEX IF NOT EXISTS idx_videos_summary_embedding
    ON videos USING ivfflat (summary_embedding vector_cosine_ops)
    WITH (lists = 10);

-- RPC function: search videos by embedding similarity
-- Returns logod_videos joined with parent videos, ranked by cosine similarity
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
    similarity float,
    tiktok_description text,
    tiktok_hashtags text[],
    search_keyword text,
    category text,
    video_summary text
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
        1 - (v.summary_embedding <=> query_embedding) AS similarity,
        v.tiktok_description,
        v.tiktok_hashtags,
        v.search_keyword,
        v.category,
        v.video_summary
    FROM logod_videos lv
    JOIN videos v ON v.id = lv.video_id
    WHERE lv.status = 'ready'
      AND lv.downloaded_at IS NULL
      AND v.summary_embedding IS NOT NULL
      AND 1 - (v.summary_embedding <=> query_embedding) > match_threshold
    ORDER BY v.summary_embedding <=> query_embedding
    LIMIT match_count;
$$;
