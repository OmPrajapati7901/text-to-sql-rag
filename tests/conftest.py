from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.authorization.scope import establish_scope
from app.contracts.semantic_plan import Operation, SemanticRequest, TimeRange
from app.services import Services
from app.sql.gateway import DuckDbGateway
from fixtures.build import build_all, db_path

ROOT = Path(__file__).resolve().parent.parent
Q2_2026 = TimeRange(
    start=datetime(2026, 4, 1, tzinfo=UTC), end_exclusive=datetime(2026, 7, 1, tzinfo=UTC)
)


@pytest.fixture(scope="session", autouse=True)
def _fixtures_built():
    if not db_path("golden").exists():
        build_all()


@pytest.fixture(scope="session")
def services() -> Services:
    return Services.build()


@pytest.fixture
def scope(services):
    return establish_scope(services.policy, "analyst_full", "local-001")


@pytest.fixture
def restricted_scope(services):
    return establish_scope(services.policy, "analyst_restricted", "local-001")


@pytest.fixture
def other_tenant_scope(services):
    return establish_scope(services.policy, "analyst_other_tenant", "local-001")


@pytest.fixture
def revenue_by_region() -> SemanticRequest:
    """The §40A question: monthly revenue by customer region in Q2 2026."""
    return SemanticRequest(
        operation=Operation.TREND,
        metric_ids=("finance.revenue",),
        dimension_ids=("customer.region_at_order",),
        time_range=Q2_2026,
        time_grain="month",
    )


@pytest.fixture
def pipeline(services):
    from app.pipeline import Pipeline

    def _make(scenario: str = "golden"):
        return Pipeline(services, DuckDbGateway(db_path(scenario)))

    return _make
