"""Graph nodes.

Each node is a thin call into a tested module. Most contain no model call at all: routing is
the only place language interpretation happens, and binding/compilation/validation/execution
are entirely deterministic.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.authorization.scope import reauthorize
from app.contracts.clarification import ClarificationRequest, validate_answer
from app.contracts.errors import Finding, GovernedError, ReasonCode
from app.contracts.semantic_plan import SemanticRequest
from app.graph.state import (
    MAX_RETRIEVAL_ROUNDS,
    GraphState,
    RequestContext,
    clear_downstream,
)
from app.planning import clarify as clarify_rules
from app.planning.repair import MAX_REPAIR_ATTEMPTS, propose_repair
from app.results.render import refusal, render
from app.results.validation import validate_result
from app.sql.attestation import build_attestations
from app.sql.attestation import check as attestation_check


def _ctx(runtime: Runtime[RequestContext]) -> RequestContext:
    return runtime.context


async def admit(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """Authenticate-derived scope is refreshed on entry and on every resume."""
    ctx = _ctx(runtime)
    try:
        scope = reauthorize(ctx.services.policy, ctx.scope)
    except GovernedError as exc:
        return {
            "decision": "denied",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
        }
    return {
        "decision": "allowed",
        "policy_ref": scope.policy_ref,
        "snapshot_ref": scope.snapshot_id,
        "plan_revision": state.get("plan_revision", 1),
    }


async def understand_and_route(
    state: GraphState, runtime: Runtime[RequestContext]
) -> dict[str, Any]:
    ctx = _ctx(runtime)
    question = state["question"]
    try:
        understanding = ctx.understander.understand(question, ctx.scope)
    except GovernedError as exc:
        return {
            "route": "unsupported",
            "decision": "blocked",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
        }

    sketch = understanding.sketch
    return {
        "route": sketch.route,
        "intent": sketch.model_dump(mode="json"),
        "exact_metric_refs": list(understanding.exact_metric_refs),
        "exact_dimension_refs": list(understanding.exact_dimension_refs),
    }


async def retrieve_candidates(
    state: GraphState, runtime: Runtime[RequestContext]
) -> dict[str, Any]:
    ctx = _ctx(runtime)
    rounds = state.get("retrieval_rounds", 0)
    budget = 20 if rounds == 0 else 40 * (rounds + 1)

    outcome = await ctx.retrieval.search(state["question"], ctx.scope, budget=budget)
    return {
        "candidates": [c.model_dump(mode="json") for c in outcome.candidates],
        "retrieval_status": outcome.status.value,
        "retrieval_findings": [f.message for f in outcome.findings],
        "retrieval_rounds": rounds + 1,
        "decision": "ready" if outcome.candidates or outcome.exact_matches else "empty",
    }


async def expand_retrieval(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    if state.get("retrieval_rounds", 0) >= MAX_RETRIEVAL_ROUNDS + 1:
        return {
            "decision": "blocked",
            "reason_codes": ["BUDGET_EXHAUSTED"],
            "notes": ["Retrieval expansion budget exhausted without complete evidence."],
        }
    return {"decision": "ready"}


async def propose_plan(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    ctx = _ctx(runtime)
    from app.contracts.retrieval import Candidate

    candidates = tuple(Candidate.model_validate(c) for c in state.get("candidates", []))

    # On resume, the settled proposal is authoritative. Re-proposing here would discard the
    # user's clarification while `clarified_slots` suppressed re-asking for it -- the query
    # would then run over a population nobody chose.
    if state.get("clarification_answer") and state.get("proposal"):
        settled = SemanticRequest.model_validate(state["proposal"])
        asked = frozenset(state.get("clarified_slots", []))
        followup = clarify_rules.detect(
            ctx.services.catalog,
            settled,
            candidates,
            ctx.scope,
            exact_metric_refs=tuple(state.get("exact_metric_refs", [])),
            already_asked=asked,
        )
        if followup is not None:
            return {
                "clarification": followup.model_dump(mode="json"),
                "decision": "clarify",
            }
        return {"clarification": None, "decision": "ready"}

    try:
        proposal = ctx.proposer.propose(
            state["question"],
            candidates,
            ctx.scope,
            exact_metric_refs=tuple(state.get("exact_metric_refs", [])),
            exact_dimension_refs=tuple(state.get("exact_dimension_refs", [])),
        )
    except GovernedError as exc:
        return {
            "decision": "blocked",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
        }

    if proposal.request is None:
        # No authorized metric matched. Expand once, then stop -- absence of retrieval
        # evidence is never reported as "this data does not exist".
        if state.get("retrieval_rounds", 0) <= MAX_RETRIEVAL_ROUNDS:
            return {"decision": "expand", "notes": [proposal.notes]}
        return {
            "decision": "blocked",
            "reason_codes": ["COVERAGE_INCOMPLETE"],
            "notes": [
                proposal.notes,
                (
                    "No authorized metric in this catalog answers the question. This means "
                    "no match was found under your permissions, not that the data does not "
                    "exist."
                ),
            ],
        }

    request = proposal.request
    asked = frozenset(state.get("clarified_slots", []))
    question = clarify_rules.detect(
        ctx.services.catalog,
        request,
        candidates,
        ctx.scope,
        exact_metric_refs=tuple(state.get("exact_metric_refs", [])),
        already_asked=asked,
    )
    if question is not None:
        return {
            "proposal": request.model_dump(mode="json"),
            "clarification": question.model_dump(mode="json"),
            "decision": "clarify",
            "notes": [proposal.notes] if proposal.notes else [],
        }

    return {
        "proposal": request.model_dump(mode="json"),
        "clarification": None,
        "decision": "ready",
        "notes": [proposal.notes] if proposal.notes else [],
    }


async def clarify(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """Ask one targeted question and suspend.

    This node has no side effects before the interrupt, because resuming re-runs it from the
    top. The resumed payload is untrusted and is validated against the options that were
    offered, so a resume cannot introduce an object retrieval and authorization never saw.
    """
    request = ClarificationRequest.model_validate(state["clarification"])

    answer_payload = interrupt(request.model_dump(mode="json"))

    try:
        answer = validate_answer(answer_payload, request)
    except GovernedError as exc:
        return {
            "decision": "blocked",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
        }

    proposal = SemanticRequest.model_validate(state["proposal"])
    updated = clarify_rules.apply_answer(proposal, answer.slot, answer.value)

    # Record the settled slot so the same question is never asked twice, and carry every
    # other resolved slot forward rather than restarting the dialogue.
    settled = [*state.get("clarified_slots", []), answer.slot]
    return {
        "proposal": updated.model_dump(mode="json"),
        "clarification": None,
        "clarification_answer": answer.model_dump(mode="json"),
        "clarified_slots": settled,
        "decision": "resumed",
        "notes": [f"Clarified {answer.slot}: {answer.value}"],
    }


async def repair(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """Bounded, meaning-preserving repair.

    The repairable set is deliberately narrow: anything that would change the population,
    metric, period or filters is refused here and terminates instead.
    """
    attempts = state.get("repair_attempts", 0)
    if attempts >= MAX_REPAIR_ATTEMPTS:
        return {
            "decision": "blocked",
            "reason_codes": ["BUDGET_EXHAUSTED"],
            "notes": [f"Plan repair budget exhausted after {attempts} attempt(s)."],
        }

    request = SemanticRequest.model_validate(state["proposal"])
    findings = [
        Finding(code=ReasonCode(code), message=note)
        for code, note in zip(state.get("reason_codes", []), state.get("notes", []), strict=False)
    ]
    columns = tuple((state.get("plan_summary") or {}).get("columns", ()))

    try:
        outcome = propose_repair(request, findings, result_columns=columns)
    except GovernedError as exc:
        return {
            "decision": "blocked",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
        }

    if not outcome.repaired or outcome.request is None:
        return {
            "decision": "blocked",
            "reason_codes": state.get("reason_codes", ["VALIDATION_FAILED"]),
            "notes": [*state.get("notes", []), outcome.description],
        }

    revision = state.get("plan_revision", 1) + 1
    return {
        **clear_downstream(revision),
        "proposal": outcome.request.model_dump(mode="json"),
        "repair_attempts": attempts + 1,
        "decision": "ready",
        "reason_codes": [],
        "notes": [outcome.description],
    }


async def bind_validate_compile(
    state: GraphState, runtime: Runtime[RequestContext]
) -> dict[str, Any]:
    """Deterministic: binding, rule closure, join planning, compilation and every gate."""
    ctx = _ctx(runtime)
    s = ctx.services
    request = SemanticRequest.model_validate(state["proposal"])
    revision = state.get("plan_revision", 1)

    try:
        bound = s.binder.bind(
            request, ctx.scope, plan_id=state["request_id"], plan_revision=revision
        )
        logical = s.lowering.lower(bound.plan, bound.join_plan, ctx.scope, dialect=s.dialect)
        parameters: dict[str, Any] = {"p_tenant": ctx.scope.tenant_id}
        if bound.plan.time:
            parameters["p_time_start"] = bound.plan.time.range.start
            parameters["p_time_end"] = bound.plan.time.range.end_exclusive
        compiled = s.compiler.compile(logical, ctx.scope, parameters=parameters)
    except GovernedError as exc:
        from app.planning.binder import expected_output_columns
        from app.planning.repair import REPAIRABLE_CODES

        repairable = exc.finding.code in REPAIRABLE_CODES
        budget_left = state.get("repair_attempts", 0) < MAX_REPAIR_ATTEMPTS
        return {
            "decision": "repairable" if (repairable and budget_left) else "blocked",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
            "plan_summary": {
                "columns": list(expected_output_columns(request, s.catalog)),
            },
        }

    report = s.validator.validate(compiled, logical, bound.plan, ctx.scope)
    if not report.passed:
        from app.planning.repair import REPAIRABLE_CODES

        codes = [f.code for f in report.blocking]
        repairable = bool(set(codes) & REPAIRABLE_CODES)
        budget_left = state.get("repair_attempts", 0) < MAX_REPAIR_ATTEMPTS
        return {
            "decision": "repairable" if (repairable and budget_left) else "blocked",
            "sql": compiled.sql,
            "validation": {"passed": False, "findings": [str(f) for f in report.blocking]},
            "reason_codes": codes,
            "notes": [f.message for f in report.blocking],
            "plan_summary": {
                "columns": list(bound.plan.result_contract.columns),
            },
        }

    ctx.artifacts.discard_from(revision)
    ctx.artifacts.put(revision, (bound, logical, compiled, parameters))
    return {
        "decision": "ready",
        "sql": compiled.sql,
        "validation": {"passed": True, "findings": []},
        "plan_summary": {
            "metric_refs": list(bound.plan.metric_refs),
            "rule_refs": list(bound.plan.rule_refs),
            "relationship_refs": list(bound.plan.relationship_refs),
            "columns": list(bound.plan.result_contract.columns),
            "coverage_complete": bound.plan.coverage.is_complete,
        },
    }


async def execute(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """Preflight, attest the join contract, then execute through the gateway."""
    ctx = _ctx(runtime)
    revision = state.get("plan_revision", 1)
    try:
        bound, logical, compiled, parameters = ctx.artifacts.get(revision)
    except KeyError as exc:
        return {"decision": "blocked", "reason_codes": ["COMPILER_DEFECT"], "notes": [str(exc)]}

    try:
        fresh = reauthorize(ctx.services.policy, ctx.scope)
        ticket = ctx.gateway.issue_ticket(compiled, fresh)
        ctx.gateway.preflight(compiled, ticket)

        for attestation in build_attestations(logical, ctx.services.catalog, fresh, parameters):
            _, overlapping, unmatched = ctx.gateway.run_attestation(attestation)
            finding = attestation_check(attestation, overlapping, unmatched)
            if finding is not None:
                return {
                    "decision": "blocked",
                    "reason_codes": [finding.code],
                    "notes": [finding.message],
                }

        result = ctx.gateway.execute(compiled, ticket, fresh)
    except GovernedError as exc:
        return {
            "decision": "blocked",
            "reason_codes": [exc.finding.code],
            "notes": [exc.finding.message],
        }

    report = validate_result(result, bound.plan)
    if not report.passed:
        return {
            "decision": "blocked",
            "reason_codes": [f.code for f in report.blocking],
            "notes": [f.message for f in report.blocking],
        }

    envelope = render(result, bound.plan, notes=tuple(f.message for f in report.findings))
    return {
        "decision": "answered",
        "job_id": result.job_id,
        "result_columns": list(result.columns),
        "result_rows": [list(r) for r in result.rows],
        "answer": envelope.model_dump(mode="json"),
    }


async def definition_answer(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """A definition question needs no SQL. The answer comes from the authoritative contract."""
    ctx = _ctx(runtime)
    refs = state.get("exact_metric_refs", [])
    if not refs:
        return {
            "decision": "blocked",
            "reason_codes": ["OBJECT_NOT_FOUND"],
            "notes": ["No authorized definition matched that term."],
        }

    lines: list[str] = []
    for ref in refs:
        try:
            metric = ctx.services.catalog.get_metric_contract(ref, ctx.scope)
        except GovernedError as exc:
            return {
                "decision": "blocked",
                "reason_codes": [exc.finding.code],
                "notes": [exc.finding.message],
            }
        grains = ", ".join(g.value for g in metric.time.grains)
        lines.append(
            f"{metric.name} ({metric.ref}): {metric.description} "
            f"Unit: {metric.unit}. Time field: {metric.time.time_column} in "
            f"{metric.time.timezone}. Supported grains: {grains}. "
            f"Required rules: {', '.join(metric.required_rule_refs) or 'none'}."
        )
    envelope = refusal("answered", tuple(lines), ())
    envelope = envelope.model_copy(update={"evidence_status": "verified"})
    return {"decision": "answered", "answer": envelope.model_dump(mode="json")}


async def metadata_answer(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """Describe only the catalog surface discoverable and executable by this principal."""

    ctx = _ctx(runtime)
    catalog = ctx.services.catalog
    tables = catalog.queryable_tables(ctx.scope)
    metrics = catalog.executable_metrics(ctx.scope)

    table_text = ", ".join(f"{table.name} (`{table.id}`)" for table in tables) or "none"
    metric_text = ", ".join(f"{metric.name} (`{metric.id}`)" for metric in metrics) or "none"
    lines = (
        f"This application queries a local read-only DuckDB fixture through governed "
        f"catalog snapshot `{ctx.scope.snapshot_id}`.",
        f"Queryable certified tables: {table_text}.",
        f"Executable governed metrics: {metric_text}.",
        "It cannot browse arbitrary databases or run unrestricted SQL. Access is "
        "rechecked for every request.",
    )
    envelope = refusal("answered", tuple(lines), ()).model_copy(
        update={"evidence_status": "verified"}
    )
    return {"decision": "answered", "answer": envelope.model_dump(mode="json")}


async def stop(state: GraphState, runtime: Runtime[RequestContext]) -> dict[str, Any]:
    """One explicit terminal response. Existence-aware and non-leaking."""
    status = {
        "denied": "denied",
        "unsupported": "unsupported",
        "clarify": "clarify",
    }.get(state.get("route", ""), "unavailable")
    if state.get("decision") == "denied":
        status = "denied"
    default_note = {
        "unsupported": (
            "This system answers read-only analytics questions over governed metrics. It "
            "does not modify data, and the requested operation is not a certified capability."
        ),
        "clarify": (
            "That question is ambiguous in a way that would change the answer. Name the "
            "measure and period you mean."
        ),
        "denied": "You are not authorized for this request.",
    }.get(status, "This request could not be completed.")
    notes = tuple(state.get("notes") or (default_note,))
    codes = tuple(str(c) for c in state.get("reason_codes", []))
    return {"answer": refusal(status, notes, codes).model_dump(mode="json")}
