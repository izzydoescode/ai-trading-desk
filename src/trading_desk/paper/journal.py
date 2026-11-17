"""SQLite journal: the durable record of every desk event and closed trade.

Standard-library sqlite3 only. All statements are parameterised. The default
in-memory database suits tests and one-off backtests; pass a file path to keep
the journal between runs.
"""

import json
import sqlite3
from pathlib import Path

from trading_desk.models import ClosedTrade, DeskEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_events (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS closed_trades (
    run_id TEXT NOT NULL,
    trade_number INTEGER NOT NULL,
    trade_json TEXT NOT NULL,
    PRIMARY KEY (run_id, trade_number)
);
"""


class Journal:
    def __init__(self, database_path: Path | str = ":memory:"):
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(database_path))
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def record_event(self, event: DeskEvent) -> None:
        self.connection.execute(
            "INSERT INTO desk_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.run_id,
                event.sequence,
                event.timestamp.isoformat(),
                event.symbol,
                event.agent,
                event.kind,
                event.reason,
                json.dumps(event.detail, sort_keys=True),
            ),
        )
        self.connection.commit()

    def record_trade(self, run_id: str, trade: ClosedTrade) -> None:
        trade_number = self.connection.execute(
            "SELECT COUNT(*) FROM closed_trades WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO closed_trades VALUES (?, ?, ?)",
            (run_id, trade_number, trade.model_dump_json()),
        )
        self.connection.commit()

    def events(self, run_id: str, limit: int | None = None) -> list[DeskEvent]:
        """Events for a run in order; with `limit`, the most recent ones only."""
        rows = self.connection.execute(
            "SELECT run_id, sequence, timestamp, symbol, agent, kind, reason, detail_json "
            "FROM desk_events WHERE run_id = ? ORDER BY sequence DESC LIMIT ?",
            (run_id, -1 if limit is None else limit),
        ).fetchall()
        return [
            DeskEvent(
                run_id=row[0],
                sequence=row[1],
                timestamp=row[2],
                symbol=row[3],
                agent=row[4],
                kind=row[5],
                reason=row[6],
                detail=json.loads(row[7]),
            )
            for row in reversed(rows)
        ]

    def trades(self, run_id: str) -> list[ClosedTrade]:
        rows = self.connection.execute(
            "SELECT trade_json FROM closed_trades WHERE run_id = ? ORDER BY trade_number",
            (run_id,),
        ).fetchall()
        return [ClosedTrade.model_validate_json(row[0]) for row in rows]

    def reason_counts(self, run_id: str, kind: str) -> dict[str, int]:
        """How often each reason occurred for one kind of event, most frequent first."""
        rows = self.connection.execute(
            "SELECT reason, COUNT(*) FROM desk_events WHERE run_id = ? AND kind = ? "
            "GROUP BY reason ORDER BY COUNT(*) DESC, reason",
            (run_id, kind),
        ).fetchall()
        return dict(rows)

    def close(self) -> None:
        self.connection.close()
