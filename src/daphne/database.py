import os
import datetime
from typing import Optional, List, Dict, Any, Union
import aiosqlite

DEFAULT_DB_PATH = "daphne.db"
ENV_DATABASE_URL = "DAPHNE_DATABASE_URL"


def get_db_path() -> str:
    """
    Get the database path:
    1. If DAPHNE_DATABASE_URL is set, use it (strip 'sqlite:///' if present).
    2. Else if 'daphne.db' is present in current working directory, use it.
    3. Else, default to '~/.local/share/daphne/daphne.db' (create directory if missing).
    """
    url = os.environ.get(ENV_DATABASE_URL)
    if url is not None:
        if url.startswith("sqlite:///"):
            return url[len("sqlite:///") :]
        return url

    if os.path.exists("daphne.db"):
        return "daphne.db"

    path = os.path.expanduser("~/.local/share/daphne/daphne.db")
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    return path


class _DatabaseConnectionContext:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            await self.conn.close()


def get_db_connection(db_path: Optional[str] = None) -> _DatabaseConnectionContext:
    """
    Get an asynchronous context manager for an aiosqlite database connection.
    Enables sqlite3.Row for dictionary-like access.
    """
    path = db_path or get_db_path()
    return _DatabaseConnectionContext(path)


async def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize the database by running migration SQL to create tables and indexes.
    """
    path = db_path or get_db_path()
    # Ensure parent directories exist if database path is a file in a subfolder
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    async with aiosqlite.connect(path) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS exchange_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_currency VARCHAR(10) NOT NULL,
            target_currency VARCHAR(10) NOT NULL,
            rate REAL NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            fetched_at DATETIME NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_exchange_rates_currencies ON exchange_rates(source_currency, target_currency);
        CREATE INDEX IF NOT EXISTS idx_exchange_rates_fetched_at ON exchange_rates(fetched_at);
        """)
        await db.commit()


def _parse_datetime(val: Any) -> Optional[Union[datetime.datetime, str]]:
    if not val:
        return None
    if isinstance(val, datetime.datetime):
        return val
    val_str = str(val)
    try:
        # standard ISO 8601 parsing, handle potential timezone indicator or space separators
        # SQLite's CURRENT_TIMESTAMP is like "2026-06-20 07:50:46"
        return datetime.datetime.fromisoformat(val_str.replace(" ", "T"))
    except ValueError:
        return val_str


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    d = dict(row)
    if "created_at" in d:
        d["created_at"] = _parse_datetime(d["created_at"])
    if "fetched_at" in d:
        d["fetched_at"] = _parse_datetime(d["fetched_at"])
    return d


async def save_exchange_rate(
    db_path: Optional[str],
    source: str,
    target: str,
    rate: float,
    fetched_at: Union[datetime.datetime, str],
) -> int:
    """
    Save exchange rate to the database.
    Returns the id of the inserted row.
    """
    path = db_path or get_db_path()

    # Standardize fetched_at format
    fetched_at_str = (
        fetched_at.isoformat()
        if isinstance(fetched_at, datetime.datetime)
        else fetched_at
    )

    async with get_db_connection(path) as db:
        async with db.execute(
            """
            INSERT INTO exchange_rates (source_currency, target_currency, rate, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (source.upper(), target.upper(), rate, fetched_at_str),
        ) as cursor:
            row_id = cursor.lastrowid
            await db.commit()
            return row_id


async def get_latest_exchange_rate(
    db_path: Optional[str], source: str, target: str
) -> Optional[Dict[str, Any]]:
    """
    Get the latest exchange rate for a given source and target currency.
    Returns a dictionary or None.
    """
    path = db_path or get_db_path()
    async with get_db_connection(path) as db:
        async with db.execute(
            """
            SELECT id, source_currency, target_currency, rate, created_at, fetched_at
            FROM exchange_rates
            WHERE source_currency = ? AND target_currency = ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1
            """,
            (source.upper(), target.upper()),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return _row_to_dict(row)
            return None


async def get_exchange_rate_history(
    db_path: Optional[str], count: int
) -> List[Dict[str, Any]]:
    """
    Get overall history of all exchange rates.
    Returns a list of dictionaries up to `count` items.
    """
    path = db_path or get_db_path()
    async with get_db_connection(path) as db:
        async with db.execute(
            """
            SELECT id, source_currency, target_currency, rate, created_at, fetched_at
            FROM exchange_rates
            ORDER BY fetched_at DESC, id DESC
            LIMIT ?
            """,
            (count,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]
