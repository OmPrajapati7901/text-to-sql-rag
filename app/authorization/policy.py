"""Deterministic policy decision point.

Separate actions, because a grant is not a blanket permission: DISCOVER (may see it exists),
READ (may project it), USE_IN_PREDICATE (may filter/join/group/order by it),
EXECUTE_METRIC (may run a sealed aggregate), DISCLOSE (may receive it in a response).

Explicit denies always win. Default is deny. A policy outage fails closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.contracts.errors import AuthorizationError, ReasonCode
from app.contracts.ids import table_id_of
from app.contracts.scope import Action

# Denying a broader action denies the narrower ones it would otherwise leak through.
# DISCOVER is the broadest: not knowing an object exists denies every use of it.
DENY_CASCADE: dict[Action, tuple[Action, ...]] = {
    Action.READ: (Action.DISCOVER,),
    Action.USE_IN_PREDICATE: (Action.DISCOVER, Action.READ),
    Action.EXECUTE_METRIC: (Action.DISCOVER,),
    Action.DISCLOSE: (Action.DISCOVER, Action.READ),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    policy_epoch: int

    def require(self, subject: str) -> None:
        if not self.allowed:
            raise AuthorizationError(ReasonCode.NOT_AUTHORIZED, self.reason, subject=subject)


@dataclass(frozen=True)
class PrincipalPolicy:
    principal_id: str
    tenant_id: str
    purpose: str
    domains: frozenset[str]
    access_scopes: frozenset[str]
    grants: dict[Action, frozenset[str]]
    denies: dict[Action, frozenset[str]]

    def _matches(self, patterns: frozenset[str], resource: str) -> bool:
        if resource in patterns:
            return True
        # A grant on a table implies nothing about its columns; but a grant written as a
        # prefix wildcard is an explicit, reviewed choice by the policy author.
        return any(
            p.endswith(".*") and (resource == p[:-2] or resource.startswith(p[:-1]))
            for p in patterns
        )


class PolicyEngine:
    """Loads a reviewed policy file. No LLM input reaches this class."""

    def __init__(self, principals: dict[str, PrincipalPolicy], policy_epoch: int) -> None:
        self._principals = principals
        self.policy_epoch = policy_epoch

    @classmethod
    def from_file(cls, path: Path) -> PolicyEngine:
        raw = json.loads(Path(path).read_text())
        principals: dict[str, PrincipalPolicy] = {}
        for entry in raw["principals"]:
            principals[entry["principal_id"]] = PrincipalPolicy(
                principal_id=entry["principal_id"],
                tenant_id=entry["tenant_id"],
                purpose=entry.get("purpose", "analytics"),
                domains=frozenset(entry.get("domains", [])),
                access_scopes=frozenset(entry.get("access_scopes", [])),
                grants={
                    Action(a): frozenset(v) for a, v in entry.get("grants", {}).items()
                },
                denies={
                    Action(a): frozenset(v) for a, v in entry.get("denies", {}).items()
                },
            )
        return cls(principals, int(raw["policy_epoch"]))

    def principal(self, principal_id: str) -> PrincipalPolicy:
        try:
            return self._principals[principal_id]
        except KeyError:
            raise AuthorizationError(
                ReasonCode.NOT_AUTHORIZED, "Unknown principal", subject=principal_id
            ) from None

    def check(self, principal_id: str, action: Action, resource: str) -> Decision:
        policy = self.principal(principal_id)

        if policy._matches(policy.denies.get(action, frozenset()), resource):
            return Decision(False, f"Explicit deny on {action} for {resource}", self.policy_epoch)

        # A deny on a broader action cascades. Masking a projected value does not prevent
        # inference: if a principal may not READ a column, it may not filter, join, group or
        # sort by it either. Only an exact-resource grant on the narrower action -- never a
        # wildcard -- can override, so a broad grant cannot silently reopen a specific deny.
        for broader in DENY_CASCADE.get(action, ()):
            if policy._matches(policy.denies.get(broader, frozenset()), resource):
                if resource in policy.grants.get(action, frozenset()):
                    return Decision(
                        True,
                        f"exact {action} grant overrides cascaded {broader} deny",
                        self.policy_epoch,
                    )
                return Decision(
                    False,
                    f"Deny on {broader} for {resource} cascades to {action}",
                    self.policy_epoch,
                )

        if policy._matches(policy.grants.get(action, frozenset()), resource):
            return Decision(True, "granted", self.policy_epoch)

        # USE_IN_PREDICATE is narrower than READ, so an explicit READ grant implies it.
        if action is Action.USE_IN_PREDICATE and policy._matches(
            policy.grants.get(Action.READ, frozenset()), resource
        ):
            return Decision(True, "implied by read grant", self.policy_epoch)

        return Decision(False, f"No {action} grant for {resource}", self.policy_epoch)

    def check_all(
        self, principal_id: str, action: Action, resources: frozenset[str]
    ) -> list[str]:
        """Returns the denied subset. Callers must treat a non-empty result as fatal."""
        return sorted(r for r in resources if not self.check(principal_id, action, r).allowed)

    def filter_discoverable(self, principal_id: str, resources: frozenset[str]) -> frozenset[str]:
        """Discovery filtering happens inside the trusted boundary, before anything reaches a
        reranker or a model."""
        return frozenset(
            r for r in resources if self.check(principal_id, Action.DISCOVER, r).allowed
        )

    def denied_object_ids(self, principal_id: str) -> frozenset[str]:
        policy = self.principal(principal_id)
        denied: set[str] = set()
        for action in (Action.DISCOVER, Action.READ):
            denied |= policy.denies.get(action, frozenset())
        return frozenset(denied)

    def column_parent_readable(self, principal_id: str, column_id: str) -> bool:
        """A column is only usable if its own grant holds. This helper exists to make the
        'table grant does not imply column access' rule explicit at call sites."""
        return self.check(principal_id, Action.READ, column_id).allowed and self.check(
            principal_id, Action.DISCOVER, table_id_of(column_id)
        ).allowed
