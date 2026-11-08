-- Verified file identity for the song file_id cache (full-song mismatch fix, 2026-11-09).
-- A wrong file was once stored under the RIGHT song key (identify said "ATM" /
-- "Tiem & Salii", the sent file was another song) and would have been re-sent
-- from cache forever. The file's own title/artist (from its ID3 tags, probed
-- at send time) is stored next to the file_id so a cache hit can be
-- re-checked before send. Rows stored before this migration have NULL
-- verified columns (legacy = unverified: never sent from cache, the fresh
-- lookup re-verifies and re-stores).
ALTER TABLE file_cache ADD COLUMN verified_title TEXT;
ALTER TABLE file_cache ADD COLUMN verified_artist TEXT;
