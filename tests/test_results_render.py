from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.results.render import AnswerEnvelope

pytestmark = pytest.mark.unit


def test_markdown_and_csv_render_normal_answer() -> None:
    answer = AnswerEnvelope(
        status="answered",
        columns=("month", "region", "revenue_usd"),
        rows=((datetime(2026, 4, 1, tzinfo=UTC), "North", Decimal("200.00")),),
        unit="USD",
        metric_versions={"finance.revenue": "v7"},
        applied_rules=("security.tenant_scope@5",),
        join_paths=("commerce.orders_customers_asof@2",),
        time_interpretation="Q2 2026, by month",
        notes=("Verified result.",),
    )

    markdown = answer.to_markdown()
    assert "| 2026-04-01 | North | 200.00 |" in markdown
    assert "Metric: finance.revenue (v7)" in markdown
    assert "Rules applied: security.tenant_scope@5" in markdown
    assert "Join path: commerce.orders_customers_asof@2" in markdown
    assert "Evidence: verified" in markdown
    assert answer.to_csv_bytes().decode() == (
        "month,region,revenue_usd\r\n2026-04-01T00:00:00+00:00,North,200.00\r\n"
    )


def test_markdown_renders_empty_answer_without_csv() -> None:
    answer = AnswerEnvelope(
        status="answered",
        columns=("month", "revenue_usd"),
        rows=(),
        metric_versions={"finance.revenue": "v7"},
    )

    assert "No rows matched" in answer.to_markdown()
    assert answer.to_csv_bytes() == b""


def test_markdown_renders_definition_without_table_or_csv() -> None:
    answer = AnswerEnvelope(
        status="answered",
        notes=("Revenue is captured order amount net of fully refunded orders.",),
    )

    markdown = answer.to_markdown()
    assert "Revenue is captured" in markdown
    assert "| ---" not in markdown
    assert "### Provenance" in markdown
    assert answer.to_csv_bytes() == b""


@pytest.mark.parametrize("status", ["denied", "unsupported"])
def test_markdown_renders_governed_terminal_answers(status: str) -> None:
    answer = AnswerEnvelope(
        status=status,
        evidence_status="unavailable",
        notes=("No governed answer is available.",),
        reason_codes=("NOT_AUTHORIZED",),
    )

    markdown = answer.to_markdown()
    assert f"`{status}`" in markdown
    assert "No governed answer is available." in markdown
    assert "`NOT_AUTHORIZED`" in markdown
    assert "**Evidence:** unavailable" in markdown


def test_markdown_escapes_special_table_characters() -> None:
    answer = AnswerEnvelope(
        status="answered",
        columns=("name|code", "note"),
        rows=((r"north\west|1", "first\nsecond"),),
    )

    markdown = answer.to_markdown()
    assert "name\\|code" in markdown
    assert r"north\\west\|1" in markdown
    assert "first<br>second" in markdown


def test_markdown_limits_display_but_csv_contains_every_row() -> None:
    answer = AnswerEnvelope(
        status="answered",
        columns=("day", "value"),
        rows=tuple((date(2026, 4, day), Decimal(f"{day}.00")) for day in range(1, 4)),
    )

    markdown = answer.to_markdown(max_rows=2)
    assert "Showing 2 of 3 rows" in markdown
    assert "2026-04-03" not in markdown
    assert "2026-04-03,3.00" in answer.to_csv_bytes().decode()


def test_markdown_rejects_non_positive_display_limit() -> None:
    answer = AnswerEnvelope(status="answered")
    with pytest.raises(ValueError, match="at least 1"):
        answer.to_markdown(max_rows=0)
