"""The deterministic request pipeline.

Bind -> lower -> compile -> validate -> admit -> preflight -> execute -> check -> release.

Every stage can terminate explicitly. This module contains no LLM call: it is the path a
hand-authored semantic request takes, and the path the graph's nodes delegate to.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.authorization.scope import reauthorize
from app.contracts.errors import GovernedError
from app.contracts.scope import Action, TrustedScope
from app.contracts.semantic_plan import BoundSemanticPlan, CompiledQuery, SemanticRequest
from app.results.render import AnswerEnvelope, refusal, render
from app.results.validation import ResultReport, validate_result
from app.services import Services
from app.sql.attestation import build_attestations
from app.sql.attestation import check as attestation_check
from app.sql.gateway import DuckDbGateway, QueryResult
from app.sql.validators.suite import ValidationReport


@dataclass
class PipelineOutcome:
    answer: AnswerEnvelope
    plan: BoundSemanticPlan | None = None
    compiled: CompiledQuery | None = None
    validation: ValidationReport | None = None
    result: QueryResult | None = None
    result_report: ResultReport | None = None
    preflight: dict[str, Any] | None = None


class Pipeline:
    def __init__(self, services: Services, gateway: DuckDbGateway) -> None:
        self.s = services
        self.gateway = gateway

    def run(
        self,
        request: SemanticRequest,
        scope: TrustedScope,
        *,
        plan_id: str | None = None,
        plan_revision: int = 1,
    ) -> PipelineOutcome:
        plan_id = plan_id or f"req_{uuid.uuid4().hex[:8]}"

        # 1. Bind: resolve IDs and versions, close rules, plan joins, authorize everything.
        try:
            bound = self.s.binder.bind(
                request, scope, plan_id=plan_id, plan_revision=plan_revision
            )
        except GovernedError as exc:
            return PipelineOutcome(
                answer=refusal("unavailable", (exc.finding.message,), (exc.finding.code,))
            )

        plan = bound.plan

        # 2. Lower and compile.
        try:
            logical = self.s.lowering.lower(
                plan, bound.join_plan, scope, dialect=self.s.dialect
            )
            parameters = self._parameters(plan, scope)
            compiled = self.s.compiler.compile(logical, scope, parameters=parameters)
        except GovernedError as exc:
            return PipelineOutcome(
                answer=refusal("unavailable", (exc.finding.message,), (exc.finding.code,)),
                plan=plan,
            )

        # 3. Static validation, against the plan AND the re-parsed SQL.
        validation = self.s.validator.validate(compiled, logical, plan, scope)
        if not validation.passed:
            return PipelineOutcome(
                answer=refusal(
                    "blocked",
                    tuple(f.message for f in validation.blocking),
                    tuple(f.code for f in validation.blocking),
                ),
                plan=plan,
                compiled=compiled,
                validation=validation,
            )

        # 4. Reauthorize immediately before admission -- policy may have changed since binding.
        fresh = reauthorize(self.s.policy, scope)
        if fresh.policy_epoch != plan.policy_epoch:
            return PipelineOutcome(
                answer=refusal(
                    "blocked",
                    (
                        (
                            "Authorization changed while this request was being planned; it "
                            "must be rebound rather than executed."
                        ),
                    ),
                    ("POLICY_EPOCH_CHANGED",),
                ),
                plan=plan, compiled=compiled, validation=validation,
            )

        # 5. Admit, preflight, attest the join contract, then execute.
        try:
            ticket = self.gateway.issue_ticket(compiled, fresh)
            preflight = self.gateway.preflight(compiled, ticket)

            # DESIGN §40 execution prerequisite: prove each fact matches exactly one
            # dimension version before aggregating across an as-of join. An overlap double
            # counts while leaving the output grain perfectly unique, so no downstream
            # result check can detect it.
            for attestation in build_attestations(logical, self.s.catalog, fresh, parameters):
                _, overlapping, unmatched = self.gateway.run_attestation(attestation)
                finding = attestation_check(attestation, overlapping, unmatched)
                if finding is not None:
                    return PipelineOutcome(
                        answer=refusal("blocked", (finding.message,), (finding.code,)),
                        plan=plan, compiled=compiled, validation=validation,
                        preflight=preflight,
                    )

            result = self.gateway.execute(compiled, ticket, fresh)
        except GovernedError as exc:
            return PipelineOutcome(
                answer=refusal("failed", (exc.finding.message,), (exc.finding.code,)),
                plan=plan, compiled=compiled, validation=validation,
            )

        # 6. Result contract.
        report = validate_result(result, plan)
        if not report.passed:
            return PipelineOutcome(
                answer=refusal(
                    "blocked",
                    tuple(f.message for f in report.blocking),
                    tuple(f.code for f in report.blocking),
                ),
                plan=plan, compiled=compiled, validation=validation,
                result=result, result_report=report, preflight=preflight,
            )

        # 7. Release: recheck disclosure permission on every projected object.
        denied = self._disclosure_denials(plan, fresh)
        if denied:
            return PipelineOutcome(
                answer=refusal(
                    "blocked",
                    (f"Disclosure not permitted for: {', '.join(denied)}",),
                    ("NOT_AUTHORIZED",),
                ),
                plan=plan, compiled=compiled, validation=validation,
                result=result, result_report=report, preflight=preflight,
            )

        notes = tuple(f.message for f in report.findings)
        return PipelineOutcome(
            answer=render(result, plan, notes=notes),
            plan=plan, compiled=compiled, validation=validation,
            result=result, result_report=report, preflight=preflight,
        )

    def _parameters(self, plan: BoundSemanticPlan, scope: TrustedScope) -> dict[str, Any]:
        params: dict[str, Any] = {"p_tenant": scope.tenant_id}
        if plan.time:
            params["p_time_start"] = plan.time.range.start
            params["p_time_end"] = plan.time.range.end_exclusive
        return params

    def _disclosure_denials(self, plan: BoundSemanticPlan, scope: TrustedScope) -> list[str]:
        subjects = [m.ref.split("@")[0] for m in plan.metrics]
        subjects += [d.column for d in plan.dimensions]
        return [
            s for s in subjects
            if not self.s.policy.check(scope.principal_id, Action.DISCLOSE, s).allowed
        ]
