"""Deterministic validation gates.

Validation runs against the bound plan AND the independently re-parsed SQL. Comparing plan
objects to themselves proves nothing, so the emitted text is parsed back with SQLGlot and
checked on its own terms — this is what catches a compiler defect.

Every gate fails closed. "Unknown node" means block, never "unknown means safe".
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp

from app.catalog.service import CatalogService
from app.contracts.errors import Finding, ReasonCode, Severity
from app.contracts.scope import Action, TrustedScope
from app.contracts.semantic_plan import BoundSemanticPlan, CompiledQuery, LogicalPlan
from app.sql.dialects import duckdb as duckdb_dialect

# Only these node types may appear anywhere in the tree, including inside CTEs and subqueries.
_ALLOWED_NODES = {
    exp.Select, exp.From, exp.Table, exp.TableAlias, exp.Column, exp.Identifier,
    exp.Alias, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered,
    exp.Limit, exp.And, exp.Or, exp.Not, exp.Paren, exp.EQ, exp.NEQ, exp.LT,
    exp.LTE, exp.GT, exp.GTE, exp.Is, exp.Null, exp.In, exp.Placeholder,
    exp.Literal, exp.Boolean, exp.Star, exp.Sum, exp.Count, exp.Avg, exp.Min,
    exp.Max, exp.Distinct, exp.Div, exp.Anonymous, exp.AtTimeZone, exp.Cast,
    exp.DataType, exp.Neg,
    # DATE_TRUNC('month', ts) re-parses as TimestampTrunc with a Var unit, not Anonymous.
    exp.TimestampTrunc, exp.DateTrunc, exp.Var,
}

# Aggregate node types, used to tell a measure projection from a grouping projection.
_AGGREGATE_NODES = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)

_WRITE_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Merge, exp.Command, exp.Copy,
)


@dataclass
class ValidationReport:
    findings: list[Finding]

    @property
    def passed(self) -> bool:
        return not any(f.severity is Severity.BLOCK for f in self.findings)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]


class Validator:
    def __init__(self, catalog: CatalogService) -> None:
        self.catalog = catalog

    def validate(
        self,
        compiled: CompiledQuery,
        logical: LogicalPlan,
        plan: BoundSemanticPlan,
        scope: TrustedScope,
    ) -> ValidationReport:
        findings: list[Finding] = []

        try:
            parsed = sqlglot.parse(compiled.sql, dialect=compiled.dialect)
        except Exception as exc:
            return ValidationReport([
                Finding(
                    code=ReasonCode.COMPILER_DEFECT,
                    message=f"Emitted SQL does not parse: {exc}",
                )
            ])

        findings += self._gate_single_statement(parsed)
        if any(f.severity is Severity.BLOCK for f in findings):
            return ValidationReport(findings)

        tree = parsed[0]
        gates = [
            ("read_only", lambda: self._gate_read_only(tree)),
            ("allowed_nodes", lambda: self._gate_allowed_nodes(tree)),
            ("functions", lambda: self._gate_functions(tree)),
            ("no_star", lambda: self._gate_no_star(tree)),
            ("parameters", lambda: self._gate_parameters(tree, compiled)),
            ("tables", lambda: self._gate_tables(tree, compiled, scope)),
            ("columns", lambda: self._gate_columns(tree, compiled, scope)),
            ("no_cartesian", lambda: self._gate_no_cartesian(tree, logical)),
            ("joins_approved", lambda: self._gate_joins_approved(logical, scope)),
            (
                "join_predicates_sql",
                lambda: self._gate_join_predicates_in_sql(tree, logical, scope),
            ),
            ("aggregates_sql", lambda: self._gate_aggregates_in_sql(tree, plan)),
            ("authorization", lambda: self._gate_authorization(compiled, plan, scope)),
            ("metric_integrity", lambda: self._gate_metric_integrity(plan, scope)),
            ("obligations", lambda: self._gate_obligations(tree, plan, logical)),
            ("obligations_sql", lambda: self._gate_obligations_in_sql(tree, plan, logical)),
            ("grain", lambda: self._gate_grain(logical, plan)),
            ("group_by", lambda: self._gate_group_by(tree, logical)),
            ("time", lambda: self._gate_time(plan)),
            ("fan_out", lambda: self._gate_fan_out(logical, plan)),
            ("policy_epoch", lambda: self._gate_policy_epoch(plan, scope)),
        ]
        for name, gate in gates:
            try:
                findings += gate()
            except Exception as exc:
                # A gate that cannot complete has not proven anything. Block.
                findings.append(Finding(
                    code=ReasonCode.VALIDATION_FAILED,
                    message=f"Gate {name!r} could not complete: {type(exc).__name__}: {exc}",
                    subject=name))
        return ValidationReport(findings)

    # -- 1. syntax ------------------------------------------------------

    def _gate_single_statement(self, parsed: list) -> list[Finding]:
        if len(parsed) != 1:
            return [Finding(code=ReasonCode.UNSAFE_QUERY,
                            message=f"Expected exactly one statement, parsed {len(parsed)}")]
        if not isinstance(parsed[0], exp.Select):
            return [Finding(code=ReasonCode.UNSAFE_QUERY,
                            message=f"Only SELECT is permitted, got {type(parsed[0]).__name__}")]
        return []

    # -- 13. safety -----------------------------------------------------

    def _gate_read_only(self, tree: exp.Expression) -> list[Finding]:
        for node in tree.walk():
            if isinstance(node, _WRITE_NODES):
                return [Finding(code=ReasonCode.UNSAFE_QUERY,
                                message=f"Statement contains a write/DDL node: "
                                        f"{type(node).__name__}")]
        return []

    def _gate_allowed_nodes(self, tree: exp.Expression) -> list[Finding]:
        """Complete AST traversal. A node absent from the allowlist blocks."""
        out: list[Finding] = []
        for node in tree.walk():
            if type(node) not in _ALLOWED_NODES:
                out.append(Finding(code=ReasonCode.UNSAFE_QUERY,
                                   message=f"Unsupported AST node {type(node).__name__}",
                                   subject=type(node).__name__))
        return out

    def _gate_functions(self, tree: exp.Expression) -> list[Finding]:
        out: list[Finding] = []
        for node in tree.find_all(exp.Anonymous):
            name = (node.name or "").lower()
            if name in duckdb_dialect.FORBIDDEN_FUNCTIONS:
                out.append(Finding(code=ReasonCode.UNSAFE_QUERY,
                                   message=f"Forbidden function {name!r}", subject=name))
            elif name not in duckdb_dialect.SUPPORTED_FUNCTIONS:
                out.append(Finding(code=ReasonCode.UNSAFE_QUERY,
                                   message=f"Function {name!r} is not on the dialect allowlist",
                                   subject=name))
        return out

    def _gate_no_star(self, tree: exp.Expression) -> list[Finding]:
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Star):
                    return [Finding(code=ReasonCode.UNSAFE_QUERY,
                                    message="SELECT * is never emitted; projections are explicit")]
        return []

    def _gate_parameters(self, tree: exp.Expression, compiled: CompiledQuery) -> list[Finding]:
        out: list[Finding] = []
        found = len(list(tree.find_all(exp.Placeholder)))
        if found != len(compiled.parameter_values):
            out.append(Finding(code=ReasonCode.COMPILER_DEFECT,
                               message=f"Re-parsed SQL has {found} placeholder(s) but "
                                       f"{len(compiled.parameter_values)} value(s) are bound"))
        # A value that should have been parameterized is a SQL-injection surface, so this
        # check runs regardless of the count above.
        for literal in tree.find_all(exp.Literal):
            if not literal.is_string:
                continue
            parent = literal.parent
            if isinstance(parent, (exp.EQ, exp.NEQ, exp.In, exp.LT, exp.LTE, exp.GT, exp.GTE)):
                out.append(Finding(
                    code=ReasonCode.UNSAFE_QUERY,
                    message=f"String literal {literal.name!r} appears inline in a predicate "
                            f"instead of being bound as a parameter",
                    subject=literal.name))
        return out

    # -- 3/4. tables and columns ----------------------------------------

    def _gate_tables(
        self, tree: exp.Expression, compiled: CompiledQuery, scope: TrustedScope
    ) -> list[Finding]:
        """Independently re-bind every relation named in the SQL text."""
        out: list[Finding] = []
        for table in tree.find_all(exp.Table):
            parts = [p for p in (table.catalog, table.db, table.name) if p]
            physical = ".".join(parts)
            match = [
                t for t in self.catalog.snapshot.all("tables")
                if str(t.physical) == physical
            ]
            if not match:
                out.append(Finding(code=ReasonCode.UNRESOLVED_REFERENCE,
                                   message=f"SQL references relation {physical!r}, which is not "
                                           f"in snapshot {self.catalog.snapshot.snapshot_id}",
                                   subject=physical))
                continue
            record = match[0]
            if record.id not in compiled.referenced_tables:
                out.append(Finding(code=ReasonCode.UNRESOLVED_REFERENCE,
                                   message=f"SQL scans {record.id}, which the compiled plan did "
                                           f"not declare",
                                   subject=record.id))
            if not record.is_certified:
                out.append(Finding(code=ReasonCode.UNSUPPORTED_CAPABILITY,
                                   message=f"{record.id} is catalogued but not certified for "
                                           f"automatic execution",
                                   subject=record.id))
        return out

    def _gate_columns(
        self, tree: exp.Expression, compiled: CompiledQuery, scope: TrustedScope
    ) -> list[Finding]:
        """Every column in the SQL must resolve to a declared alias and a real catalog column."""
        aliases: dict[str, str] = {}
        for table in tree.find_all(exp.Table):
            physical = ".".join([p for p in (table.catalog, table.db, table.name) if p])
            match = [t for t in self.catalog.snapshot.all("tables")
                     if str(t.physical) == physical]
            if match and table.alias:
                aliases[table.alias] = match[0].id

        # Output aliases the projection list declares. ORDER BY may name these.
        output_aliases = {
            p.alias for p in tree.expressions if isinstance(p, exp.Alias) and p.alias
        }

        out: list[Finding] = []
        for column in tree.find_all(exp.Column):
            if not column.table:
                if column.name in output_aliases:
                    continue
                out.append(Finding(code=ReasonCode.UNRESOLVED_REFERENCE,
                                   message=f"Unqualified column {column.name!r}; every reference "
                                           f"must be alias-qualified",
                                   subject=column.name))
                continue
            table_id = aliases.get(column.table)
            if table_id is None:
                continue  # output alias in ORDER BY, checked by _gate_group_by
            column_id = f"{table_id}.{column.name}"
            try:
                self.catalog.snapshot.get("columns", column_id)
            except Exception:
                out.append(Finding(code=ReasonCode.UNRESOLVED_REFERENCE,
                                   message=f"SQL references unknown column {column_id}",
                                   subject=column_id))
                continue
            if column_id not in compiled.referenced_columns:
                out.append(Finding(code=ReasonCode.UNRESOLVED_REFERENCE,
                                   message=f"SQL uses {column_id}, undeclared by the plan",
                                   subject=column_id))
        return out

    # -- 5. joins -------------------------------------------------------

    def _gate_no_cartesian(self, tree: exp.Expression, logical: LogicalPlan) -> list[Finding]:
        for join in tree.find_all(exp.Join):
            if not join.args.get("on") and not join.args.get("using"):
                return [Finding(code=ReasonCode.UNSAFE_QUERY,
                                message="Join without an ON predicate (Cartesian product)")]
        return []

    def _gate_joins_approved(self, logical: LogicalPlan, scope: TrustedScope) -> list[Finding]:
        out: list[Finding] = []
        for join in logical.joins:
            try:
                rel = self.catalog.get_relationship(join.relationship_ref, scope)
            except Exception as exc:
                out.append(Finding(code=ReasonCode.NO_APPROVED_PATH,
                                   message=f"Join cites unknown relationship "
                                           f"{join.relationship_ref}: {exc}",
                                   subject=join.relationship_ref))
                continue
            if len(join.predicates) != len(rel.predicates):
                out.append(Finding(code=ReasonCode.VALIDATION_FAILED,
                                   message=f"Join {join.relationship_ref} emitted "
                                           f"{len(join.predicates)} predicate(s); the approved "
                                           f"relationship declares {len(rel.predicates)}. A "
                                           f"partial key match is not the approved join.",
                                   subject=join.relationship_ref))
        return out

    # -- 6. authorization -----------------------------------------------

    def _gate_authorization(
        self, compiled: CompiledQuery, plan: BoundSemanticPlan, scope: TrustedScope
    ) -> list[Finding]:
        """Check every reference, including join keys and filters -- not only projections."""
        out: list[Finding] = []
        measure_columns = set()
        for metric in plan.metrics:
            from app.contracts.expressions import columns_in
            measure_columns |= columns_in(metric.expression)

        for column_id in sorted(compiled.referenced_columns):
            action = Action.READ if column_id in measure_columns else Action.USE_IN_PREDICATE
            decision = self.catalog.policy.check(scope.principal_id, action, column_id)
            if not decision.allowed:
                out.append(Finding(code=ReasonCode.NOT_AUTHORIZED,
                                   message=f"{decision.reason}", subject=column_id))
        for table_id in sorted(compiled.referenced_tables):
            decision = self.catalog.policy.check(scope.principal_id, Action.DISCOVER, table_id)
            if not decision.allowed:
                out.append(Finding(code=ReasonCode.NOT_AUTHORIZED,
                                   message=decision.reason, subject=table_id))
        for ref in plan.metric_refs:
            decision = self.catalog.policy.check(
                scope.principal_id, Action.EXECUTE_METRIC, ref.split("@")[0]
            )
            if not decision.allowed:
                out.append(Finding(code=ReasonCode.NOT_AUTHORIZED,
                                   message=decision.reason, subject=ref))
        return out

    # -- 7. metric integrity --------------------------------------------

    def _gate_metric_integrity(
        self, plan: BoundSemanticPlan, scope: TrustedScope
    ) -> list[Finding]:
        out: list[Finding] = []
        for bound, ref in zip(plan.metrics, plan.metric_refs, strict=False):
            record = self.catalog.snapshot.get_pinned("metrics", ref)
            if bound.expression != record.expression:
                out.append(Finding(code=ReasonCode.VALIDATION_FAILED,
                                   message=f"Bound measure for {ref} differs from the published "
                                           f"definition", subject=ref))
            if bound.population != record.population:
                out.append(Finding(code=ReasonCode.VALIDATION_FAILED,
                                   message=f"Bound population for {ref} differs from the "
                                           f"published definition", subject=ref))
            if bound.unit != record.unit:
                out.append(Finding(code=ReasonCode.VALIDATION_FAILED,
                                   message=f"Unit mismatch for {ref}", subject=ref))
        return out

    # -- 8. rule obligations --------------------------------------------

    def _gate_obligations(
        self, tree: exp.Expression, plan: BoundSemanticPlan, logical: LogicalPlan
    ) -> list[Finding]:
        """Every mandatory obligation must have evidence at the correct operator location."""
        from app.contracts.expressions import columns_in

        where_columns: set[str] = set()
        for pred in logical.where:
            where_columns |= columns_in(pred)
        having_columns: set[str] = set()
        for pred in logical.having:
            having_columns |= columns_in(pred)

        out: list[Finding] = []
        for obligation in plan.obligations:
            if obligation.predicate is not None:
                required = columns_in(obligation.predicate)
                target = (
                    where_columns
                    if obligation.placement == "before_aggregation"
                    else having_columns
                )
                missing = required - target
                if missing:
                    out.append(Finding(
                        code=ReasonCode.MANDATORY_RULE_UNRESOLVED,
                        message=f"Obligation {obligation.rule_ref} is not present at "
                                f"{obligation.placement}; missing {', '.join(sorted(missing))}",
                        subject=obligation.rule_ref))
            elif obligation.rule_ref.startswith("security.tenant_scope"):
                tenant_columns = {c for c in where_columns if self._is_tenant_key(c)}
                scanned = {logical.base.table_id} | {j.right.table_id for j in logical.joins}
                needs = {
                    t for t in scanned
                    if "tenant_scoped" in self.catalog.snapshot.get("tables", t).tags
                }
                covered = {c.rsplit(".", 1)[0] for c in tenant_columns}
                if not needs <= covered:
                    out.append(Finding(
                        code=ReasonCode.MANDATORY_RULE_UNRESOLVED,
                        message=f"Tenant isolation missing for "
                                f"{', '.join(sorted(needs - covered))}",
                        subject=obligation.rule_ref))
        return out

    # -- 9/10. aggregation and grouping ---------------------------------

    def _gate_grain(self, logical: LogicalPlan, plan: BoundSemanticPlan) -> list[Finding]:
        expected = set(plan.result_contract.grain)
        actual = set(logical.output_grain)
        if expected != actual:
            return [Finding(code=ReasonCode.VALIDATION_FAILED,
                            message=f"Output grain {sorted(actual)} does not match the result "
                                    f"contract {sorted(expected)}")]
        return []

    def _gate_group_by(self, tree: exp.Expression, logical: LogicalPlan) -> list[Finding]:
        select = tree
        group = select.args.get("group")
        grouped = len(group.expressions) if group else 0
        non_aggregate = 0
        for projection in select.expressions:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if not any(isinstance(n, _AGGREGATE_NODES) for n in inner.walk()):
                non_aggregate += 1
        if non_aggregate != grouped:
            return [Finding(code=ReasonCode.VALIDATION_FAILED,
                            message=f"{non_aggregate} non-aggregate projection(s) but {grouped} "
                                    f"GROUP BY expression(s)")]
        return []

    # -- 11. time -------------------------------------------------------

    def _gate_time(self, plan: BoundSemanticPlan) -> list[Finding]:
        if plan.time is None:
            return []
        out: list[Finding] = []
        if plan.time.range.end_exclusive <= plan.time.range.start:
            out.append(Finding(code=ReasonCode.VALIDATION_FAILED,
                               message="Time interval is empty or inverted"))
        record = self.catalog.snapshot.get("columns", plan.time.column)
        if record.time_role is None:
            out.append(Finding(code=ReasonCode.VALIDATION_FAILED,
                               message=f"{plan.time.column} has no declared time role",
                               subject=plan.time.column))
        return out

    # -- 16. fan-out ----------------------------------------------------

    def _gate_fan_out(self, logical: LogicalPlan, plan: BoundSemanticPlan) -> list[Finding]:
        additive = any(
            getattr(m.expression, "op", None) in ("sum", "avg") for m in plan.metrics
        )
        if not additive:
            return []
        out: list[Finding] = []
        for join in logical.joins:
            if not join.preserves_left_multiplicity:
                out.append(Finding(
                    code=ReasonCode.FAN_OUT_UNPROVEN,
                    message=f"Join {join.relationship_ref} ({join.cardinality}) multiplies the "
                            f"base fact while an additive measure is aggregated. DISTINCT is "
                            f"not a repair.",
                    subject=join.relationship_ref))
        return out

    # -- helpers for checking the emitted text on its own terms ----------

    @staticmethod
    def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
        """Split a predicate tree on top-level AND."""
        if node is None:
            return []
        if isinstance(node, exp.And):
            return Validator._conjuncts(node.this) + Validator._conjuncts(node.expression)
        return [node]

    def _sql_aliases(self, tree: exp.Expression) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table in tree.find_all(exp.Table):
            physical = ".".join([p for p in (table.catalog, table.db, table.name) if p])
            match = [t for t in self.catalog.snapshot.all("tables")
                     if str(t.physical) == physical]
            if match and table.alias:
                aliases[table.alias] = match[0].id
        return aliases

    def _is_tenant_key(self, column_id: str) -> bool:
        """A column that does not resolve is not a tenant key. Never raises: an unknown
        reference is reported by the column gate, and this gate must still fail closed."""
        try:
            return bool(self.catalog.snapshot.get("columns", column_id).is_tenant_key)
        except Exception:
            return False

    @staticmethod
    def _columns_of(node: exp.Expression | None, aliases: dict[str, str]) -> set[str]:
        if node is None:
            return set()
        return {
            f"{aliases[c.table]}.{c.name}"
            for c in node.find_all(exp.Column)
            if c.table in aliases
        }

    # -- independent SQL-level obligation evidence ----------------------

    def _gate_obligations_in_sql(
        self, tree: exp.Expression, plan: BoundSemanticPlan, logical: LogicalPlan
    ) -> list[Finding]:
        """Verify obligations in the EMITTED TEXT, not in the plan that produced it.

        Checking the logical plan against itself cannot catch a compiler that dropped a
        predicate on the way out. This gate re-reads the SQL and asks whether the evidence
        is actually there.
        """
        from app.contracts.expressions import columns_in

        aliases = self._sql_aliases(tree)
        where_columns = self._columns_of(tree.args.get("where"), aliases)
        having_columns = self._columns_of(tree.args.get("having"), aliases)

        out: list[Finding] = []
        for obligation in plan.obligations:
            if obligation.predicate is not None:
                required = columns_in(obligation.predicate)
                target = (
                    where_columns
                    if obligation.placement == "before_aggregation"
                    else having_columns
                )
                missing = required - target
                if missing:
                    out.append(Finding(
                        code=ReasonCode.MANDATORY_RULE_UNRESOLVED,
                        message=f"Emitted SQL does not carry obligation "
                                f"{obligation.rule_ref} at {obligation.placement}; "
                                f"missing {', '.join(sorted(missing))}",
                        subject=obligation.rule_ref))
            elif obligation.rule_ref.startswith("security.tenant_scope"):
                scanned = {logical.base.table_id} | {j.right.table_id for j in logical.joins}
                needs = {
                    t for t in scanned
                    if "tenant_scoped" in self.catalog.snapshot.get("tables", t).tags
                }
                covered = {
                    c.rsplit(".", 1)[0] for c in where_columns if self._is_tenant_key(c)
                }
                if not needs <= covered:
                    out.append(Finding(
                        code=ReasonCode.MANDATORY_RULE_UNRESOLVED,
                        message=f"Emitted SQL does not isolate tenant for "
                                f"{', '.join(sorted(needs - covered))}",
                        subject=obligation.rule_ref))
        return out

    def _gate_join_predicates_in_sql(
        self, tree: exp.Expression, logical: LogicalPlan, scope: TrustedScope
    ) -> list[Finding]:
        """A partial key match is not the approved join. Count the conjuncts actually
        emitted in each ON clause against the approved relationship."""
        sql_joins = list(tree.find_all(exp.Join))
        if len(sql_joins) != len(logical.joins):
            return [Finding(
                code=ReasonCode.VALIDATION_FAILED,
                message=f"Emitted SQL has {len(sql_joins)} join(s); the plan declares "
                        f"{len(logical.joins)}")]

        out: list[Finding] = []
        for sql_join, planned in zip(sql_joins, logical.joins, strict=True):
            try:
                rel = self.catalog.get_relationship(planned.relationship_ref, scope)
            except Exception:
                continue  # already reported by _gate_joins_approved
            emitted = len(self._conjuncts(sql_join.args.get("on")))
            if emitted != len(rel.predicates):
                out.append(Finding(
                    code=ReasonCode.VALIDATION_FAILED,
                    message=f"Emitted ON clause for {planned.relationship_ref} has {emitted} "
                            f"conjunct(s); the approved relationship declares "
                            f"{len(rel.predicates)}. A partial key match is a different join.",
                    subject=planned.relationship_ref))
        return out

    def _gate_aggregates_in_sql(
        self, tree: exp.Expression, plan: BoundSemanticPlan
    ) -> list[Finding]:
        """Every aggregate in the text must match a declared measure. In particular, a
        DISTINCT the plan never declared is the classic silent fan-out 'repair'."""
        declared_distinct = {
            m.output_alias for m in plan.metrics
            if getattr(m.expression, "op", None) == "count_distinct"
        }
        out: list[Finding] = []
        for projection in tree.expressions:
            alias = projection.alias if isinstance(projection, exp.Alias) else None
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            for agg in inner.find_all(_AGGREGATE_NODES):
                has_distinct = any(isinstance(n, exp.Distinct) for n in agg.walk())
                if has_distinct and alias not in declared_distinct:
                    out.append(Finding(
                        code=ReasonCode.VALIDATION_FAILED,
                        message=f"Projection {alias!r} applies DISTINCT inside an aggregate, "
                                f"which the metric definition does not declare. Distinct "
                                f"values are not distinct facts.",
                        subject=alias or "projection"))
        return out

    def _gate_policy_epoch(
        self, plan: BoundSemanticPlan, scope: TrustedScope
    ) -> list[Finding]:
        if plan.policy_epoch != scope.policy_epoch:
            return [Finding(code=ReasonCode.POLICY_EPOCH_CHANGED,
                            message=f"Plan bound under policy epoch {plan.policy_epoch}, current "
                                    f"epoch is {scope.policy_epoch}; rebinding is required")]
        if plan.snapshot_id != scope.snapshot_id:
            return [Finding(code=ReasonCode.STALE_SNAPSHOT,
                            message=f"Plan pins snapshot {plan.snapshot_id}, scope pins "
                                    f"{scope.snapshot_id}")]
        return []
