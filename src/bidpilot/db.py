"""Persistence layer for confirmed submissions and submission rate limits.

Discovered projects are evaluated from the marketplace's current page and are
not stored as a scan cache. Project rows are minimal identity anchors for the
confirmed-submission history; scraped project details are not persisted.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Import subscription/account tables so Base.metadata.create_all creates them.
# They live in subscriptions.models and register themselves via Base subclass.
try:
    from .subscriptions.models import PaymentRow, SubscriptionRecord, UserAccountRow  # noqa: F401
except Exception:
    pass


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_project_source_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    budget_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_text: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    employer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class BidRow(Base):
    __tablename__ = "bids"
    __table_args__ = (
        Index("ix_bid_source_created", "source", "created_at"),
        Index("ix_bid_status", "status"),
        Index("ix_bid_user", "user_chat_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # Per-user accounting for multi-tenant rate limiting (nullable for legacy rows)
    user_chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_text: Mapped[str] = mapped_column(Text, default="")
    proposal_generated_by: Mapped[str] = mapped_column(String(128), default="")
    pricing_rationale: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    external_bid_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    project_uid: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _sql_literal(value: object) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def migrate_schema(sync_conn) -> list[str]:
    """Bring an existing database up to the current models, never destroying data.

    ``create_all`` only creates *missing tables*; it does not add columns that
    later releases introduced (e.g. ``bids.user_chat_id``), so an old SQLite
    file made every INSERT/SELECT fail with "no column named ...". This adds
    every missing column (nullable, with the model default when it is a plain
    literal) and every missing index, and returns what it changed.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text

    changes: list[str] = []
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    dialect = sync_conn.dialect
    quote = dialect.identifier_preparer.quote
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present or column.primary_key:
                continue
            ddl = f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(column.name)} {column.type.compile(dialect=dialect)}"
            default = None
            if column.server_default is not None and getattr(column.server_default, "arg", None) is not None:
                arg = column.server_default.arg
                default = str(getattr(arg, "text", arg))
            elif column.default is not None and getattr(column.default, "is_scalar", False):
                default = _sql_literal(column.default.arg)
            if default is not None:
                ddl += f" DEFAULT {default}"
            sync_conn.execute(sa_text(ddl))
            changes.append(f"{table.name}.{column.name}")
        known_indexes = {ix["name"] for ix in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name and index.name not in known_indexes:
                index.create(sync_conn, checkfirst=True)
                changes.append(f"index:{index.name}")
    return changes


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ensure_sqlite_dir(url)
        self.engine = create_async_engine(url, echo=False, future=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @staticmethod
    def _ensure_sqlite_dir(url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if url.startswith(prefix):
            path = Path(url[len(prefix) :])
            if str(path) not in (":memory:", ""):
                path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            try:
                migrated = await conn.run_sync(migrate_schema)
                if migrated:
                    import logging

                    logging.getLogger(__name__).warning(
                        "[DB][MIGRATE] دیتابیس قدیمی به‌روز شد؛ ستون/ایندکس اضافه‌شده: %s", ", ".join(migrated)
                    )
            except Exception:
                import logging

                logging.getLogger(__name__).exception("DB schema migration failed")
                raise
            # ── Lightweight migrations for existing DBs (old installs) ──
            # SQLite has no IF NOT EXISTS for ADD COLUMN constraints in old versions,
            # so we do manual PRAGMA checks.
            from sqlalchemy import text as sa_text

            try:
                # Ensure referral columns exist on user_accounts (added in v0.9.1)
                result = await conn.execute(sa_text("PRAGMA table_info(user_accounts)"))
                cols = {row[1] for row in result.fetchall()}  # row[1] = name
                if "referral_code" not in cols:
                    await conn.execute(
                        sa_text("ALTER TABLE user_accounts ADD COLUMN referral_code VARCHAR(16)")
                    )
                if "referred_by" not in cols:
                    await conn.execute(
                        sa_text("ALTER TABLE user_accounts ADD COLUMN referred_by VARCHAR(32)")
                    )
                if "granted_milestones" not in cols:
                    await conn.execute(
                        sa_text("ALTER TABLE user_accounts ADD COLUMN granted_milestones TEXT DEFAULT '[]'")
                    )
                # Ensure gateway column on payments
                result2 = await conn.execute(sa_text("PRAGMA table_info(payments)"))
                cols2 = {row[1] for row in result2.fetchall()}
                if "gateway" not in cols2:
                    await conn.execute(
                        sa_text("ALTER TABLE payments ADD COLUMN gateway VARCHAR(32) DEFAULT 'zarinpal'")
                    )
                # Create indexes for new columns if missing (SQLite ignores duplicates with IF NOT EXISTS)
                await conn.execute(
                    sa_text(
                        "CREATE INDEX IF NOT EXISTS ix_user_accounts_referral_code ON user_accounts (referral_code)"
                    )
                )
                await conn.execute(
                    sa_text(
                        "CREATE INDEX IF NOT EXISTS ix_user_accounts_referred_by ON user_accounts (referred_by)"
                    )
                )
            except Exception:
                # Migration is best-effort; never break startup
                import logging

                logging.getLogger(__name__).exception("DB auto-migration failed (non-fatal)")

            # Remove legacy scan-only records, dry runs, failed attempts, and
            # per-project skip events. Only confirmed submissions remain.
            await conn.execute(delete(BidRow).where(BidRow.status != "submitted"))
            submitted_project_ids = select(BidRow.project_id).where(BidRow.status == "submitted")
            await conn.execute(delete(ProjectRow).where(~ProjectRow.id.in_(submitted_project_ids)))
            await conn.execute(delete(EventRow).where(EventRow.kind == "bid_skipped"))

            # Keep only the identity fields needed by the bid foreign key/history.
            # Old releases stored full scraped project content, so redact that too.
            await conn.execute(
                update(ProjectRow).values(
                    title="",
                    url="",
                    description="",
                    budget_min=None,
                    budget_max=None,
                    budget_text="",
                    skills="[]",
                    category=None,
                    employer=None,
                    published_at=None,
                    raw_json="{}",
                )
            )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.session_factory()


__all__ = ["Base", "BidRow", "Database", "EventRow", "ProjectRow", "migrate_schema", "select"]
