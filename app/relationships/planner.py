"""Approved-relationship join planning.

The graph is a directed, typed multigraph: several edges may connect the same pair of tables
with different business roles, so a role-playing date dimension can serve order date and
shipment date without conflating them.

The shortest path is not the answer. A candidate survives only after proving grain,
cardinality and permission; ranking happens strictly inside the valid set.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.catalog.service import CatalogService
from app.contracts.catalog import Cardinality, RelationshipRecord
from app.contracts.errors import GovernedError, ReasonCode
from app.contracts.scope import Action, TrustedScope

MAX_HOPS = 4
MAX_CANDIDATES = 32

# Joining across these cardinalities multiplies the left side's rows, so an additive measure
# on the left cannot be aggregated afterwards without a semi-join or approved allocation.
_MULTIPLYING = {Cardinality.ONE_TO_MANY, Cardinality.MANY_TO_MANY}


@dataclass(frozen=True)
class PlannedJoin:
    relationship: RelationshipRecord
    from_table: str
    to_table: str
    reversed: bool
    preserves_left_multiplicity: bool


@dataclass(frozen=True)
class JoinPlan:
    base_table: str
    joins: tuple[PlannedJoin, ...]

    @property
    def tables(self) -> frozenset[str]:
        return frozenset({self.base_table}) | {j.to_table for j in self.joins}

    @property
    def hops(self) -> int:
        return len(self.joins)

    @property
    def is_additive_safe(self) -> bool:
        """True when no join multiplies the base fact's rows."""
        return all(j.preserves_left_multiplicity for j in self.joins)

    @property
    def relationship_refs(self) -> tuple[str, ...]:
        return tuple(str(j.relationship.ref) for j in self.joins)


class JoinPlanner:
    def __init__(self, catalog: CatalogService) -> None:
        self.catalog = catalog

    def _edges_from(
        self, table_id: str, scope: TrustedScope
    ) -> list[tuple[RelationshipRecord, str, bool]]:
        """Approved edges leaving a table, as (record, other_table, reversed)."""
        out = []
        for rel in self.catalog.snapshot.relationships_by_table.get(table_id, []):
            if rel.left_table == table_id:
                out.append((rel, rel.right_table, False))
            if rel.right_table == table_id:
                out.append((rel, rel.left_table, True))
        return out

    def _permitted(self, rel: RelationshipRecord, scope: TrustedScope) -> bool:
        for table_id in (rel.left_table, rel.right_table):
            if not self.catalog.policy.check(
                scope.principal_id, Action.DISCOVER, table_id
            ).allowed:
                return False
        # Every join key must be usable in a predicate, not merely readable.
        keys = {p.left_column for p in rel.predicates} | {
            p.right_column for p in rel.predicates
        }
        return not self.catalog.policy.check_all(
            scope.principal_id, Action.USE_IN_PREDICATE, frozenset(keys)
        )

    def _preserves_multiplicity(self, rel: RelationshipRecord, reversed_: bool) -> bool:
        card = rel.cardinality
        if reversed_:
            card = {
                Cardinality.MANY_TO_ONE: Cardinality.ONE_TO_MANY,
                Cardinality.ONE_TO_MANY: Cardinality.MANY_TO_ONE,
            }.get(card, card)
        return card not in _MULTIPLYING

    def plan(
        self,
        base_table: str,
        required_tables: frozenset[str],
        scope: TrustedScope,
        *,
        required_relationship_refs: frozenset[str] = frozenset(),
        forbid_multiplying: bool = True,
    ) -> JoinPlan:
        """Connect `base_table` to every required table over approved edges.

        `forbid_multiplying` is on whenever an additive measure sits on the base fact: a plan
        that multiplies the fact's rows is rejected outright rather than patched with DISTINCT.
        """
        terminals = set(required_tables) - {base_table}
        if not terminals:
            return JoinPlan(base_table=base_table, joins=())

        candidates = self._enumerate(base_table, terminals, scope, forbid_multiplying)

        if not candidates:
            raise GovernedError(
                ReasonCode.NO_APPROVED_PATH,
                f"No approved relationship path connects {base_table} to "
                f"{', '.join(sorted(terminals))} within {MAX_HOPS} hops under this principal's "
                f"permissions"
                + (" without multiplying the base fact" if forbid_multiplying else ""),
                subject=base_table,
            )

        # A rule or dimension may mandate a specific edge (e.g. the as-of customer join).
        if required_relationship_refs:
            required_only = [
                c for c in candidates if required_relationship_refs <= set(c.relationship_refs)
            ]
            if not required_only:
                raise GovernedError(
                    ReasonCode.NO_APPROVED_PATH,
                    f"No candidate plan uses the required relationship(s) "
                    f"{', '.join(sorted(required_relationship_refs))}",
                    subject=base_table,
                )
            candidates = required_only

        ranked = self._rank(candidates)
        best = ranked[0]

        # Two structurally different plans with equal rank would change the answer silently.
        if (
            len(ranked) > 1
            and self._rank_key(ranked[1]) == self._rank_key(best)
            and set(ranked[1].relationship_refs) != set(best.relationship_refs)
        ):
            raise GovernedError(
                ReasonCode.AMBIGUOUS_PATH,
                "Two equally ranked approved paths connect these tables with different "
                f"business meaning: {best.relationship_refs} vs "
                f"{ranked[1].relationship_refs}. This needs a role decision, not a guess.",
                subject=base_table,
            )
        return best

    def _enumerate(
        self,
        base_table: str,
        terminals: set[str],
        scope: TrustedScope,
        forbid_multiplying: bool,
    ) -> list[JoinPlan]:
        """Bounded breadth-first enumeration of connection subgraphs."""
        found: list[JoinPlan] = []
        start = (base_table, (), frozenset({base_table}))
        queue: deque = deque([start])

        while queue and len(found) < MAX_CANDIDATES:
            _, joins, visited = queue.popleft()
            if terminals <= visited:
                found.append(JoinPlan(base_table=base_table, joins=joins))
                continue
            if len(joins) >= MAX_HOPS:
                continue
            for frontier in sorted(visited):
                for rel, other, reversed_ in self._edges_from(frontier, scope):
                    if other in visited or not self._permitted(rel, scope):
                        continue
                    preserves = self._preserves_multiplicity(rel, reversed_)
                    if forbid_multiplying and not preserves:
                        continue
                    if rel.prohibited_usages and forbid_multiplying:
                        # Prohibited usages are recorded on the edge; honour them literally.
                        continue
                    step = PlannedJoin(
                        relationship=rel,
                        from_table=frontier,
                        to_table=other,
                        reversed=reversed_,
                        preserves_left_multiplicity=preserves,
                    )
                    queue.append((other, (*joins, step), visited | {other}))
        return found

    @staticmethod
    def _rank_key(plan: JoinPlan) -> tuple:
        # Lexicographic: aggregation safety, then fewest tables, then fewest hops, then a
        # deterministic tiebreak. Cost never compensates for an unsafe plan.
        return (
            0 if plan.is_additive_safe else 1,
            len(plan.tables),
            plan.hops,
            plan.relationship_refs,
        )

    def _rank(self, candidates: list[JoinPlan]) -> list[JoinPlan]:
        return sorted(candidates, key=self._rank_key)
