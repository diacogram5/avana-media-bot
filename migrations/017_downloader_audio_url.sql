-- 017_downloader_audio_url: keep Instagram's separate DASH audio-stream URL
-- on the download job, so the downloader can mux audio+video into one MP4
-- (Instagram serves them as separate streams; the video URL alone is silent).
--
-- The column is NULL/'' for non-Instagram jobs and older rows; consumers
-- must treat a missing value as "no separate audio" and keep the previous
-- video-only behavior.
ALTER TABLE download_jobs ADD COLUMN audio_url TEXT NOT NULL DEFAULT '';
