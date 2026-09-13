"""Dependency wiring.

Services are constructed once and injected. Live clients and credentials live here, never in
graph state and never in a checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.authorization.policy import PolicyEngine
from app.catalog.service import CatalogService, CatalogSnapshot
from app.planning.binder import Binder
from app.planning.lowering import Lowering
from app.relationships.planner import JoinPlanner
from app.semantics.metrics import MetricService
from app.semantics.rules import RuleEngine
from app.sql.compiler import SqlCompiler
from app.sql.validators.suite import Validator

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = ROOT / "catalog" / "snapshots" / "local-001"
DEFAULT_POLICY = ROOT / "catalog" / "policy.json"


@dataclass
class Services:
    policy: PolicyEngine
    catalog: CatalogService
    metrics: MetricService
    rules: RuleEngine
    joins: JoinPlanner
    binder: Binder
    lowering: Lowering
    compiler: SqlCompiler
    validator: Validator

    @property
    def dialect(self) -> str:
        return self.catalog.snapshot.dialect

    @classmethod
    def build(
        cls,
        snapshot_path: Path = DEFAULT_SNAPSHOT,
        policy_path: Path = DEFAULT_POLICY,
    ) -> Services:
        policy = PolicyEngine.from_file(policy_path)
        catalog = CatalogService(CatalogSnapshot(snapshot_path), policy)
        metrics = MetricService(catalog)
        rules = RuleEngine(catalog)
        joins = JoinPlanner(catalog)
        return cls(
            policy=policy,
            catalog=catalog,
            metrics=metrics,
            rules=rules,
            joins=joins,
            binder=Binder(catalog, metrics, rules, joins),
            lowering=Lowering(catalog),
            compiler=SqlCompiler(catalog),
            validator=Validator(catalog),
        )
