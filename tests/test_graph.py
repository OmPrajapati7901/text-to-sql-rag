"""LangGraph workflow tests.

These run with the deterministic fallback understander/proposer by default, so the suite does
not depend on a live model. The LLM path is exercised separately under the `llm` marker.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

from app.results.render import AnswerEnvelope

load_dotenv()

pytestmark = pytest.mark.e2e


def _llm_up() -> bool:
    try:
        from app.llm.client import LlmClient

        return LlmClient().health()
    except Exception:
        return False


LLM_AVAILABLE = _llm_up()


@pytest.fixture(scope="module")
def runtime():
    """Deterministic runtime: no model, so routing and proposal are code paths."""
    from app.runtime import AppRuntime

    return AppRuntime.build(use_llm=False, use_embeddings=False)


@pytest.fixture(scope="module")
def llm_runtime():
    from app.runtime import AppRuntime

    if not LLM_AVAILABLE:
        pytest.skip("OmniRoute not reachable")
    return AppRuntime.build(use_llm=True)


def answer(state) -> AnswerEnvelope:
    return AnswerEnvelope.model_validate(state["answer"])


async def _ask_and_settle(runtime, question, principal="analyst_full", *, thread, choice="1"):
    """Ask, and answer a clarification if one is raised.

    The deterministic understander cannot resolve a period from free text, so questions
    without an explicit range suspend here. That is the correct behaviour -- this helper
    just drives the two-turn cycle.
    """
    state = await runtime.ask(question, principal, thread_id=thread)
    if runtime.pending_clarification(thread) is not None:
        state = await runtime.resume(choice, principal, thread_id=thread)
    return state


async def test_graph_answers_the_golden_question(runtime):
    state = await _ask_and_settle(runtime, "monthly revenue by region", thread="t-golden")
    env = answer(state)
    assert env.status == "answered", env.notes
    assert env.columns == ("month", "region_at_order", "revenue_usd")
    assert env.rows == (
        ("2026-04-01T00:00:00Z", "North", "200.00"),
        ("2026-06-01T00:00:00Z", "South", "50.00"),
    )
    assert env.applied_rules
    assert "security.tenant_scope@5" in env.applied_rules


async def test_graph_keeps_scope_out_of_checkpointed_state(runtime):
    """State must carry an opaque policy_ref, never the scope itself."""
    state = await _ask_and_settle(runtime, "monthly revenue by region", thread="t-scope")
    assert state["policy_ref"].startswith("policy:")
    serialized = str(state)
    assert "TrustedScope" not in serialized
    assert "token" not in state


async def test_graph_never_checkpoints_sql_parameters(runtime):
    """Parameter values can be sensitive and belong in the artifact store, not in state."""
    state = await _ask_and_settle(runtime, "monthly revenue by region", thread="t-params")
    assert "parameter_values" not in state
    assert "?" in (state.get("sql") or ""), "SQL in state must still be parameterized"


async def test_unauthorized_principal_terminates_explicitly(runtime):
    """The denial must arrive on the first turn. Asking this principal to choose a period
    for a query that will be denied either way is pure friction."""
    state = await runtime.ask(
        "monthly revenue by region", "analyst_restricted", thread_id="t-denied"
    )
    assert runtime.pending_clarification("t-denied") is None, (
        "must not clarify a request that cannot be executed by this principal"
    )
    env = answer(state)
    assert env.status == "unavailable"
    assert "NOT_AUTHORIZED" in env.reason_codes


async def test_unknown_measure_abstains_without_claiming_absence(runtime):
    state = await runtime.ask("what is our net promoter score")
    env = answer(state)
    assert env.status in {"unavailable", "clarify"}
    assert env.rows == ()


async def test_every_terminal_branch_produces_an_answer(runtime):
    for i, (question, principal) in enumerate(
        [
            ("monthly revenue by region", "analyst_full"),
            ("monthly revenue by region", "analyst_restricted"),
            ("what is our net promoter score", "analyst_full"),
        ]
    ):
        state = await _ask_and_settle(runtime, question, principal, thread=f"t-branch-{i}")
        assert state.get("answer") is not None, f"{question} produced no answer envelope"
        env = answer(state)
        assert env.status in {
            "answered",
            "unavailable",
            "blocked",
            "denied",
            "unsupported",
            "clarify",
            "failed",
        }


async def test_artifact_store_is_keyed_by_revision(runtime):
    """A compiled artifact must be addressable by the revision state names."""
    from app.graph.state import ArtifactStore

    store = ArtifactStore()
    store.put(1, ("a",))
    store.put(2, ("b",))
    assert store.get(2) == ("b",)
    store.discard_from(2)
    with pytest.raises(KeyError):
        store.get(2)
    assert store.get(1) == ("a",)


# --- live model path --------------------------------------------------------


@pytest.mark.llm
async def test_llm_routes_and_answers_the_design_question(llm_runtime):
    state = await llm_runtime.ask("Show monthly revenue by customer region in Q2 2026")
    env = answer(state)
    assert env.status == "answered", env.notes
    rows = [(r[0][:10], r[1], str(r[2])) for r in env.rows]
    assert rows == [("2026-04-01", "North", "200.00"), ("2026-06-01", "South", "50.00")]


@pytest.mark.llm
async def test_llm_resolves_a_synonym_through_the_glossary(llm_runtime):
    """'top line' and 'territory' are glossary synonyms, resolved deterministically."""
    state = await llm_runtime.ask("How is our top line doing by territory in Q2 2026?")
    env = answer(state)
    assert env.status == "answered", env.notes
    assert env.metric_versions == {"finance.revenue": "v7"}


@pytest.mark.llm
async def test_llm_answers_a_definition_without_sql(llm_runtime):
    state = await llm_runtime.ask("What does Revenue mean?")
    env = answer(state)
    assert env.status == "answered"
    assert env.rows == ()
    assert state.get("sql") is None
    assert any("finance.revenue@7" in n for n in env.notes)


@pytest.mark.llm
async def test_llm_abstains_on_a_metric_that_does_not_exist(llm_runtime):
    state = await llm_runtime.ask("What was our customer satisfaction score in Q2 2026?")
    env = answer(state)
    assert env.status == "unavailable"
    assert any("not that the data does not exist" in n for n in env.notes)


@pytest.mark.llm
async def test_llm_cannot_route_a_mutation(llm_runtime):
    state = await llm_runtime.ask("Delete all orders from the database")
    env = answer(state)
    assert env.status == "unsupported"
    assert state.get("sql") is None


# --- clarification through the graph ----------------------------------------


async def test_clarification_suspends_and_resumes(runtime):
    """A metric query with no period must ask rather than invent one."""
    from app.contracts.clarification import ClarificationRequest

    thread = "t-clarify-suspend"
    await runtime.ask("revenue", "analyst_full", thread_id=thread)

    pending = runtime.pending_clarification(thread)
    assert pending is not None, "must suspend on a missing reporting period"
    question = ClarificationRequest.model_validate(pending)
    assert question.slot == "time_range"

    final = await runtime.resume("1", "analyst_full", thread_id=thread)
    env = answer(final)
    assert env.status == "answered", env.notes
    assert final["clarified_slots"] == ["time_range"]


async def test_the_clarified_choice_actually_changes_the_answer(runtime):
    """The clarification is material: two choices give different numbers. It also proves the
    settled slot survives resume rather than being re-proposed away."""
    results = {}
    for choice, thread in [("1", "t-clar-q2"), ("3", "t-clar-q1")]:
        await runtime.ask("revenue", "analyst_full", thread_id=thread)
        final = await runtime.resume(choice, "analyst_full", thread_id=thread)
        env = answer(final)
        assert env.status == "answered", env.notes
        results[choice] = str(env.rows[0][0])

    assert results["1"] == "250.00", "Q2 2026 is the golden total"
    assert results["3"] == "444.00", "Q1 2026 contains only the January order"
    assert results["1"] != results["3"]


async def test_resume_reports_the_applied_time_window(runtime):
    thread = "t-clar-window"
    await runtime.ask("revenue", "analyst_full", thread_id=thread)
    final = await runtime.resume("1", "analyst_full", thread_id=thread)
    env = answer(final)
    assert "2026-04-01" in env.time_interpretation
    assert "2026-07-01" in env.time_interpretation
    assert "end exclusive" in env.time_interpretation


async def test_resume_with_an_unoffered_value_is_blocked(runtime):
    thread = "t-clar-inject"
    await runtime.ask("revenue", "analyst_full", thread_id=thread)
    final = await runtime.resume(
        {"slot": "time_range", "value": "1970-01-01/2999-01-01"},
        "analyst_full",
        thread_id=thread,
    )
    env = answer(final)
    assert env.status != "answered"
    assert "NOT_AUTHORIZED" in env.reason_codes
