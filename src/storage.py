import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_mapping (
    mesh_id    INTEGER PRIMARY KEY,
    tg_id      INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS reply_mapping_tg_id ON reply_mapping(tg_id);

CREATE TABLE IF NOT EXISTS nodes (
    node_id    TEXT PRIMARY KEY,
    long_name  TEXT,
    short_name TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    node_id        TEXT PRIMARY KEY,
    temperature    REAL,
    humidity       REAL,
    pressure       REAL,
    gas_resistance REAL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    node_id    TEXT PRIMARY KEY,
    latitude   REAL NOT NULL,
    longitude  REAL NOT NULL,
    altitude   REAL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dm_origin (
    mesh_id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound (
    mesh_id     INTEGER PRIMARY KEY,
    destination TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
"""

_PRUNE_LIMIT = 10000


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = None
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def add_reply(self, mesh_id: int, tg_id: int) -> None:
        now = int(time.time())
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO reply_mapping (mesh_id, tg_id, created_at) VALUES (?, ?, ?)",
                (mesh_id, tg_id, now),
            )
            self._conn.execute(
                "DELETE FROM reply_mapping WHERE rowid NOT IN "
                "(SELECT rowid FROM reply_mapping ORDER BY created_at DESC LIMIT ?)",
                (_PRUNE_LIMIT,),
            )
            self._conn.commit()
        except Exception:
            logger.error("add_reply failed", exc_info=True)

    def mesh_for_tg(self, tg_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT mesh_id FROM reply_mapping WHERE tg_id = ?", (tg_id,)
        ).fetchone()
        return row[0] if row else None

    def tg_for_mesh(self, mesh_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT tg_id FROM reply_mapping WHERE mesh_id = ?", (mesh_id,)
        ).fetchone()
        return row[0] if row else None

    def record_outbound(self, mesh_id: int, destination: str) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO outbound (mesh_id, destination, created_at) VALUES (?, ?, ?)",
                (mesh_id, destination, int(time.time())),
            )
            self._conn.commit()
        except Exception:
            logger.error("record_outbound failed", exc_info=True)

    def outbound_destination(self, mesh_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT destination FROM outbound WHERE mesh_id = ?", (mesh_id,)
        ).fetchone()
        return row[0] if row else None

    def list_nodes(self, since: int = 0) -> list[tuple[str, str | None, str | None, int]]:
        """Return (node_id, long_name, short_name, updated_at) for known nodes
        with updated_at >= since, ordered by updated_at DESC."""
        rows = self._conn.execute(
            "SELECT node_id, long_name, short_name, updated_at FROM nodes "
            "WHERE updated_at >= ? ORDER BY updated_at DESC",
            (since,),
        ).fetchall()
        return rows

    def upsert_node(self, node_id: str, long_name: str | None, short_name: str | None) -> None:
        now = int(time.time())
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes (node_id, long_name, short_name, updated_at) VALUES (?, ?, ?, ?)",
                (node_id, long_name, short_name, now),
            )
            self._conn.commit()
        except Exception:
            logger.error("upsert_node failed", exc_info=True)

    def upsert_telemetry(
        self,
        node_id: str,
        temperature: float | None,
        humidity: float | None,
        pressure: float | None,
        gas_resistance: float | None,
    ) -> None:
        if temperature is None and humidity is None and pressure is None and gas_resistance is None:
            return
        now = int(time.time())
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO telemetry "
                "(node_id, temperature, humidity, pressure, gas_resistance, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, temperature, humidity, pressure, gas_resistance, now),
            )
            self._conn.commit()
        except Exception:
            logger.error("upsert_telemetry failed", exc_info=True)

    def recent_telemetry(
        self, since: int
    ) -> list[tuple[str, float | None, float | None, float | None, float | None, int]]:
        rows = self._conn.execute(
            "SELECT node_id, temperature, humidity, pressure, gas_resistance, updated_at "
            "FROM telemetry WHERE updated_at >= ? ORDER BY updated_at DESC",
            (since,),
        ).fetchall()
        return rows

    def upsert_position(
        self,
        node_id: str,
        latitude: float | None,
        longitude: float | None,
        altitude: float | None,
    ) -> None:
        if latitude is None or longitude is None:
            return
        now = int(time.time())
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO positions (node_id, latitude, longitude, altitude, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (node_id, latitude, longitude, altitude, now),
            )
            self._conn.commit()
        except Exception:
            logger.error("upsert_position failed", exc_info=True)

    def recent_positions(self, since: int) -> list[tuple[str, float, float, float | None, int]]:
        """Tuples (node_id, latitude, longitude, altitude, updated_at) where updated_at >= since.
        Sort by updated_at DESC."""
        rows = self._conn.execute(
            "SELECT node_id, latitude, longitude, altitude, updated_at "
            "FROM positions WHERE updated_at >= ? ORDER BY updated_at DESC",
            (since,),
        ).fetchall()
        return rows

    def lookup_node(self, node_id: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT long_name, short_name FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return (row[0], row[1])

    def record_dm_origin(self, mesh_id: int, node_id: str) -> None:
        """Store that mesh_id was a DM from node_id addressed to our node."""
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO dm_origin (mesh_id, node_id) VALUES (?, ?)",
                (mesh_id, node_id),
            )
            self._conn.commit()
        except Exception:
            logger.error("record_dm_origin failed", exc_info=True)

    def dm_origin(self, mesh_id: int) -> str | None:
        """Return the sender node_id if mesh_id was a DM to our node, else None."""
        row = self._conn.execute(
            "SELECT node_id FROM dm_origin WHERE mesh_id = ?", (mesh_id,)
        ).fetchone()
        return row[0] if row else None

    def find_node_ids_by_short_name(self, short_name: str) -> list[str]:
        """Return all node_ids whose shortName matches (case-insensitive)."""
        rows = self._conn.execute(
            "SELECT node_id FROM nodes WHERE LOWER(short_name) = LOWER(?)",
            (short_name,),
        ).fetchall()
        return [r[0] for r in rows]
