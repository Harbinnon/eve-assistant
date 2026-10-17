-- One-off clean-up, approved by the repository owner: wallet snapshots
-- collected before snapshots.sanitise existed kept other players' IDs, their
-- names inside journal descriptions, and player-written journal reasons.
-- Strip the same fields sanitise strips now. Amounts, dates, types and
-- reference types are untouched. The append-only trigger is lifted only for
-- this migration and restored at the end (it runs in one transaction).
--
-- content_hash is left as it was, so the next collection of each kind writes
-- one fresh (cleaned) row; nothing else depends on the old hashes.

DROP TRIGGER snapshots_no_update;

UPDATE snapshots
SET payload = (
    SELECT json_group_array(json(json_remove(value, '$.client_id')))
    FROM json_each(snapshots.payload)
)
WHERE kind = 'wallet_transactions' AND json_type(payload) = 'array';

-- Removing a path that doesn't exist ('$.kept') changes nothing, which is
-- how each CASE keeps a field.
UPDATE snapshots
SET payload = (
    SELECT json_group_array(json(json_remove(
        value,
        '$.description',
        '$.reason',
        CASE WHEN json_extract(value, '$.first_party_id') IN (
            SELECT character_id FROM characters
            UNION SELECT corporation_id FROM sde_npc_corporations
        ) THEN '$.kept' ELSE '$.first_party_id' END,
        CASE WHEN json_extract(value, '$.second_party_id') IN (
            SELECT character_id FROM characters
            UNION SELECT corporation_id FROM sde_npc_corporations
        ) THEN '$.kept' ELSE '$.second_party_id' END,
        CASE WHEN json_extract(value, '$.tax_receiver_id') IN (
            SELECT character_id FROM characters
            UNION SELECT corporation_id FROM sde_npc_corporations
        ) THEN '$.kept' ELSE '$.tax_receiver_id' END,
        CASE WHEN json_extract(value, '$.context_id_type')
                  IN ('character_id', 'corporation_id', 'alliance_id')
             AND json_extract(value, '$.context_id') NOT IN (
                 SELECT character_id FROM characters
                 UNION SELECT corporation_id FROM sde_npc_corporations
             )
        THEN '$.context_id' ELSE '$.kept' END,
        CASE WHEN json_extract(value, '$.context_id_type')
                  IN ('character_id', 'corporation_id', 'alliance_id')
             AND json_extract(value, '$.context_id') NOT IN (
                 SELECT character_id FROM characters
                 UNION SELECT corporation_id FROM sde_npc_corporations
             )
        THEN '$.context_id_type' ELSE '$.kept' END
    )))
    FROM json_each(snapshots.payload)
)
WHERE kind = 'wallet_journal' AND json_type(payload) = 'array';

CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;

-- Raw copies in the ESI cache; these endpoints are no longer cached.
DELETE FROM esi_cache
WHERE cache_key LIKE '%/wallet/journal%' OR cache_key LIKE '%/wallet/transactions%';
