import sqlite3

import pytest

from bidpilot.db import BidRow, Database


@pytest.mark.asyncio
async def test_old_database_without_user_chat_id_is_migrated_and_usable(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY, source VARCHAR(64), external_id VARCHAR(128),
            title TEXT, url TEXT, description TEXT, budget_min INTEGER, budget_max INTEGER, budget_text TEXT,
            skills TEXT, category VARCHAR(128), employer VARCHAR(255), published_at DATETIME,
            fetched_at DATETIME, raw_json TEXT);
        CREATE TABLE bids (id INTEGER PRIMARY KEY, project_id INTEGER, source VARCHAR(64), status VARCHAR(32),
            amount INTEGER, delivery_days INTEGER, proposal_text TEXT, proposal_generated_by VARCHAR(128),
            pricing_rationale TEXT, message TEXT, external_bid_id VARCHAR(128), http_status INTEGER,
            attempts INTEGER, latency_ms FLOAT, created_at DATETIME, updated_at DATETIME);
        INSERT INTO projects (id, source, external_id, title) VALUES (1, 'parscoders', '1', '');
        INSERT INTO bids (project_id, source, status, attempts) VALUES (1, 'parscoders', 'submitted', 0);
        """
    )
    con.commit()
    con.close()

    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.init()
    await db.init()  # idempotent
    async with db.session() as session:
        session.add(BidRow(project_id=1, source="parscoders", status="submitted", user_chat_id="42"))
        await session.commit()
    await db.dispose()

    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(bids)")}
    indexes = {row[1] for row in con.execute("PRAGMA index_list(bids)")}
    kept = con.execute("SELECT COUNT(*) FROM bids").fetchone()[0]
    con.close()
    assert "user_chat_id" in columns
    assert "ix_bid_user" in indexes
    assert kept >= 1
