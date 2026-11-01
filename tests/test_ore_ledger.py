"""Shared ore ledgers (design 4.11): who mined what, and the split at the end."""

import re

import pytest
from fastapi.testclient import TestClient

from eve_assistant import ledgers, numbers, snapshots, store
from eve_assistant.web import create_app

VELDSPAR, SCORDITE, PYROXERES, COMPRESSED, VENTURE = 1230, 1228, 1224, 62516, 32880
LOCAL = "http://127.0.0.1"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    conn = store.connect(path)
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(462, 25, "Veldspar"), (460, 25, "Scordite"), (459, 25, "Pyroxeres"), (2, 6, "Ships")],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) VALUES (?, ?, ?, 1, ?)",
        [
            (VELDSPAR, "Veldspar", 462, 0.1),
            (SCORDITE, "Scordite", 460, 0.15),
            (PYROXERES, "Pyroxeres", 459, 0.3),
            (COMPRESSED, "Compressed Veldspar", 462, 0.001),
            (VENTURE, "Venture", 2, 2500.0),
        ],
    )
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'now')",
        [(VELDSPAR, 20.0), (SCORDITE, 30.0), (COMPRESSED, 20.0), (VENTURE, 1_000_000.0)],
    )  # Pyroxeres has no price yet
    conn.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at) VALUES (1, 'x', 'y')"
    )
    snapshots.ensure_character(conn, 7, "Alex Owner")
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    conn = store.connect(db_path)
    yield conn
    conn.close()


def _by_person(split):
    return {s.person: s for s in split.shares}


def test_the_profit_follows_the_value_of_each_persons_ore(db):
    ledger = ledgers.start(db, "  Friday   mining ")
    ledgers.add_entry(db, ledger, "Alice", "veldspar", 10_000)  # 200k at CCP's average
    ledgers.add_entry(db, ledger, "Bob", "Scordite", 5_000)  # 150k
    ledgers.add_entry(db, ledger, "Bob", "Scordite", 5_000)  # another session: 300k in all

    before = ledgers.split(db, ledger)
    assert (before.estimated, before.by, before.total) == (True, "value", 500_000)
    assert _by_person(before)["Alice"].weight == pytest.approx(0.4)

    ledgers.close(db, ledger, 1_000_000)
    after = ledgers.split(db, ledger)
    shares = _by_person(after)
    assert not after.estimated
    assert shares["Alice"].total == pytest.approx(400_000)
    assert shares["Bob"].total == pytest.approx(600_000)
    assert shares["Bob"].m3 == pytest.approx(1_500)
    assert ledgers.get(db, ledger).name == "Friday mining"
    assert ledgers.summary_text(ledgers.get(db, ledger), after).splitlines() == [
        "Friday mining: sold for 1,000,000 ISK",
        "Bob: 600,000 ISK (ore 60.0%)",
        "Alice: 400,000 ISK (ore 40.0%)",
    ]


def test_role_shares_come_off_the_top_split_between_the_people_in_them(db):
    ledger = ledgers.start(db, "Belt run")
    ledgers.add_entry(db, ledger, "Alice", "Veldspar", 10_000)
    ledgers.add_entry(db, ledger, "Bob", "Veldspar", 30_000)
    ledgers.add_role(db, ledger, "Alex Owner", "Booster")
    ledgers.add_role(db, ledger, "Carol", "escort")  # no ore: only the escort share
    ledgers.add_role(db, ledger, "Dave", "escort")
    ledgers.add_role(db, ledger, "Bob", "hauler")  # a role with no share is only a note
    ledgers.set_share(db, ledger, "booster", 10)
    ledgers.set_share(db, ledger, "Escort", 10)
    ledgers.close(db, ledger, 1_000_000)

    result = ledgers.split(db, ledger)
    shares = _by_person(result)
    assert result.ore_pool == pytest.approx(800_000)
    assert shares["Alex Owner"].total == pytest.approx(100_000)
    assert shares["Carol"].total == shares["Dave"].total == pytest.approx(50_000)
    assert shares["Alice"].total == pytest.approx(200_000)
    assert shares["Bob"].total == pytest.approx(600_000)
    assert shares["Bob"].roles == ("hauler",)
    assert sum(s.total for s in result.shares) == pytest.approx(1_000_000)
    assert ledgers.role_table(db, ledger) == [
        ("booster", 10, ["Alex Owner"]),
        ("escort", 10, ["Carol", "Dave"]),
        ("hauler", None, ["Bob"]),
    ]
    assert "Carol: 50,000 ISK (escort share)" in ledgers.summary_text(
        ledgers.get(db, ledger), result
    )


def test_role_shares_cant_pass_100_percent_and_an_unheld_share_goes_with_the_ore(db):
    ledger = ledgers.start(db, "Belt run")
    ledgers.add_entry(db, ledger, "Alice", "Veldspar", 10_000)
    ledgers.set_share(db, ledger, "booster", 60)
    with pytest.raises(ledgers.LedgerError, match="already take 60%"):
        ledgers.set_share(db, ledger, "hauler", 50)
    ledgers.set_share(db, ledger, "booster", 90)  # changing a share replaces it
    with pytest.raises(ledgers.LedgerError):
        ledgers.set_share(db, ledger, "hauler", 0)

    result = ledgers.split(db, ledger)  # nobody is a booster yet
    assert [r.code for r in result.reasons] == ["ledger_share_unheld"]
    assert _by_person(result)["Alice"].total == pytest.approx(result.total)
    ledgers.set_share(db, ledger, "booster", None)
    assert ledgers.shares(db, ledger) == {}


def test_an_ore_without_a_price_splits_everything_by_volume(db):
    ledger = ledgers.start(db, "Belt run")
    ledgers.add_entry(db, ledger, "Alice", "Veldspar", 10_000)  # 1,000 m³
    ledgers.add_entry(db, ledger, "Bob", "Pyroxeres", 10_000)  # 3,000 m³, no price
    ledgers.close(db, ledger, 400)

    result = ledgers.split(db, ledger)
    assert result.by == "volume"
    assert result.reasons[0].code == "ledger_unpriced"
    assert result.reasons[0].detail == {"ores": ["Pyroxeres"]}
    assert _by_person(result)["Bob"].total == pytest.approx(300)


def test_names_match_whatever_the_capitals_and_only_ore_is_taken(db):
    ledger = ledgers.start(db, "Belt run")
    ledgers.add_entry(db, ledger, "Bob", "Veldspar", 100)
    ledgers.add_entry(db, ledger, "  bob ", "Compressed Veldspar", 100)
    assert ledgers.people(db, ledger) == ["Bob"]
    other = ledgers.start(db, "Another run")
    ledgers.add_entry(db, other, "BOB", "Veldspar", 1)  # spelled as in the first ledger
    ledgers.add_role(db, other, "alex owner", "booster")  # and as the owner's character
    assert ledgers.people(db, other) == ["Bob", "Alex Owner"]
    with pytest.raises(ledgers.LedgerError, match="Venture isn't ore"):
        ledgers.add_entry(db, ledger, "Bob", "Venture", 1)
    with pytest.raises(ledgers.LedgerError, match="No item called"):
        ledgers.add_entry(db, ledger, "Bob", "Veldsparr", 1)
    with pytest.raises(ledgers.LedgerError, match="who it was"):
        ledgers.add_entry(db, ledger, "  ", "Veldspar", 1)
    with pytest.raises(ledgers.LedgerError, match="a name for the ledger"):
        ledgers.start(db, "")


@pytest.mark.parametrize(
    ("text", "units"), [("12,000", 12_000), ("12 000", 12_000), ("7", 7)]
)  # fmt: skip
def test_units_are_read_as_people_type_them(text, units):
    assert ledgers.parse_quantity(text) == units


@pytest.mark.parametrize("text", ["", "0", "-5", "1.5", "lots"])
def test_units_must_be_a_whole_number_above_nothing(text):
    with pytest.raises(ledgers.LedgerError):
        ledgers.parse_quantity(text)


@pytest.mark.parametrize(
    ("text", "isk"),
    [("123,456,789", 123_456_789), ("123.5M", 123_500_000), ("1.2b", 1.2e9), ("500k ISK", 500_000)],
)
def test_isk_is_read_as_people_type_it(text, isk):
    assert numbers.parse_isk(text) == pytest.approx(isk)


@pytest.mark.parametrize("text", ["", "a lot", "nan", "inf"])
def test_isk_that_isnt_a_number_is_refused(text):
    with pytest.raises(ValueError):
        numbers.parse_isk(text)


def test_a_sold_ledger_is_kept_as_it_was_until_reopened(db):
    ledger = ledgers.start(db, "Belt run")
    with pytest.raises(ledgers.LedgerError, match="who mined what"):
        ledgers.close(db, ledger, 1_000)
    entry = ledgers.add_entry(db, ledger, "Alice", "Veldspar", 100)
    with pytest.raises(ledgers.LedgerError, match="once the ore is sold"):
        ledgers.set_paid(db, ledger, "Alice", True)
    ledgers.close(db, ledger, 1_000)
    for change in (
        lambda: ledgers.add_entry(db, ledger, "Bob", "Veldspar", 1),
        lambda: ledgers.remove_entry(db, ledger, entry),
        lambda: ledgers.add_role(db, ledger, "Bob", "escort"),
        lambda: ledgers.set_share(db, ledger, "escort", 5),
        lambda: ledgers.close(db, ledger, 2_000),
    ):
        with pytest.raises(ledgers.LedgerError, match="closed"):
            change()

    ledgers.reopen(db, ledger)
    assert ledgers.get(db, ledger).is_open
    assert ledgers.split(db, ledger).estimated
    ledgers.remove_entry(db, ledger, entry)
    assert ledgers.entries(db, ledger) == []


def test_payments_are_ticked_off_and_the_owners_own_share_needs_none(db):
    ledger = ledgers.start(db, "Belt run")
    ledgers.add_entry(db, ledger, "Alice", "Veldspar", 100)
    ledgers.add_entry(db, ledger, "Alex Owner", "Veldspar", 100)
    ledgers.add_role(db, ledger, "Bob", "scout")  # a role without a share: nothing to pay
    ledgers.close(db, ledger, 1_000)
    own = ledgers.own_names(db)

    assert [s.person for s in ledgers.to_pay(ledgers.split(db, ledger), own)] == ["Alice"]
    ledgers.set_paid(db, ledger, "alice", True)
    assert ledgers.to_pay(ledgers.split(db, ledger), own) == []
    ledgers.set_paid(db, ledger, "Alice", False)
    assert len(ledgers.to_pay(ledgers.split(db, ledger), own)) == 1
    with pytest.raises(ledgers.LedgerError, match="isn't in this ledger"):
        ledgers.set_paid(db, ledger, "Zed", True)


def test_a_name_can_be_removed_everywhere_and_the_splits_still_add_up(db):
    first, second = ledgers.start(db, "One"), ledgers.start(db, "Two")
    ledgers.add_entry(db, first, "Alice", "Veldspar", 100)
    ledgers.add_entry(db, first, "Bob", "Veldspar", 100)
    ledgers.add_role(db, second, "alice", "escort")
    ledgers.close(db, first, 1_000)
    ledgers.set_paid(db, first, "Alice", True)

    assert ledgers.forget(db, "ALICE") == "Removed person 1"
    assert "Alice" not in ledgers.named(db) + ledgers.known_people(db)
    assert ledgers.people(db, first) == ["Removed person 1", "Bob"]
    assert ledgers.people(db, second) == ["Removed person 1"]
    assert _by_person(ledgers.split(db, first))["Removed person 1"].paid_at is not None
    assert ledgers.forget(db, "Bob") == "Removed person 2"
    with pytest.raises(ledgers.LedgerError, match="No one by that name"):
        ledgers.forget(db, "Removed person 1")


# The page


@pytest.fixture
def client(db_path):
    with TestClient(create_app(db_path), base_url=LOCAL) as test_client:
        yield test_client


def _token(html: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def _post(client, path, **data):
    token = _token(client.get("/ledgers").text)
    return client.post(path, data={"token": token, **data}, follow_redirects=False)


def test_a_ledger_from_start_to_split_on_the_page(client):
    started = _post(client, "/ledgers", name="Friday mining")
    assert started.status_code == 303
    page_url = started.headers["location"].split("?")[0]
    assert re.fullmatch(r"/ledgers/\d+", page_url)

    for person, ore, units in (("Alice", "Veldspar", "10,000"), ("Bob", "Scordite", "10,000")):
        added = _post(client, f"{page_url}/entries", person=person, ore=ore, quantity=units)
        assert added.status_code == 303
        assert person not in added.headers["location"]  # names never go in addresses
    _post(client, f"{page_url}/roles", person="Alex Owner", role="booster")
    _post(client, f"{page_url}/shares", role="booster", percent="10%")

    page = client.get(page_url).text
    assert "Friday mining" in page
    assert "Would get" in page
    assert 'aria-current="page">Ore ledger' in page
    assert "Alice" in page
    assert "Scordite" in page

    sold = _post(client, f"{page_url}/close", sold_for="1.5M")
    assert sold.headers["location"] == f"{page_url}?msg=ledger_closed#split"
    page = client.get(page_url).text
    assert "Friday mining: sold for 1,500,000 ISK" in page
    assert "Mark paid" in page
    assert "yours" in page  # the owner's own character
    assert "2 to pay" in client.get("/ledgers").text


def test_mistakes_come_back_as_messages_on_the_page(client):
    page_url = _post(client, "/ledgers", name="Belt run").headers["location"].split("?")[0]
    page = _post(client, f"{page_url}/entries", person="Alice", ore="Venture", quantity="5")
    assert page.status_code == 200
    assert "Venture isn&#39;t ore" in page.text
    page = _post(client, f"{page_url}/entries", person="Alice", ore="Veldspar", quantity="lots")
    assert "Enter how many units" in page.text
    page = _post(client, f"{page_url}/close", sold_for="a lot")
    assert "Enter what the ore sold for" in page.text
    assert client.get("/ledgers/999").status_code == 404


def test_ledger_forms_need_the_form_token(client):
    response = client.post("/ledgers", data={"token": "wrong", "name": "x"})
    assert response.status_code == 403


def test_removing_a_name_asks_first(client):
    page_url = _post(client, "/ledgers", name="Belt run").headers["location"].split("?")[0]
    _post(client, f"{page_url}/entries", person="Alice", ore="Veldspar", quantity="5")
    _post(client, f"{page_url}/entries", person="Alex Owner", ore="Veldspar", quantity="5")
    confirm = _post(client, "/ledgers/forget/confirm", person="Alice")
    assert "Remove this name?" in confirm.text
    assert "<option>Alex Owner</option>" not in client.get("/ledgers").text  # your own
    done = _post(client, "/ledgers/forget", person="Alice")
    assert done.headers["location"] == "/ledgers?msg=ledger_forgotten"
    assert "Alice" not in client.get(page_url).text
    assert "Removed person 1" in client.get(page_url).text


def test_ledger_pages_have_no_inline_styles_or_scripts(client):
    page_url = _post(client, "/ledgers", name="Belt run").headers["location"].split("?")[0]
    _post(client, f"{page_url}/entries", person="Alice", ore="Pyroxeres", quantity="5")
    _post(client, f"{page_url}/roles", person="Bob", role="escort")
    for path in ("/ledgers", page_url):
        page = client.get(path).text
        assert "<script" not in page, path
        assert "style=" not in page, path
        assert "<style" not in page, path
        ids = re.findall(r'\sid="([^"]+)"', page)
        assert len(ids) == len(set(ids)), path  # each id once: labels and lists rely on it
