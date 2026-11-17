"""SQLite journal: the durable record of every desk event and closed trade.

Standard-library sqlite3 only. All statements are parameterised. The default
in-memory database suits tests and one-off backtests; pass a file path to keep
the journal between runs.
"""

import json
import sqlite3
from pathlib import Path

from trading_desk.models import ClosedTrade, DeskEvent, DeskReport

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
CREATE TABLE IF NOT EXISTS trade_decisions (
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    report_json TEXT NOT NULL,
    PRIMARY KEY (run_id, symbol, decided_at)
);
CREATE TABLE IF NOT EXISTS closed_trades (
    run_id TEXT NOT NULL,
    trade_number INTEGER NOT NULL,
    trade_json TEXT NOT NULL,
    PRIMARY KEY (run_id, trade_number)
);
"""


class Journal:
    """SQLite record of everything the desk did: every event with its reason, and every closed
    trade.
    In-memory by default; give it a path to keep it.
    """

    def __init__(self, database_path: Path | str = ":memory:"):
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(database_path))
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def record_event(self, event: DeskEvent) -> None:
        """Append one desk event to the journal."""
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

    def record_decision(self, run_id: str, desk_report: DeskReport) -> None:
        """Keep the full six-agent report behind a TRADE decision.

        This is the "why" of a trade: the indicators at the time, each agent's view,
        every risk check and the plan. The bars themselves are left out; they can be
        re-read from the data source.
        """
        self.connection.execute(
            "INSERT OR REPLACE INTO trade_decisions VALUES (?, ?, ?, ?)",
            (
                run_id,
                desk_report.symbol,
                desk_report.evaluated_as_of.isoformat(),
                desk_report.model_dump_json(exclude={"data"}),
            ),
        )
        self.connection.commit()

    def decisions(self, run_id: str) -> list[dict]:
        """The saved reports behind every TRADE decision of a run, oldest first."""
        rows = self.connection.execute(
            "SELECT report_json FROM trade_decisions WHERE run_id = ? ORDER BY decided_at, symbol",
            (run_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def run_ids(self) -> list[str]:
        """Every run that has recorded at least one event, oldest first."""
        rows = self.connection.execute(
            "SELECT run_id FROM desk_events GROUP BY run_id ORDER BY MIN(timestamp), run_id"
        ).fetchall()
        return [row[0] for row in rows]

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
