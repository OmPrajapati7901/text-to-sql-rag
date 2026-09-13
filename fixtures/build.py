"""Build DuckDB warehouse fixtures.

`golden` is the §40A dataset from the architecture document, verbatim, including every row it
says must be excluded. Its expected answer is therefore independently specified rather than
invented here.

Each counterexample is its own database, because a single convenient dataset can make a wrong
query look right. Every scenario must *change* an answer or block if the semantics are wrong.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

DB_DIR = Path(__file__).parent / "db"

DDL = """
SET TimeZone='UTC';
CREATE SCHEMA IF NOT EXISTS commerce;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE SCHEMA IF NOT EXISTS support;
CREATE SCHEMA IF NOT EXISTS marketing;

CREATE TABLE commerce.orders (
    tenant_id        VARCHAR        NOT NULL,
    order_id         BIGINT         NOT NULL,
    customer_id      BIGINT         NOT NULL,
    ordered_at       TIMESTAMPTZ    NOT NULL,
    order_amount_usd DECIMAL(18,2)  NOT NULL,
    order_status     VARCHAR        NOT NULL,
    is_test          BOOLEAN        NOT NULL,
    refund_status    VARCHAR        NOT NULL,
    PRIMARY KEY (tenant_id, order_id)
);

CREATE TABLE commerce.customer_history (
    tenant_id        VARCHAR      NOT NULL,
    customer_id      BIGINT       NOT NULL,
    valid_from       TIMESTAMPTZ  NOT NULL,
    valid_to         TIMESTAMPTZ,
    region_name      VARCHAR      NOT NULL,
    is_internal      BOOLEAN      NOT NULL,
    is_test_customer BOOLEAN      NOT NULL,
    PRIMARY KEY (tenant_id, customer_id, valid_from)
);

CREATE TABLE commerce.order_items (
    tenant_id       VARCHAR       NOT NULL,
    order_id        BIGINT        NOT NULL,
    line_number     INTEGER       NOT NULL,
    product_id      BIGINT        NOT NULL,
    line_amount_usd DECIMAL(18,2) NOT NULL,
    PRIMARY KEY (tenant_id, order_id, line_number)
);
"""

# Distractor tables exist physically so schema-drift checks and retrieval have real targets.
DISTRACTOR_DDL = """
CREATE TABLE commerce.products         (tenant_id VARCHAR, id BIGINT);
CREATE TABLE commerce.product_categories (tenant_id VARCHAR, id BIGINT);
CREATE TABLE commerce.categories       (tenant_id VARCHAR, id BIGINT);
CREATE TABLE commerce.shipments        (tenant_id VARCHAR, id BIGINT);
CREATE TABLE commerce.returns          (tenant_id VARCHAR, id BIGINT);
CREATE TABLE support.tickets           (tenant_id VARCHAR, id BIGINT);
CREATE TABLE finance.invoices          (tenant_id VARCHAR, id BIGINT);
CREATE TABLE finance.payments          (tenant_id VARCHAR, id BIGINT);
CREATE TABLE marketing.campaigns       (tenant_id VARCHAR, id BIGINT);
"""

T = "tenant-demo"
OTHER = "tenant-other"

# --- customer_history rows: (tenant, cust, valid_from, valid_to, region, internal, test) ---
GOLDEN_CUSTOMERS = [
    # C1 moves North -> South on 1 May 2026. Half-open intervals, no overlap, no gap.
    (T, 1, "2020-01-01", "2026-05-01", "North", False, False),
    (T, 1, "2026-05-01", None, "South", False, False),
    (T, 2, "2020-01-01", None, "North", True, False),    # internal -> excluded
    (T, 3, "2020-01-01", None, "North", False, True),    # test customer -> excluded
    (T, 4, "2020-01-01", None, "North", False, False),
    (T, 5, "2020-01-01", None, "North", False, False),
    (OTHER, 1, "2020-01-01", None, "North", False, False),  # other tenant, colliding key
]

# --- orders: (tenant, order, cust, ordered_at, amount, status, is_test, refund) ---
GOLDEN_ORDERS = [
    (T, 101, 1, "2026-04-15 10:00:00", "100.00", "completed", False, "none"),
    (T, 102, 1, "2026-06-15 10:00:00", "50.00", "completed", False, "none"),
    (T, 103, 4, "2026-04-20 10:00:00", "100.00", "completed", False, "none"),
    # --- each of the following must be excluded, for a different reason ---
    (T, 104, 2, "2026-04-10 10:00:00", "999.00", "completed", False, "none"),
    (T, 105, 3, "2026-04-10 11:00:00", "888.00", "completed", False, "none"),
    (T, 106, 5, "2026-04-11 10:00:00", "777.00", "completed", True, "none"),
    (T, 107, 5, "2026-04-12 10:00:00", "666.00", "completed", False, "fully_refunded"),
    (T, 108, 5, "2026-04-13 10:00:00", "555.00", "pending", False, "none"),
    (T, 109, 5, "2026-01-15 10:00:00", "444.00", "completed", False, "none"),  # outside Q2
    # Same order_id as a tenant-demo row, to prove isolation is by tenant not by ID.
    (OTHER, 101, 1, "2026-04-15 10:00:00", "5000.00", "completed", False, "none"),
]


def _insert(con: duckdb.DuckDBPyConnection, customers: list, orders: list) -> None:
    # An empty scenario is a legitimate fixture, so guard executemany's non-empty requirement.
    if customers:
        con.executemany(
            "INSERT INTO commerce.customer_history "
            "VALUES (?,?,?::TIMESTAMPTZ,?::TIMESTAMPTZ,?,?,?)",
            customers,
        )
    if orders:
        con.executemany(
            "INSERT INTO commerce.orders VALUES (?,?,?,?::TIMESTAMPTZ,?::DECIMAL(18,2),?,?,?)",
            orders,
        )


def _new_db(name: str) -> duckdb.DuckDBPyConnection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    path = DB_DIR / f"{name}.duckdb"
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute(DDL)
    con.execute(DISTRACTOR_DDL)
    return con


def build_golden() -> None:
    con = _new_db("golden")
    _insert(con, GOLDEN_CUSTOMERS, GOLDEN_ORDERS)
    con.close()


def build_partial_refund() -> None:
    """Revenue v7 retains the original amount for a partially refunded order. A naive
    'exclude anything refunded' implementation drops this row and under-reports."""
    con = _new_db("partial_refund")
    orders = [
        *GOLDEN_ORDERS,
        (T, 201, 4, "2026-04-25 10:00:00", "200.00", "completed", False, "partially_refunded"),
    ]
    _insert(con, GOLDEN_CUSTOMERS, orders)
    con.close()


def build_scd_overlap() -> None:
    """Two customer versions cover the same instant. An inner join silently doubles the
    order; the uniqueness contract must detect it instead."""
    con = _new_db("scd_overlap")
    customers = [
        (T, 1, "2020-01-01", "2026-06-01", "North", False, False),
        (T, 1, "2026-05-01", None, "South", False, False),  # overlaps May
        (T, 4, "2020-01-01", None, "North", False, False),
    ]
    orders = [
        (T, 101, 1, "2026-05-15 10:00:00", "100.00", "completed", False, "none"),
        (T, 103, 4, "2026-04-20 10:00:00", "100.00", "completed", False, "none"),
    ]
    _insert(con, customers, orders)
    con.close()


def build_scd_gap() -> None:
    """An order falls in a coverage gap. An inner join silently drops it; the unmatched
    policy is quality_failure, so it must block rather than under-report."""
    con = _new_db("scd_gap")
    customers = [
        (T, 1, "2020-01-01", "2026-04-01", "North", False, False),
        (T, 1, "2026-05-01", None, "South", False, False),  # April is uncovered
    ]
    orders = [(T, 101, 1, "2026-04-15 10:00:00", "100.00", "completed", False, "none")]
    _insert(con, customers, orders)
    con.close()


def build_fanout() -> None:
    """One order, three lines. Summing the order header amount across this edge triples it."""
    con = _new_db("fanout")
    customers = [(T, 1, "2020-01-01", None, "North", False, False)]
    orders = [(T, 101, 1, "2026-04-15 10:00:00", "100.00", "completed", False, "none")]
    _insert(con, customers, orders)
    con.executemany(
        "INSERT INTO commerce.order_items VALUES (?,?,?,?,?::DECIMAL(18,2))",
        [(T, 101, 1, 900, "40.00"), (T, 101, 2, 901, "35.00"), (T, 101, 3, 902, "25.00")],
    )
    con.close()


def build_equal_amounts() -> None:
    """Two distinct orders with identical amounts. SUM(DISTINCT amount) would report 100
    instead of 200 -- which is why DISTINCT is never a fan-out repair."""
    con = _new_db("equal_amounts")
    customers = [(T, 1, "2020-01-01", None, "North", False, False)]
    orders = [
        (T, 101, 1, "2026-04-15 10:00:00", "100.00", "completed", False, "none"),
        (T, 102, 1, "2026-04-16 10:00:00", "100.00", "completed", False, "none"),
    ]
    _insert(con, customers, orders)
    con.close()


def build_empty() -> None:
    """Authorized, well-formed, and genuinely no matching rows. Must be reported as a
    legitimate empty result, never repaired by loosening a filter."""
    con = _new_db("empty")
    _insert(con, [(T, 1, "2020-01-01", None, "North", False, False)], [])
    con.close()


def build_dst() -> None:
    """Orders either side of a US DST transition, stored in UTC. Bucketing must follow the
    governed reporting timezone, not the machine's."""
    con = _new_db("dst")
    customers = [(T, 1, "2020-01-01", None, "North", False, False)]
    orders = [
        (T, 101, 1, "2026-03-08 06:30:00", "100.00", "completed", False, "none"),
        (T, 102, 1, "2026-03-08 07:30:00", "50.00", "completed", False, "none"),
        (T, 103, 1, "2026-03-01 05:00:00", "25.00", "completed", False, "none"),
    ]
    _insert(con, customers, orders)
    con.close()


SCENARIOS = {
    "golden": build_golden,
    "partial_refund": build_partial_refund,
    "scd_overlap": build_scd_overlap,
    "scd_gap": build_scd_gap,
    "fanout": build_fanout,
    "equal_amounts": build_equal_amounts,
    "empty": build_empty,
    "dst": build_dst,
}


def build_all() -> list[str]:
    if DB_DIR.exists():
        shutil.rmtree(DB_DIR)
    for build in SCENARIOS.values():
        build()
    return sorted(SCENARIOS)


def db_path(scenario: str = "golden") -> Path:
    return DB_DIR / f"{scenario}.duckdb"


if __name__ == "__main__":
    print("built:", ", ".join(build_all()))
