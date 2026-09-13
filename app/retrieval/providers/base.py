"""The provider-neutral retrieval interface.

Two rules every adapter must honour:

1. It enforces the requested discovery restriction faithfully. If it cannot represent the
   restriction, it raises POLICY_UNREPRESENTABLE. It never silently drops a filter.
2. It reports service completion honestly. A timeout or shard failure is PARTIAL/TIMED_OUT,
   never a short COMPLETE list.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.contracts.retrieval import ProviderCapabilities, SearchRequest, SearchResponse
from app.contracts.scope import TrustedScope


@runtime_checkable
class RetrievalProvider(Protocol):
    """Ranked discovery of relevant objects. Never a source of truth or authorization."""

    def capabilities(self) -> ProviderCapabilities: ...

    async def search(
        self, request: SearchRequest, scope: TrustedScope
    ) -> SearchResponse: ...

    async def health(self) -> bool: ...


@runtime_checkable
class ProviderPublisher(Protocol):
    """Ingestion side: submit a bundle, report readiness, retire old releases.

    Bulk indexing and a managed Knowledge Base sync do not have identical synchronous
    semantics, so readiness is always asked for explicitly rather than assumed on return.
    """

    async def publish(self, cards: list[dict], snapshot_id: str) -> dict: ...

    async def is_ready(self, snapshot_id: str, expected_count: int) -> bool: ...

    async def retire(self, snapshot_id: str) -> None: ...
