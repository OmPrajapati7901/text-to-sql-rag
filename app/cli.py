"""Command-line driver."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import typer
from dotenv import load_dotenv

# Load .env before any client reads its configuration, or credentials silently default to
# placeholder values and every service reports itself as down.
load_dotenv()

from app.authorization.scope import establish_scope
from app.services import Services

app = typer.Typer(add_completion=False, help="Governed text-to-SQL")
catalog_app = typer.Typer(help="Catalog snapshot operations")
fixtures_app = typer.Typer(help="Warehouse fixtures")
app.add_typer(catalog_app, name="catalog")
app.add_typer(fixtures_app, name="fixtures")


@catalog_app.command("publish")
def catalog_publish(snapshot: str = "local-001") -> None:
    """Validate and publish an immutable catalog snapshot."""
    from ingestion.publish import publish

    manifest = publish(Path("catalog/snapshots"), snapshot_id=snapshot)
    typer.echo(
        f"published {manifest.snapshot_id}: {manifest.object_count} objects, "
        f"dialect={manifest.dialect}, hash={manifest.manifest_hash[:16]}"
    )


@catalog_app.command("show")
def catalog_show() -> None:
    """Summarize the loaded snapshot."""
    s = Services.build()
    snap = s.catalog.snapshot
    typer.echo(f"snapshot {snap.snapshot_id} (dialect {snap.dialect})")
    for kind in ("domains", "tables", "columns", "relationships", "metrics",
                 "dimensions", "rules", "glossary", "value_sets"):
        typer.echo(f"  {kind:16} {len(snap.all(kind))}")
    typer.echo(f"  certified tables: {[t.name for t in s.catalog.certified_tables()]}")


@fixtures_app.command("seed")
def fixtures_seed() -> None:
    """Build every DuckDB fixture scenario."""
    from fixtures.build import build_all

    typer.echo("built: " + ", ".join(build_all()))


@app.command("plan")
def plan_command(
    metric: str = "finance.revenue",
    dimension: str = "customer.region_at_order",
    start: str = "2026-04-01",
    end: str = "2026-07-01",
    grain: str = "month",
    principal: str = "analyst_full",
    scenario: str = "golden",
    show_sql: bool = True,
) -> None:
    """Bind, compile and execute a hand-authored semantic request."""
    from app.contracts.semantic_plan import Operation, SemanticRequest, TimeRange
    from app.pipeline import Pipeline
    from app.sql.gateway import DuckDbGateway
    from fixtures.build import db_path

    s = Services.build()
    scope = establish_scope(s.policy, principal, s.catalog.snapshot.snapshot_id)
    request = SemanticRequest(
        operation=Operation.TREND if grain else Operation.AGGREGATE,
        metric_ids=(metric,),
        dimension_ids=(dimension,) if dimension else (),
        time_range=TimeRange(
            start=datetime.fromisoformat(start + "T00:00:00+00:00"),
            end_exclusive=datetime.fromisoformat(end + "T00:00:00+00:00"),
        ),
        time_grain=grain or None,
    )
    outcome = Pipeline(s, DuckDbGateway(db_path(scenario))).run(request, scope)
    if show_sql and outcome.compiled:
        typer.echo("--- compiled SQL ---")
        typer.echo(outcome.compiled.sql)
        typer.echo("")
    typer.echo(outcome.answer.to_text())


@app.command("ask")
def ask_command(
    question: str,
    principal: str = "analyst_full",
    scenario: str = "golden",
    provider: str = "inmemory",
    no_llm: bool = False,
    show_sql: bool = False,
) -> None:
    """Ask a natural-language question through the full LangGraph workflow."""
    import asyncio

    from app.results.render import AnswerEnvelope
    from app.runtime import AppRuntime

    runtime = AppRuntime.build(
        scenario=scenario, provider_name=provider, use_llm=not no_llm
    )
    typer.secho(
        f"provider={runtime.provider_name} cards={runtime.card_count} "
        f"model={runtime.llm_model or 'none (deterministic fallback)'}",
        fg=typer.colors.BRIGHT_BLACK,
    )
    import uuid

    from app.contracts.clarification import ClarificationRequest

    thread = f"cli_{uuid.uuid4().hex[:8]}"
    state = asyncio.run(runtime.ask(question, principal, thread_id=thread))

    # A clarification suspends the graph. Answer it here so the two-turn cycle is usable
    # from the terminal; a non-interactive caller gets the question and exits.
    while (pending := runtime.pending_clarification(thread)) is not None:
        request = ClarificationRequest.model_validate(pending)
        typer.echo("")
        typer.secho(request.question, fg=typer.colors.YELLOW, bold=True)
        for i, option in enumerate(request.options, start=1):
            suffix = f" — {option.effect}" if option.effect else ""
            typer.echo(f"  {i}. {option.label}{suffix}")
        if request.reason:
            typer.secho(f"  ({request.reason})", fg=typer.colors.BRIGHT_BLACK)

        # Read directly rather than via prompt(), so a pipe, a terminal and a closed stdin
        # all behave predictably. An unanswerable question is a normal outcome, not an error.
        typer.echo("Choice [1]: ", nl=False)
        line = sys.stdin.readline()
        if not line:
            typer.echo("")
            typer.secho(
                "No answer available on stdin, so this question cannot be settled here. "
                "Re-run interactively, or state the missing detail in the question.",
                fg=typer.colors.BRIGHT_BLACK,
            )
            raise typer.Exit(code=0)
        choice = line.strip() or "1"
        state = asyncio.run(runtime.resume(choice, principal, thread_id=thread))
        typer.echo("")

    if show_sql and state.get("sql"):
        typer.echo("--- compiled SQL ---")
        typer.echo(state["sql"])
        typer.echo("")
    typer.echo(AnswerEnvelope.model_validate(state["answer"]).to_text())


@app.command("doctor")
def doctor_command() -> None:
    """Report which local services are reachable."""
    import httpx

    from app.llm.client import LlmClient, planner_is_pinned
    from app.retrieval.embeddings import EmbeddingClient

    def mark(ok: bool) -> str:
        return typer.style("  ok  ", fg=typer.colors.GREEN) if ok else typer.style(
            " down ", fg=typer.colors.RED
        )

    embed = EmbeddingClient()
    typer.echo(f"{mark(embed.health())} embeddings   {embed.config.base_url} "
               f"({embed.config.model}, {embed.config.dimensions}d)")

    llm = LlmClient()
    healthy = llm.health()
    typer.echo(f"{mark(healthy)} llm router   {llm.config.base_url} "
               f"(planner={llm.config.planner_model})")
    if healthy and not planner_is_pinned(llm.config.planner_model):
        typer.secho(
            "       warning: planner uses an auto/* alias, so plan proposals are not "
            "reproducible. Set TTSQL_PLANNER_MODEL to a concrete id.",
            fg=typer.colors.YELLOW,
        )

    import os

    url = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
    try:
        up = httpx.get(url, timeout=2.0).status_code == 200
    except Exception:
        up = False
    typer.echo(f"{mark(up)} opensearch   {url}")
    if not up:
        typer.secho("       optional: the in-memory provider is used instead.",
                    fg=typer.colors.BRIGHT_BLACK)

    from fixtures.build import db_path

    typer.echo(f"{mark(db_path('golden').exists())} fixtures     {db_path('golden')}")


if __name__ == "__main__":
    app()
