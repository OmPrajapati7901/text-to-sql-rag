"""Local Chainlit interface for the governed text-to-SQL runtime.

Run with: ``uv run chainlit run app/chainlit_ui.py -w``

Only presentation state and an asyncio lock live in the Chainlit user session. Authority,
credentials, SQL parameter values, and service clients remain server-side in AppRuntime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("LANGSMITH_PROJECT", "texttosql-chainlit")

import chainlit as cl  # noqa: E402

from app.contracts.clarification import ClarificationRequest  # noqa: E402
from app.results.render import AnswerEnvelope  # noqa: E402
from app.runtime import AppRuntime  # noqa: E402

logger = logging.getLogger(__name__)

_RUNTIME: AppRuntime | None = None
_RUNTIME_SIGNATURE: tuple[Any, ...] | None = None
_RUNTIME_LOCK = threading.Lock()
_SESSION_LOCK_KEY = "texttosql_request_lock"


class UiConfig:
    """Immutable-by-convention server configuration.

    This intentionally avoids ``dataclass``: Chainlit loads an application module without
    first registering it in ``sys.modules``, which triggers a Python 3.13 dataclasses bug.
    """

    __slots__ = (
        "max_rows",
        "principal",
        "retrieval_provider",
        "scenario",
        "show_sql",
        "use_embeddings",
        "use_llm",
    )

    def __init__(
        self,
        principal: str = "analyst_full",
        scenario: str = "golden",
        retrieval_provider: str = "inmemory",
        use_llm: bool = True,
        use_embeddings: bool = True,
        show_sql: bool = True,
        max_rows: int = 100,
    ) -> None:
        self.principal = principal
        self.scenario = scenario
        self.retrieval_provider = retrieval_provider
        self.use_llm = use_llm
        self.use_embeddings = use_embeddings
        self.show_sql = show_sql
        self.max_rows = max_rows

    @classmethod
    def from_env(cls) -> UiConfig:
        provider = os.getenv("TTSQL_RETRIEVAL_PROVIDER", "inmemory").strip().lower()
        if provider not in {"inmemory", "opensearch"}:
            raise ValueError("TTSQL_RETRIEVAL_PROVIDER must be inmemory or opensearch")
        max_rows = int(os.getenv("TTSQL_CHAINLIT_MAX_ROWS", "100"))
        if max_rows < 1:
            raise ValueError("TTSQL_CHAINLIT_MAX_ROWS must be at least 1")
        return cls(
            principal=os.getenv("TTSQL_CHAINLIT_PRINCIPAL", "analyst_full").strip(),
            scenario=os.getenv("TTSQL_SCENARIO", "golden").strip(),
            retrieval_provider=provider,
            use_llm=_env_bool("TTSQL_USE_LLM", default=True),
            use_embeddings=_env_bool("TTSQL_USE_EMBEDDINGS", default=True),
            show_sql=_env_bool("TTSQL_CHAINLIT_SHOW_SQL", default=True),
            max_rows=max_rows,
        )

    @property
    def runtime_signature(self) -> tuple[Any, ...]:
        return (
            self.scenario,
            self.retrieval_provider,
            self.use_llm,
            self.use_embeddings,
        )


class QueryOutcome:
    __slots__ = ("clarification_timed_out", "graph_thread_id", "state")

    def __init__(
        self,
        state: dict[str, Any] | None,
        graph_thread_id: str,
        clarification_timed_out: bool = False,
    ) -> None:
        self.state = state
        self.graph_thread_id = graph_thread_id
        self.clarification_timed_out = clarification_timed_out


@cl.set_starters
async def starters() -> list[cl.Starter]:
    return [
        cl.Starter(
            label="Revenue trends",
            message="Show monthly revenue by customer region in Q2 2026",
        ),
        cl.Starter(
            label="Order counts",
            message="Show monthly order count in Q2 2026",
        ),
        cl.Starter(
            label="Metric definition",
            message="What does Revenue mean?",
        ),
    ]


@cl.on_chat_start
async def on_chat_start() -> None:
    try:
        config = UiConfig.from_env()
        runtime = await get_runtime(config)
        cl.user_session.set(_SESSION_LOCK_KEY, asyncio.Lock())
        await cl.Message(content=_runtime_status(runtime, config)).send()
    except Exception:
        logger.exception("Chainlit runtime initialization failed")
        await cl.Message(
            content="The text-to-SQL runtime could not start. Check the server configuration "
            "and logs, then reload this chat."
        ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    question = (message.content or "").strip()
    if not question:
        await cl.Message(
            content="Please enter a question about the available business data."
        ).send()
        return
    if _is_greeting(question):
        await cl.Message(
            content="Hello! Ask me about revenue trends, order counts, metric definitions, "
            "or which governed data is available."
        ).send()
        return

    lock = cl.user_session.get(_SESSION_LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        cl.user_session.set(_SESSION_LOCK_KEY, lock)

    async with lock:
        try:
            config = UiConfig.from_env()
            runtime = await get_runtime(config)
            callback = cl.LangchainCallbackHandler()
            session_id = str(cl.user_session.get("id") or "unknown")
            outcome = await execute_question(
                runtime,
                question,
                config=config,
                session_id=session_id,
                callback=callback,
                ask_clarification=_ask_clarification,
            )
            if outcome.clarification_timed_out:
                await cl.Message(
                    content="The clarification timed out, so the query was left unanswered. "
                    "Submit the question again when you are ready."
                ).send()
                return
            if outcome.state is None or outcome.state.get("answer") is None:
                raise RuntimeError("Graph completed without an answer envelope")
            answer = AnswerEnvelope.model_validate(outcome.state["answer"])
            await cl.Message(
                content=answer.to_markdown(max_rows=config.max_rows),
                elements=_result_elements(answer, outcome.state, config),
            ).send()
        except Exception:
            logger.exception("Chainlit query failed")
            await cl.Message(
                content="The query could not be completed because of an unexpected server "
                "error. No partial answer was returned."
            ).send()


async def get_runtime(config: UiConfig) -> AppRuntime:
    """Return the single lazily constructed runtime shared by this server process."""

    return await asyncio.to_thread(_get_or_build_runtime, config)


def _get_or_build_runtime(config: UiConfig) -> AppRuntime:
    global _RUNTIME, _RUNTIME_SIGNATURE  # noqa: PLW0603

    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = AppRuntime.build(
                scenario=config.scenario,
                provider_name=config.retrieval_provider,
                use_llm=config.use_llm,
                use_embeddings=config.use_embeddings,
            )
            _RUNTIME_SIGNATURE = config.runtime_signature
        elif config.runtime_signature != _RUNTIME_SIGNATURE:
            raise RuntimeError(
                "Runtime environment changed after startup; restart Chainlit to apply it"
            )
        return _RUNTIME


async def execute_question(
    runtime: AppRuntime,
    question: str,
    *,
    config: UiConfig,
    session_id: str,
    callback: Any,
    ask_clarification: Callable[[ClarificationRequest], Awaitable[str | None]],
) -> QueryOutcome:
    """Run one independent graph thread, resuming that thread for each clarification."""

    graph_thread_id = f"chainlit_{uuid.uuid4().hex}"
    trace_metadata = {
        "interface": "chainlit",
        "chainlit_session_id": session_id,
    }
    invoke_options = {
        "callbacks": (callback,),
        "trace_tags": ("interface:chainlit",),
        "trace_metadata": trace_metadata,
    }
    state = await runtime.ask(
        question,
        config.principal,
        thread_id=graph_thread_id,
        **invoke_options,
    )

    while (pending := runtime.pending_clarification(graph_thread_id)) is not None:
        request = ClarificationRequest.model_validate(pending)
        selected = await ask_clarification(request)
        if selected is None:
            return QueryOutcome(None, graph_thread_id, clarification_timed_out=True)
        state = await runtime.resume(
            {"slot": request.slot, "value": selected},
            config.principal,
            thread_id=graph_thread_id,
            **invoke_options,
        )

    return QueryOutcome(state, graph_thread_id)


async def _ask_clarification(request: ClarificationRequest) -> str | None:
    actions = [
        cl.Action(
            name=f"clarify_{index}",
            payload={"value": option.value},
            label=option.label,
        )
        for index, option in enumerate(request.options)
    ]
    response = await cl.AskActionMessage(
        content=_clarification_markdown(request),
        actions=actions,
        timeout=180,
        raise_on_timeout=False,
    ).send()
    return _selected_action_value(response)


def _clarification_markdown(request: ClarificationRequest) -> str:
    lines = [request.question]
    if request.reason:
        lines.extend(("", f"_Why this matters: {request.reason}_"))
    effects = [
        f"- **{option.label}:** {option.effect}" for option in request.options if option.effect
    ]
    if effects:
        lines.extend(("", *effects))
    return "\n".join(lines)


def _selected_action_value(response: Any) -> str | None:
    if response is None:
        return None
    payload: Any
    if isinstance(response, Mapping):
        payload = response.get("payload", {})
    else:
        payload = getattr(response, "payload", {})
    if isinstance(payload, Mapping) and isinstance(payload.get("value"), str):
        return payload["value"]
    return None


def _result_elements(
    answer: AnswerEnvelope,
    state: Mapping[str, Any],
    config: UiConfig,
) -> list[cl.Element]:
    elements: list[cl.Element] = []
    # Graph state contains the placeholder SQL only. Bound values live in ArtifactStore and
    # are deliberately unavailable to this renderer and to persisted/traced graph state.
    sql = state.get("sql")
    if config.show_sql and isinstance(sql, str) and sql:
        elements.append(
            cl.Text(
                name="Generated SQL (placeholders only)",
                content=sql,
                display="side",
                language="sql",
            )
        )
    if answer.rows:
        request_id = str(state.get("request_id") or "result")
        elements.append(
            cl.File(
                name=f"{request_id}.csv",
                content=answer.to_csv_bytes(),
                display="inline",
            )
        )
    return elements


def _runtime_status(runtime: AppRuntime, config: UiConfig) -> str:
    model = runtime.llm_model or "deterministic fallback"
    embeddings = "enabled" if runtime.embeddings_enabled else "disabled/fallback"
    return "\n".join(
        (
            "The governed text-to-SQL runtime is ready.",
            "",
            f"- Scenario: `{config.scenario}`",
            f"- Retrieval: `{runtime.provider_name}` ({embeddings} embeddings)",
            f"- Planner: `{model}`",
            f"- Tracing: {_tracing_status()}",
            f"- Display limit: {config.max_rows} rows (CSV downloads remain complete)",
        )
    )


def _tracing_status() -> str:
    enabled = _env_bool("LANGSMITH_TRACING", default=False) or _env_bool(
        "LANGCHAIN_TRACING_V2", default=False
    )
    if not enabled:
        return "disabled"
    return "enabled" if os.getenv("LANGSMITH_API_KEY") else "requested; API key not configured"


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _is_greeting(text: str) -> bool:
    return text.strip().lower() in {"hi", "hello", "hey", "hi!", "hello!", "hey!"}
