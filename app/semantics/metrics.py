"""Metric resolution and dependency closure.

A metric determines its own source tables. Resolving `finance.revenue@7` yields its measure
column, population columns, time column and required rules deterministically — no retrieval
involved, and no top-K pruning applied to a mandatory dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.catalog.service import CatalogService
from app.contracts.errors import CatalogError, ReasonCode
from app.contracts.expressions import columns_in
from app.contracts.ids import table_id_of
from app.contracts.scope import Action, TrustedScope
from app.contracts.semantics import MetricRecord


@dataclass(frozen=True)
class MetricClosure:
    """Everything a metric needs to be computed correctly."""

    metric: MetricRecord
    measure_columns: frozenset[str]
    population_columns: frozenset[str]
    time_column: str
    tables: frozenset[str]
    required_rule_refs: tuple[str, ...]

    @property
    def all_columns(self) -> frozenset[str]:
        return self.measure_columns | self.population_columns | {self.time_column}


@dataclass
class MetricService:
    catalog: CatalogService
    _cache: dict[str, MetricClosure] = field(default_factory=dict, repr=False)

    def resolve(self, ref: str, scope: TrustedScope) -> MetricClosure:
        """Resolve a pinned metric and its full dependency closure.

        Authorization is checked on the metric (EXECUTE_METRIC) and on every column the
        implementation touches. A user permitted to see aggregate Revenue is not thereby
        permitted to read the underlying amount column, so both are verified.
        """
        metric = self.catalog.get_metric(ref, scope)
        cache_key = f"{scope.principal_id}:{scope.policy_epoch}:{ref}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        measure_cols = columns_in(metric.expression)
        population_cols = columns_in(metric.population)
        time_col = metric.time.time_column

        all_cols = measure_cols | population_cols | {time_col}
        for column_id in sorted(all_cols):
            self.catalog.get_column(column_id, scope)  # exists + discoverable

        # Every dependency column must be usable in a predicate; the measure must be readable.
        self.catalog.require_columns_usable(
            frozenset(population_cols | {time_col}), scope, Action.USE_IN_PREDICATE
        )
        self.catalog.require_columns_usable(frozenset(measure_cols), scope, Action.READ)

        tables = frozenset(table_id_of(c) for c in all_cols)
        if metric.base_table not in tables:
            tables = tables | {metric.base_table}

        closure = MetricClosure(
            metric=metric,
            measure_columns=frozenset(measure_cols),
            population_columns=frozenset(population_cols),
            time_column=time_col,
            tables=tables,
            required_rule_refs=metric.required_rule_refs,
        )
        self._cache[cache_key] = closure
        return closure

    def resolve_dimension_columns(
        self, dimension_ref: str, scope: TrustedScope
    ) -> tuple[str, str | None]:
        """Returns (attribute column ID, required relationship ref)."""
        dim = self.catalog.get_dimension(dimension_ref, scope)
        self.catalog.get_column(dim.column, scope)
        self.catalog.require_columns_usable(
            frozenset({dim.column}), scope, Action.READ
        )
        return dim.column, dim.required_relationship_ref

    def check_dimension_allowed(
        self, metric: MetricRecord, dimension_id: str
    ) -> None:
        """A metric declares which dimensions it supports. Slicing by an unsupported
        dimension is blocked rather than silently attempted."""
        if metric.allowed_dimensions and dimension_id not in metric.allowed_dimensions:
            raise CatalogError(
                ReasonCode.UNSUPPORTED_CAPABILITY,
                f"Metric {metric.ref} does not support dimension {dimension_id!r}; "
                f"approved dimensions are {', '.join(metric.allowed_dimensions)}",
                subject=dimension_id,
            )
