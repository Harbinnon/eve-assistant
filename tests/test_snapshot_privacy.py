import json
import sqlite3

import httpx
import pytest
import respx

from eve_assistant import snapshots, store
from eve_assistant.esi.client import EsiClient

BASE = "https://esi.test"
OWNER, ALT, STRANGER, SCC = 7, 8, 95094576, 1000132

JOURNAL = [
    {
        "id": 1,
        "date": "2026-10-16T03:33:48Z",
        "ref_type": "market_transaction",
        "amount": 516500.0,
        "balance": 917423340.58,
        "description": "Market: Some Buyer bought stuff from Owner",
        "reason": "",
        "first_party_id": STRANGER,
        "second_party_id": OWNER,
        "context_id": 6894785035,
        "context_id_type": "market_transaction_id",
    },
    {
        "id": 2,
        "date": "2026-10-16T03:33:48Z",
        "ref_type": "transaction_tax",
        "amount": -38737.5,
        "balance": 917384603.08,
        "tax": 0.0,
        "tax_receiver_id": SCC,
        "description": "Sales tax paid to the SCC",
        "first_party_id": OWNER,
        "second_party_id": SCC,
    },
    {
        "id": 3,
        "date": "2026-10-16T04:00:00Z",
        "ref_type": "player_donation",
        "amount": 1_000_000.0,
        "balance": 918384603.08,
        "description": "Some Stranger deposited cash into Owner's account",
        "reason": "player-written message",
        "first_party_id": STRANGER,
        "second_party_id": OWNER,
        "context_id": STRANGER,
        "context_id_type": "character_id",
    },
    {
        "id": 4,
        "date": "2026-10-16T05:00:00Z",
        "ref_type": "player_donation",
        "amount": 5.0,
        "balance": 918384608.08,
        "first_party_id": ALT,
        "second_party_id": OWNER,
        "context_id": ALT,
        "context_id_type": "character_id",
    },
]
TRANSACTIONS = [
    {
        "client_id": STRANGER,
        "date": "2026-10-16T03:33:48Z",
        "is_buy": False,
        "is_personal": True,
        "journal_ref_id": 1,
        "location_id": 60003760,
        "quantity": 50000,
        "transaction_id": 6894785035,
        "type_id": 17470,
        "unit_price": 10.33,
    }
]


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, OWNER, "Owner")
    snapshots.ensure_character(conn, ALT, "Alt")
    conn.execute(
        "INSERT INTO sde_npc_corporations (corporation_id, faction_id) VALUES (?, NULL)", (SCC,)
    )
    yield conn
    conn.close()


def test_journal_keeps_amounts_and_drops_people_and_text(db):
    cleaned = snapshots.sanitise(db, "wallet_journal", JOURNAL)
    sale, tax, donation, from_alt = cleaned
    assert sale == {
        "id": 1,
        "date": "2026-10-16T03:33:48Z",
        "ref_type": "market_transaction",
        "amount": 516500.0,
        "balance": 917423340.58,
        "second_party_id": OWNER,
        "context_id": 6894785035,
        "context_id_type": "market_transaction_id",
    }
    assert tax["tax_receiver_id"] == SCC  # NPC corporations are kept
    assert tax["second_party_id"] == SCC
    assert "context_id" not in donation  # it pointed at another player
    assert from_alt["first_party_id"] == ALT  # the owner's own characters are kept
    assert from_alt["context_id"] == ALT
    text = json.dumps(cleaned)
    for leaked in (str(STRANGER), "Some Buyer", "Some Stranger", "player-written", "description"):
        assert leaked not in text


def test_transactions_drop_the_other_party(db):
    (cleaned,) = snapshots.sanitise(db, "wallet_transactions", TRANSACTIONS)
    assert "client_id" not in cleaned
    assert cleaned["unit_price"] == 10.33
    assert cleaned["type_id"] == 17470


def test_other_kinds_and_odd_shapes(db):
    assert snapshots.sanitise(db, "wallet", 12.5) == 12.5
    assert snapshots.sanitise(db, "wallet_journal", {"error": "x"}) == []
    assert (
        snapshots.sanitise(db, "wallet_transactions", ["text", TRANSACTIONS[0]])[0][
            "transaction_id"
        ]
        == 6894785035
    )


@respx.mock
def test_collect_stores_cleaned_payloads_and_caches_nothing_raw(db):
    respx.get(f"{BASE}/characters/{OWNER}/wallet/journal").mock(
        return_value=httpx.Response(200, json=JOURNAL, headers={"etag": '"j"'})
    )
    respx.get(f"{BASE}/characters/{OWNER}/wallet/transactions").mock(
        return_value=httpx.Response(200, json=TRANSACTIONS, headers={"etag": '"t"'})
    )
    respx.get(url__startswith=f"{BASE}/characters/{OWNER}").mock(
        return_value=httpx.Response(200, json={"ok": True}, headers={"etag": '"o"'})
    )
    with httpx.Client() as http:
        esi = EsiClient(http, db, base_url=BASE, token_for=lambda _cid: "token")
        snapshots.collect(db, esi, OWNER)
    stored = json.dumps([json.loads(r[0]) for r in db.execute("SELECT payload FROM snapshots")])
    assert str(STRANGER) not in stored
    assert "Some Buyer" not in stored
    cached = [r[0] for r in db.execute("SELECT cache_key FROM esi_cache")]
    assert cached  # other kinds still use the cache
    assert not [k for k in cached if "wallet/journal" in k or "wallet/transactions" in k]


def _migration_0010() -> str:
    return next(sql for number, _name, sql in store._migrations() if number == 10)


def test_migration_cleans_old_rows_like_sanitise_does(db):
    for kind, payload in (("wallet_journal", JOURNAL), ("wallet_transactions", TRANSACTIONS)):
        db.execute(
            "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
            "VALUES (?, ?, 'x', 'h', ?)",
            (OWNER, kind, json.dumps(payload)),
        )
    db.execute(
        "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
        "VALUES (?, 'wallet', 'x', 'h', '12.5')",
        (OWNER,),
    )
    db.execute(
        "INSERT INTO esi_cache VALUES ('GET /characters/7/wallet/journal/ 7', '\"e\"', 0, '[]')"
    )
    db.execute("INSERT INTO esi_cache VALUES ('GET /characters/7/skills/ 7', '\"e\"', 0, '{}')")
    db.commit()

    db.executescript(f"BEGIN;\n{_migration_0010()}\nCOMMIT;")

    stored = {
        kind: json.loads(payload)
        for kind, payload in db.execute("SELECT kind, payload FROM snapshots")
    }
    assert stored["wallet_journal"] == snapshots.sanitise(db, "wallet_journal", JOURNAL)
    assert stored["wallet_transactions"] == snapshots.sanitise(
        db, "wallet_transactions", TRANSACTIONS
    )
    assert stored["wallet"] == 12.5
    assert [r[0] for r in db.execute("SELECT cache_key FROM esi_cache")] == [
        "GET /characters/7/skills/ 7"
    ]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):  # the trigger is back
        db.execute("UPDATE snapshots SET payload = '[]'")
