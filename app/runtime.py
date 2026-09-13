"""Application runtime assembly.

Builds every service once, indexes the search projection, and returns an object that can run
a question end to end. This is the composition root: it is the only place that decides which
provider and which model are in play.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.authorization.scope import establish_scope
from app.graph.build import build_graph, memory_checkpointer, sqlite_checkpointer
from app.graph.state import ArtifactStore, RequestContext
from app.llm.client import LlmClient
from app.llm.propose import PlanProposer
from app.llm.understand import QuestionUnderstander
from app.retrieval.embeddings import EmbeddingClient
from app.retrieval.providers.inmemory import InMemoryProvider
from app.retrieval.providers.opensearch import OpenSearchProvider
from app.retrieval.service import RetrievalService
from app.services import Services
from app.sql.gateway import DuckDbGateway
from ingestion.export import embed_cards, export_cards

load_dotenv()


@dataclass
class AppRuntime:
    services: Services
    provider: Any
    retrieval: RetrievalService
    understander: QuestionUnderstander
    proposer: PlanProposer
    gateway: DuckDbGateway
    graph: Any
    card_count: int
    provider_name: str
    llm_model: str | None
    scenario: str
    embeddings_enabled: bool

    @classmethod
    def build(
        cls,
        *,
        scenario: str = "golden",
        provider_name: str = "inmemory",
        use_llm: bool = True,
        use_embeddings: bool = True,
        checkpoint_path: str | Path | None = None,
    ) -> AppRuntime:
        from fixtures.build import db_path

        services = Services.build()

        embedder = None
        if use_embeddings:
            candidate = EmbeddingClient()
            if candidate.health():
                embedder = candidate

        cards = export_cards(services.catalog.snapshot)
        if embedder is not None:
            cards = embed_cards(cards, embedder)
        documents = [c.to_document() for c in cards]

        if provider_name == "opensearch":
            provider = OpenSearchProvider()
            provider.retire(services.catalog.snapshot.snapshot_id)
            provider.publish(documents, services.catalog.snapshot.snapshot_id)
            if not provider.is_ready(services.catalog.snapshot.snapshot_id, len(documents)):
                raise RuntimeError("OpenSearch index did not reach the expected document count")
        else:
            provider = InMemoryProvider()
            provider.index(documents)
        if embedder is not None:
            provider.set_embedder(embedder)

        llm = None
        if use_llm and os.getenv("OMNIROUTE_BASE_URL"):
            candidate = LlmClient()
            if candidate.health():
                llm = candidate

        # A checkpointer is required, not optional: clarification suspends through an
        # interrupt, which cannot work without one.
        checkpointer = (
            sqlite_checkpointer(checkpoint_path) if checkpoint_path else memory_checkpointer()
        )

        return cls(
            services=services,
            provider=provider,
            retrieval=RetrievalService(services.catalog, provider),
            understander=QuestionUnderstander(services.catalog, llm),
            proposer=PlanProposer(services.catalog, llm),
            gateway=DuckDbGateway(db_path(scenario)),
            graph=build_graph(checkpointer),
            card_count=len(cards),
            provider_name=provider_name,
            llm_model=llm.config.planner_model if llm else None,
            scenario=scenario,
            embeddings_enabled=embedder is not None,
        )

    def context(self, principal_id: str) -> RequestContext:
        scope = establish_scope(
            self.services.policy, principal_id, self.services.catalog.snapshot.snapshot_id
        )
        return RequestContext(
            scope=scope,
            services=self.services,
            gateway=self.gateway,
            retrieval=self.retrieval,
            understander=self.understander,
            proposer=self.proposer,
            artifacts=ArtifactStore(),
        )

    async def ask(
        self,
        question: str,
        principal_id: str = "analyst_full",
        thread_id: str | None = None,
        *,
        callbacks: Sequence[Any] = (),
        trace_tags: Sequence[str] = (),
        trace_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = f"req_{uuid.uuid4().hex[:8]}"
        thread = thread_id or request_id
        state = {
            "request_id": request_id,
            "thread_id": thread,
            "question": question,
            "plan_revision": 1,
            "retrieval_rounds": 0,
            "repair_attempts": 0,
            "clarified_slots": [],
            "notes": [],
            "reason_codes": [],
        }
        context = self.context(principal_id)
        return await self.graph.ainvoke(
            state,
            self._config(
                thread,
                run_name="texttosql.ask",
                request_id=request_id,
                principal_id=principal_id,
                context=context,
                callbacks=callbacks,
                trace_tags=trace_tags,
                trace_metadata=trace_metadata,
            ),
            context=context,
        )

    async def resume(
        self,
        answer: Any,
        principal_id: str = "analyst_full",
        *,
        thread_id: str,
        callbacks: Sequence[Any] = (),
        trace_tags: Sequence[str] = (),
        trace_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Answer a pending clarification and continue.

        A fresh RequestContext is built, so authorization is re-derived from the authenticated
        principal rather than inherited from the suspended request. The graph re-enters
        `admit` on resume for the same reason: a grant may have been revoked while the request
        was waiting.
        """
        from langgraph.types import Command

        snapshot = self.graph.get_state(self._config(thread_id))
        values = snapshot.values if snapshot is not None else {}
        request_id = str((values or {}).get("request_id") or thread_id)
        context = self.context(principal_id)
        return await self.graph.ainvoke(
            Command(resume=answer),
            self._config(
                thread_id,
                run_name="texttosql.resume",
                request_id=request_id,
                principal_id=principal_id,
                context=context,
                callbacks=callbacks,
                trace_tags=trace_tags,
                trace_metadata=trace_metadata,
            ),
            context=context,
        )

    def pending_clarification(self, thread_id: str) -> dict[str, Any] | None:
        """The interrupt payload waiting on this thread, if any."""
        snapshot = self.graph.get_state(self._config(thread_id))
        for task in snapshot.tasks:
            for pending in task.interrupts:
                return pending.value
        return None

    def _config(
        self,
        thread_id: str,
        *,
        run_name: str | None = None,
        request_id: str | None = None,
        principal_id: str | None = None,
        context: RequestContext | None = None,
        callbacks: Sequence[Any] = (),
        trace_tags: Sequence[str] = (),
        trace_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 40,
        }
        if run_name is None:
            return config

        mode = "llm" if self.llm_model else "fallback"
        metadata: dict[str, Any] = _safe_trace_metadata(trace_metadata or {})
        # Authoritative runtime fields win over caller-provided presentation metadata.
        metadata.update(
            {
                "request_id": request_id,
                "graph_thread_id": thread_id,
                "principal": principal_id,
                "scenario": self.scenario,
                "snapshot": context.scope.snapshot_id if context is not None else None,
                "policy_epoch": context.scope.policy_epoch if context is not None else None,
                "retrieval_provider": self.provider_name,
                "model": self.llm_model or "deterministic-fallback",
            }
        )
        config.update(
            {
                "run_name": run_name,
                "tags": _unique_strings(
                    (
                        "texttosql",
                        f"scenario:{self.scenario}",
                        f"provider:{self.provider_name}",
                        f"mode:{mode}",
                        *trace_tags,
                    )
                ),
                "metadata": {key: value for key, value in metadata.items() if value is not None},
            }
        )
        if callbacks:
            config["callbacks"] = list(callbacks)
        return config


_SENSITIVE_TRACE_KEY_PARTS = (
    "apikey",
    "credential",
    "password",
    "parameter",
    "secret",
    "token",
)


def _safe_trace_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, str | int | float | bool | None]:
    """Keep trace metadata scalar and reject keys likely to carry secrets."""

    safe: dict[str, str | int | float | bool | None] = {}
    for raw_key, value in metadata.items():
        key = str(raw_key)
        normalized = "".join(character for character in key.lower() if character.isalnum())
        if any(part in normalized for part in _SENSITIVE_TRACE_KEY_PARTS):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value
    return safe


def _unique_strings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
