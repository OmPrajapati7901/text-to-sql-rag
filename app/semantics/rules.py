"""Deterministic rule engine.

Rule *classes* decide precedence, not a flat override chain: a preference never overrides
tenant isolation, and a table default never redefines a certified metric. Security denies win
and obligations accumulate.

Closure runs to a fixed point because a rule can introduce a table, which can activate another
rule. It is capped and must converge explicitly — a non-convergence error is never read as
permission to ignore a rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.catalog.service import CatalogService
from app.contracts.errors import GovernedError, ReasonCode
from app.contracts.expressions import columns_in
from app.contracts.ids import table_id_of
from app.contracts.scope import Action, TrustedScope
from app.contracts.semantic_plan import Obligation
from app.contracts.semantics import RuleRecord

MAX_CLOSURE_ROUNDS = 8


@dataclass(frozen=True)
class ClosureInput:
    """Seed state: what the question and its metrics already require."""

    tables: frozenset[str]
    columns: frozenset[str]
    metric_refs: frozenset[str]
    entities: frozenset[str]
    relationship_refs: frozenset[str] = frozenset()
    slots: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ClosureResult:
    tables: frozenset[str]
    columns: frozenset[str]
    relationship_refs: frozenset[str]
    obligations: tuple[Obligation, ...]
    slots: dict[str, str]
    applied_rule_refs: tuple[str, ...]
    rounds: int
    semantic_requirements: tuple[str, ...]

    @property
    def mandatory_rule_refs(self) -> tuple[str, ...]:
        return tuple(o.rule_ref for o in self.obligations)


class RuleEngine:
    def __init__(self, catalog: CatalogService) -> None:
        self.catalog = catalog
        self._by_activation: dict[str, list[RuleRecord]] = {}
        for rule in catalog.all_rules():
            self._by_activation.setdefault(rule.activation.op, []).append(rule)
        for bucket in self._by_activation.values():
            bucket.sort(key=lambda r: r.precedence)

    # -- activation ------------------------------------------------------

    def _activates(self, rule: RuleRecord, state: ClosureInput) -> bool:
        act = rule.activation
        match act.op:
            case "always":
                return True
            case "any_scan_with_tag":
                return any(
                    act.tag in self.catalog.snapshot.get("tables", t).tags
                    for t in state.tables
                )
            case "population_contains_entity":
                return act.entity in state.entities
            case "uses_table":
                return act.object_id in state.tables
            case "uses_column":
                return act.object_id in state.columns
            case "uses_metric":
                return act.metric_ref in state.metric_refs
            case "slot_unset":
                return act.slot not in state.slots
        return False

    def _candidates(self, state: ClosureInput) -> list[RuleRecord]:
        seen: dict[str, RuleRecord] = {}
        for bucket in self._by_activation.values():
            for rule in bucket:
                if self._activates(rule, state):
                    seen[str(rule.ref)] = rule
        return sorted(seen.values(), key=lambda r: r.precedence)

    # -- closure ---------------------------------------------------------

    def close(self, seed: ClosureInput, scope: TrustedScope) -> ClosureResult:
        state = seed
        obligations: dict[str, Obligation] = {}
        applied: dict[str, RuleRecord] = {}
        semantic_reqs: list[str] = []
        slots = dict(seed.slots)
        rounds = 0

        while rounds < MAX_CLOSURE_ROUNDS:
            rounds += 1
            before = (state.tables, state.columns, state.relationship_refs, tuple(sorted(slots)))

            for rule in self._candidates(replace(state, slots=slots)):
                ref = str(rule.ref)
                effect = rule.effect

                if effect.op == "set_slot":
                    # Defaults fill unset slots only; they never overwrite a stated value.
                    if effect.slot not in slots and effect.slot_value is not None:
                        slots[effect.slot] = effect.slot_value
                        applied[ref] = rule
                    continue

                if effect.op == "prefer_relationship":
                    applied[ref] = rule
                    continue

                if effect.op == "require_semijoin_or_allocation":
                    if ref not in semantic_reqs:
                        semantic_reqs.append(ref)
                        applied[ref] = rule
                    continue

                # Mandatory effects contribute obligations and may pull in dependencies.
                new_tables = set(state.tables) | set(effect.required_tables)
                new_columns = set(state.columns)
                new_rels = set(state.relationship_refs)

                if effect.predicate is not None:
                    pred_cols = columns_in(effect.predicate)
                    new_columns |= pred_cols
                    new_tables |= {table_id_of(c) for c in pred_cols}

                if effect.required_relationship_ref:
                    new_rels.add(effect.required_relationship_ref)
                    rel = self.catalog.get_relationship(effect.required_relationship_ref, scope)
                    new_tables |= {rel.left_table, rel.right_table}
                    for pred in rel.predicates:
                        new_columns |= {pred.left_column, pred.right_column}

                # Every addition is reauthorized. A rule may not smuggle in an object the
                # principal cannot use.
                added_cols = frozenset(new_columns - state.columns)
                if added_cols:
                    self.catalog.require_columns_usable(
                        added_cols, scope, Action.USE_IN_PREDICATE
                    )
                for table_id in sorted(set(new_tables) - set(state.tables)):
                    self.catalog.get_table(table_id, scope)

                obligations[ref] = Obligation(
                    rule_ref=ref,
                    rule_class=rule.rule_class.value,
                    predicate=effect.predicate,
                    placement=effect.placement,
                    required_tables=tuple(sorted(effect.required_tables)),
                    required_relationship_ref=effect.required_relationship_ref,
                )
                applied[ref] = rule
                state = replace(
                    state,
                    tables=frozenset(new_tables),
                    columns=frozenset(new_columns),
                    relationship_refs=frozenset(new_rels),
                )

            after = (state.tables, state.columns, state.relationship_refs, tuple(sorted(slots)))
            if before == after:
                break
        else:
            raise GovernedError(
                ReasonCode.RULE_CLOSURE_NOT_CONVERGED,
                f"Rule closure did not converge in {MAX_CLOSURE_ROUNDS} rounds. This is a "
                f"catalog defect and is never a reason to proceed without the rules.",
            )

        self._detect_conflicts(applied.values())

        return ClosureResult(
            tables=state.tables,
            columns=state.columns,
            relationship_refs=state.relationship_refs,
            obligations=tuple(obligations[k] for k in sorted(obligations)),
            slots=slots,
            applied_rule_refs=tuple(sorted(applied)),
            rounds=rounds,
            semantic_requirements=tuple(semantic_reqs),
        )

    def _detect_conflicts(self, rules) -> None:
        """Two mandatory rules of the same class writing the same slot with different values
        is a governance defect. It blocks rather than picking one."""
        slot_writers: dict[str, list[tuple[str, str]]] = {}
        for rule in rules:
            if rule.effect.op == "set_slot" and rule.effect.slot:
                slot_writers.setdefault(rule.effect.slot, []).append(
                    (str(rule.ref), rule.effect.slot_value or "")
                )
        for slot, writers in slot_writers.items():
            values = {v for _, v in writers}
            if len(values) > 1:
                raise GovernedError(
                    ReasonCode.RULE_CONFLICT,
                    f"Rules disagree on slot {slot!r}: "
                    + ", ".join(f"{r} -> {v}" for r, v in writers),
                    subject=slot,
                )
