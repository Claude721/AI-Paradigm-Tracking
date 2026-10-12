"""Durable conservative credit reservations, independent of research windows.

Never retain credentials, queries or response bodies. A request is reserved
before I/O; a crash/unknown charge cannot silently refund that reservation.
"""

from datetime import datetime, timezone


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS provider_credit_periods (
            provider TEXT NOT NULL, period TEXT NOT NULL,
            baseline_credits INTEGER NOT NULL, observed_credits INTEGER NOT NULL,
            observed_through INTEGER NOT NULL,
            PRIMARY KEY(provider, period)
        );
        CREATE TABLE IF NOT EXISTS provider_credit_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL, period TEXT NOT NULL,
            credits INTEGER NOT NULL, status TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_provider_credit_requests
            ON provider_credit_requests(provider, period);
    """)


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid provider credit count")
    return value


def _identity(provider, period):
    if provider != "tavily" or not isinstance(period, str):
        raise ValueError("Invalid provider credit identity")
    parsed = datetime.strptime(period, "%Y-%m")
    if parsed.strftime("%Y-%m") != period:
        raise ValueError("Invalid provider credit period")


def validate(conn):
    for provider, period, baseline, observed, through in conn.execute("SELECT provider, period, baseline_credits, observed_credits, observed_through FROM provider_credit_periods"):
        _identity(provider, period)
        for value in (baseline, observed, through):
            _integer(value)
        if observed < baseline:
            raise ValueError("Provider credit observation regressed")
        _validate_watermark(conn, provider, period, through)
    for identity, provider, period, credits, status in conn.execute("SELECT request_id, provider, period, credits, status FROM provider_credit_requests"):
        _integer(identity)
        _identity(provider, period)
        _integer(credits)
        if status not in {"reserved", "confirmed", "unknown"} or (status != "confirmed" and credits < 1):
            raise ValueError("Invalid provider credit reservation")
        if not conn.execute("SELECT 1 FROM provider_credit_periods WHERE provider=? AND period=?", (provider, period)).fetchone():
            raise ValueError("Provider credit reservation has no period")


def _validate_watermark(conn, provider, period, through):
    if through and not conn.execute(
        "SELECT 1 FROM provider_credit_requests WHERE request_id=? AND provider=? AND period=?",
        (through, provider, period),
    ).fetchone():
        raise ValueError("Provider observation has an invalid reservation watermark")


class ProviderBudgetStore:
    def __init__(self, store):
        self.store = store

    def watermark(self, provider, period):
        _identity(provider, period)
        with self.store._connect() as conn:
            return conn.execute("SELECT COALESCE(MAX(request_id), 0) FROM provider_credit_requests WHERE provider=? AND period=?", (provider, period)).fetchone()[0]

    def observe(self, provider, period, credits, *, through):
        _identity(provider, period)
        _integer(credits)
        _integer(through)
        with self.store.transaction(), self.store._connect() as conn:
            _validate_watermark(conn, provider, period, through)
            old = conn.execute("SELECT observed_credits, observed_through FROM provider_credit_periods WHERE provider=? AND period=?", (provider, period)).fetchone()
            if old is None:
                conn.execute("INSERT INTO provider_credit_periods VALUES (?, ?, ?, ?, ?)", (provider, period, credits, credits, through))
            elif credits >= old[0]:
                # Concurrent observations must not consume a newer watermark
                # with an older account value (safe overcount, never undercount).
                watermark = min(old[1], through) if credits == old[0] else through
                conn.execute("UPDATE provider_credit_periods SET observed_credits=?, observed_through=? WHERE provider=? AND period=?", (credits, watermark, provider, period))

    @staticmethod
    def _used(conn, provider, period):
        row = conn.execute("SELECT baseline_credits, observed_credits, observed_through FROM provider_credit_periods WHERE provider=? AND period=?", (provider, period)).fetchone()
        if row is None:
            raise ValueError("Provider usage has not been verified")
        total, since = conn.execute("SELECT COALESCE(SUM(credits), 0), COALESCE(SUM(CASE WHEN request_id>? THEN credits ELSE 0 END), 0) FROM provider_credit_requests WHERE provider=? AND period=?", (row[2], provider, period)).fetchone()
        return max(row[0] + total, row[1] + since)

    def reserve(self, provider, period, *, limit, credits=1):
        _identity(provider, period)
        if _integer(limit) < 1 or _integer(credits) < 1:
            raise ValueError("A positive provider budget is required")
        with self.store.transaction(), self.store._connect() as conn:
            if self._used(conn, provider, period) + credits > limit:
                return None
            cursor = conn.execute("INSERT INTO provider_credit_requests(provider, period, credits, status) VALUES (?, ?, ?, 'reserved')", (provider, period, credits))
            return cursor.lastrowid

    def settle(self, request_id, *, credits=None):
        _integer(request_id)
        if credits is not None:
            _integer(credits)
        with self.store.transaction(), self.store._connect() as conn:
            row = conn.execute("SELECT credits, status FROM provider_credit_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown provider reservation")
            if row[1] == "confirmed":
                if credits != row[0]:
                    raise ValueError("Conflicting provider credit confirmation")
                return
            conn.execute("UPDATE provider_credit_requests SET credits=?, status=? WHERE request_id=?", (row[0] if credits is None else credits, "unknown" if credits is None else "confirmed", request_id))

    def used(self, provider, period):
        _identity(provider, period)
        with self.store._connect() as conn:
            return self._used(conn, provider, period)


def billing_period():
    # Supplier billing is wall-clock UTC, never the frozen research date.
    return datetime.now(timezone.utc).strftime("%Y-%m")
