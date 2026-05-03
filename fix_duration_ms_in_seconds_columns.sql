-- One-time fix: TikTok RapidAPI ``video.duration`` (milliseconds) was written into
-- ``duration_seconds`` on ``videos`` and ``logod_videos``. Scale obvious outliers.
-- Review before running; skip if you store legitimately long clips as seconds in this range.

UPDATE videos
SET duration_seconds = duration_seconds / 1000
WHERE duration_seconds IS NOT NULL
  AND duration_seconds > 3600
  AND duration_seconds < 2000000;

UPDATE logod_videos
SET duration_seconds = duration_seconds / 1000
WHERE duration_seconds IS NOT NULL
  AND duration_seconds > 3600
  AND duration_seconds < 2000000;
