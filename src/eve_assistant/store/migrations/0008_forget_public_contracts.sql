-- Public contracts name other players (issuers) and carry their titles. They
-- are no longer cached; remove any copies cached before that change.
DELETE FROM esi_cache WHERE cache_key LIKE 'GET /contracts/public/%';
