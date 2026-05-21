import sys
import os
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "test.db"))
    s.init()
    yield s
    s.close()


def test_add_and_lookup_reply_both_directions(storage):
    storage.add_reply(100, 1)
    storage.add_reply(200, 2)
    assert storage.tg_for_mesh(100) == 1
    assert storage.mesh_for_tg(1) == 100
    assert storage.tg_for_mesh(200) == 2
    assert storage.mesh_for_tg(2) == 200


def test_replace_reply_updates_both_directions(storage):
    storage.add_reply(100, 1)
    storage.add_reply(100, 2)
    assert storage.tg_for_mesh(100) == 2
    assert storage.mesh_for_tg(1) is None
    assert storage.mesh_for_tg(2) == 100


def test_reply_prune_keeps_newest_10000(tmp_path):
    """Insert rows with controlled created_at to verify prune logic."""
    s = Storage(str(tmp_path / "test.db"))
    s.init()

    # Insert 10 rows with ascending created_at directly, bypassing add_reply time
    conn = s._conn
    for i in range(10):
        conn.execute(
            "INSERT OR REPLACE INTO reply_mapping (mesh_id, tg_id, created_at) VALUES (?, ?, ?)",
            (i, i, i),  # created_at = i (0..9)
        )
    conn.commit()

    # Temporarily patch limit to 10 to simulate pruning
    import storage as storage_mod
    original_limit = storage_mod._PRUNE_LIMIT
    storage_mod._PRUNE_LIMIT = 10

    # add_reply reads _PRUNE_LIMIT at call time, so this works
    s.add_reply(99, 99)  # created_at = now >> 9, will be newest

    storage_mod._PRUNE_LIMIT = original_limit

    # Should have exactly 10 rows; row with mesh_id=0 (oldest) should be gone
    count = conn.execute("SELECT COUNT(*) FROM reply_mapping").fetchone()[0]
    assert count == 10
    oldest = conn.execute(
        "SELECT mesh_id FROM reply_mapping ORDER BY created_at ASC LIMIT 1"
    ).fetchone()[0]
    assert oldest != 0  # mesh_id=0 was oldest and pruned
    assert s.tg_for_mesh(99) == 99

    s.close()


def test_returns_none_for_unknown_reply(storage):
    assert storage.mesh_for_tg(9999) is None
    assert storage.tg_for_mesh(9999) is None


def test_upsert_node_and_lookup(storage):
    storage.upsert_node("!abc", "Foo Bar", "FB")
    assert storage.lookup_node("!abc") == ("Foo Bar", "FB")


def test_lookup_node_returns_none_when_unknown(storage):
    assert storage.lookup_node("!xyz") is None


def test_lookup_node_returns_none_when_partial(storage):
    storage.upsert_node("!def", None, "XX")
    assert storage.lookup_node("!def") is None


def test_upsert_and_recent_telemetry(storage):
    now = int(time.time())
    storage.upsert_telemetry("!node1", 22.5, 60.0, 1013.0, 50000.0)
    storage.upsert_telemetry("!node2", 18.0, 75.0, 1010.0, None)
    rows = storage.recent_telemetry(now - 10)
    node_ids = {r[0] for r in rows}
    assert "!node1" in node_ids
    assert "!node2" in node_ids
    assert len(rows) == 2


def test_recent_telemetry_filters_by_since(storage):
    now = int(time.time())
    # Insert old record directly with a past timestamp
    storage._conn.execute(
        "INSERT OR REPLACE INTO telemetry "
        "(node_id, temperature, humidity, pressure, gas_resistance, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("!old_node", 10.0, 50.0, 1000.0, 30000.0, now - 7200),
    )
    storage._conn.commit()
    storage.upsert_telemetry("!new_node", 25.0, 65.0, 1015.0, 60000.0)
    rows = storage.recent_telemetry(now - 3600)
    node_ids = {r[0] for r in rows}
    assert "!old_node" not in node_ids
    assert "!new_node" in node_ids


def test_upsert_telemetry_skipped_when_all_none(storage):
    now = int(time.time())
    storage.upsert_telemetry("!null_node", None, None, None, None)
    rows = storage.recent_telemetry(now - 10)
    assert all(r[0] != "!null_node" for r in rows)


def test_upsert_and_recent_position(storage):
    now = int(time.time())
    storage.upsert_position("!node1", 55.75, 37.62, 150.0)
    storage.upsert_position("!node2", 48.85, 2.35, None)
    rows = storage.recent_positions(now - 10)
    node_ids = {r[0] for r in rows}
    assert "!node1" in node_ids
    assert "!node2" in node_ids
    assert len(rows) == 2
    # Verify tuple structure: (node_id, lat, lon, alt, updated_at)
    row1 = next(r for r in rows if r[0] == "!node1")
    assert row1[1] == 55.75
    assert row1[2] == 37.62
    assert row1[3] == 150.0
    assert row1[4] >= now


def test_recent_positions_filters_by_since(storage):
    now = int(time.time())
    storage._conn.execute(
        "INSERT OR REPLACE INTO positions (node_id, latitude, longitude, altitude, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("!old_node", 10.0, 20.0, None, now - 90000),
    )
    storage._conn.commit()
    storage.upsert_position("!new_node", 55.75, 37.62, None)
    rows = storage.recent_positions(now - 86400)
    node_ids = {r[0] for r in rows}
    assert "!old_node" not in node_ids
    assert "!new_node" in node_ids


def test_upsert_position_skipped_when_lat_or_lon_none(storage):
    now = int(time.time())
    storage.upsert_position("!no_lat", None, 37.62, None)
    storage.upsert_position("!no_lon", 55.75, None, None)
    rows = storage.recent_positions(now - 10)
    node_ids = {r[0] for r in rows}
    assert "!no_lat" not in node_ids
    assert "!no_lon" not in node_ids


def test_record_and_lookup_dm_origin(storage):
    storage.record_dm_origin(42, "!aabbccdd")
    assert storage.dm_origin(42) == "!aabbccdd"
    assert storage.dm_origin(99) is None


def test_find_node_ids_by_short_name_case_insensitive(storage):
    storage.upsert_node("!aaaa0001", "Alpha One", "SLVR")
    storage.upsert_node("!aaaa0002", "Alpha Two", "slvr")
    storage.upsert_node("!bbbb0003", "Beta Three", "BETA")
    result = storage.find_node_ids_by_short_name("slvr")
    assert set(result) == {"!aaaa0001", "!aaaa0002"}


def test_find_node_ids_by_short_name_empty(storage):
    result = storage.find_node_ids_by_short_name("XXXX")
    assert result == []
