from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app import chainlit_ui as ui
from app.contracts.clarification import ClarificationRequest

pytestmark = pytest.mark.unit


ANSWER = {
    "status": "answered",
    "columns": ["month", "region", "revenue_usd"],
    "rows": [["2026-04-01T00:00:00+00:00", "North", "200.00"]],
    "unit": "USD",
    "metric_versions": {"finance.revenue": "v7"},
    "applied_rules": ["security.tenant_scope@5"],
    "join_paths": ["commerce.orders_customers_asof@2"],
    "time_interpretation": "Q2 2026, by month",
    "evidence_status": "verified",
    "notes": [],
    "reason_codes": [],
}


class FakeRuntime:
    def __init__(
        self,
        *,
        state: dict[str, Any] | None = None,
        clarification: dict[str, Any] | None = None,
        delay: float = 0,
    ) -> None:
        self.state = state or {"request_id": "req_ui", "answer": ANSWER}
        self.clarification = clarification
        self.delay = delay
        self.ask_calls: list[tuple[Any, ...]] = []
        self.resume_calls: list[tuple[Any, ...]] = []
        self._resumed = False
        self.active = 0
        self.max_active = 0

    async def ask(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.ask_calls.append((args, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.active -= 1
        return self.state

    async def resume(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.resume_calls.append((args, kwargs))
        self._resumed = True
        return {"request_id": "req_ui", "answer": ANSWER}

    def pending_clarification(self, _thread_id: str) -> dict[str, Any] | None:
        return None if self._resumed else self.clarification


class FakeUserSession:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {"id": "session-ui"}

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class SentMessage:
    sent: ClassVar[list[SentMessage]] = []

    def __init__(self, *, content: str, elements: list[Any] | None = None) -> None:
        self.content = content
        self.elements = elements or []

    async def send(self) -> SentMessage:
        self.sent.append(self)
        return self


class FakeElement:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


@pytest.fixture
def ui_harness(monkeypatch: pytest.MonkeyPatch) -> FakeUserSession:
    session = FakeUserSession()
    SentMessage.sent = []
    monkeypatch.setattr(ui.cl, "user_session", session)
    monkeypatch.setattr(ui.cl, "Message", SentMessage)
    monkeypatch.setattr(ui.cl, "Text", FakeElement)
    monkeypatch.setattr(ui.cl, "File", FakeElement)
    monkeypatch.setattr(ui.cl, "LangchainCallbackHandler", object)
    monkeypatch.setattr(
        ui.UiConfig,
        "from_env",
        classmethod(lambda _cls: ui.UiConfig(use_llm=False, use_embeddings=False)),
    )
    return session


def clarification_payload() -> dict[str, Any]:
    return {
        "slot": "time_range",
        "question": "Which period should this cover?",
        "reason": "The period changes the answer.",
        "options": [
            {"value": "q2_2026", "label": "Q2 2026", "effect": "April through June"},
            {"value": "q1_2026", "label": "Q1 2026", "effect": "January through March"},
        ],
    }


async def test_execute_question_resumes_selected_clarification_on_same_thread() -> None:
    runtime = FakeRuntime(clarification=clarification_payload())
    seen: list[ClarificationRequest] = []

    async def choose(request: ClarificationRequest) -> str:
        seen.append(request)
        return "q2_2026"

    outcome = await ui.execute_question(
        runtime,
        "revenue",
        config=ui.UiConfig(),
        session_id="session-1",
        callback=object(),
        ask_clarification=choose,
    )

    assert not outcome.clarification_timed_out
    assert seen[0].question == "Which period should this cover?"
    ask_kwargs = runtime.ask_calls[0][1]
    resume_args, resume_kwargs = runtime.resume_calls[0]
    assert ask_kwargs["thread_id"] == resume_kwargs["thread_id"]
    assert resume_args[0] == {"slot": "time_range", "value": "q2_2026"}
    assert resume_kwargs["trace_metadata"]["chainlit_session_id"] == "session-1"
    assert resume_kwargs["trace_tags"] == ("interface:chainlit",)


async def test_execute_question_leaves_query_unanswered_on_clarification_timeout() -> None:
    runtime = FakeRuntime(clarification=clarification_payload())

    async def timeout(_request: ClarificationRequest) -> None:
        return None

    outcome = await ui.execute_question(
        runtime,
        "revenue",
        config=ui.UiConfig(),
        session_id="session-1",
        callback=object(),
        ask_clarification=timeout,
    )

    assert outcome.clarification_timed_out
    assert outcome.state is None
    assert runtime.resume_calls == []


async def test_each_question_gets_a_unique_graph_thread() -> None:
    runtime = FakeRuntime()

    async def unused(_request: ClarificationRequest) -> None:
        return None

    first = await ui.execute_question(
        runtime,
        "first",
        config=ui.UiConfig(),
        session_id="session-1",
        callback=object(),
        ask_clarification=unused,
    )
    second = await ui.execute_question(
        runtime,
        "second",
        config=ui.UiConfig(),
        session_id="session-1",
        callback=object(),
        ask_clarification=unused,
    )

    assert first.graph_thread_id != second.graph_thread_id
    assert first.graph_thread_id.startswith("chainlit_")


async def test_on_message_renders_answer_sql_csv_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
    ui_harness: FakeUserSession,
) -> None:
    runtime = FakeRuntime(
        state={
            "request_id": "req_normal",
            "sql": "SELECT ? AS value",
            "answer": ANSWER,
        }
    )

    async def get_runtime(_config: ui.UiConfig) -> FakeRuntime:
        return runtime

    monkeypatch.setattr(ui, "get_runtime", get_runtime)
    await ui.on_message(SimpleNamespace(content="Show revenue"))

    message = SentMessage.sent[-1]
    assert "finance.revenue (v7)" in message.content
    assert "security.tenant_scope@5" in message.content
    assert {element.name for element in message.elements} == {
        "Generated SQL (placeholders only)",
        "req_normal.csv",
    }
    sql = next(element for element in message.elements if element.name.startswith("Generated"))
    csv_file = next(element for element in message.elements if element.name.endswith(".csv"))
    assert sql.content == "SELECT ? AS value"
    assert sql.display == "side"
    assert csv_file.content.startswith(b"month,region,revenue_usd")


async def test_on_message_renders_definition_without_sql_or_csv(
    monkeypatch: pytest.MonkeyPatch,
    ui_harness: FakeUserSession,
) -> None:
    runtime = FakeRuntime(
        state={
            "request_id": "req_definition",
            "answer": {
                "status": "answered",
                "notes": ["Revenue is the governed captured-order metric."],
            },
        }
    )

    async def get_runtime(_config: ui.UiConfig) -> FakeRuntime:
        return runtime

    monkeypatch.setattr(ui, "get_runtime", get_runtime)
    await ui.on_message(SimpleNamespace(content="Define revenue"))

    message = SentMessage.sent[-1]
    assert "Revenue is the governed" in message.content
    assert message.elements == []


async def test_governed_refusal_is_rendered_as_a_normal_answer(
    monkeypatch: pytest.MonkeyPatch,
    ui_harness: FakeUserSession,
) -> None:
    runtime = FakeRuntime(
        state={
            "request_id": "req_denied",
            "answer": {
                "status": "denied",
                "evidence_status": "unavailable",
                "notes": ["The principal may not execute this metric."],
                "reason_codes": ["NOT_AUTHORIZED"],
            },
        }
    )

    async def get_runtime(_config: ui.UiConfig) -> FakeRuntime:
        return runtime

    monkeypatch.setattr(ui, "get_runtime", get_runtime)
    await ui.on_message(SimpleNamespace(content="restricted metric"))

    assert "`denied`" in SentMessage.sent[-1].content
    assert "`NOT_AUTHORIZED`" in SentMessage.sent[-1].content
    assert "unexpected server error" not in SentMessage.sent[-1].content


async def test_unexpected_exception_shows_only_safe_generic_error(
    monkeypatch: pytest.MonkeyPatch,
    ui_harness: FakeUserSession,
) -> None:
    async def get_runtime(_config: ui.UiConfig) -> FakeRuntime:
        raise RuntimeError("database-password-should-stay-in-server-log")

    monkeypatch.setattr(ui, "get_runtime", get_runtime)
    await ui.on_message(SimpleNamespace(content="Show revenue"))

    assert "unexpected server error" in SentMessage.sent[-1].content
    assert "database-password" not in SentMessage.sent[-1].content


async def test_session_lock_serializes_overlapping_messages(
    monkeypatch: pytest.MonkeyPatch,
    ui_harness: FakeUserSession,
) -> None:
    runtime = FakeRuntime(delay=0.01)

    async def get_runtime(_config: ui.UiConfig) -> FakeRuntime:
        return runtime

    monkeypatch.setattr(ui, "get_runtime", get_runtime)
    await asyncio.gather(
        ui.on_message(SimpleNamespace(content="first")),
        ui.on_message(SimpleNamespace(content="second")),
    )

    assert runtime.max_active == 1


def test_clarification_action_response_extracts_only_payload_value() -> None:
    assert ui._selected_action_value({"payload": {"value": "q2_2026"}}) == "q2_2026"
    assert ui._selected_action_value(SimpleNamespace(payload={"value": "q1_2026"})) == ("q1_2026")
    assert ui._selected_action_value(None) is None
    assert ui._selected_action_value({"payload": {"value": 123}}) is None


async def test_chainlit_clarification_uses_clickable_authorized_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_actions: list[FakeElement] = []
    captured_message: dict[str, Any] = {}

    def action(**values: Any) -> FakeElement:
        result = FakeElement(**values)
        created_actions.append(result)
        return result

    class AskAction:
        def __init__(self, **values: Any) -> None:
            captured_message.update(values)

        async def send(self) -> dict[str, Any]:
            return {"payload": {"value": "q2_2026"}}

    monkeypatch.setattr(ui.cl, "Action", action)
    monkeypatch.setattr(ui.cl, "AskActionMessage", AskAction)
    request = ClarificationRequest.model_validate(clarification_payload())

    assert await ui._ask_clarification(request) == "q2_2026"
    assert [item.label for item in created_actions] == ["Q2 2026", "Q1 2026"]
    assert [item.payload for item in created_actions] == [
        {"value": "q2_2026"},
        {"value": "q1_2026"},
    ]
    assert captured_message["raise_on_timeout"] is False
    assert "April through June" in captured_message["content"]


def test_chainlit_configuration_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    names = (
        "TTSQL_CHAINLIT_PRINCIPAL",
        "TTSQL_SCENARIO",
        "TTSQL_RETRIEVAL_PROVIDER",
        "TTSQL_USE_LLM",
        "TTSQL_USE_EMBEDDINGS",
        "TTSQL_CHAINLIT_SHOW_SQL",
        "TTSQL_CHAINLIT_MAX_ROWS",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    config = ui.UiConfig.from_env()
    assert config.runtime_signature == ("golden", "inmemory", True, True)
    assert config.principal == "analyst_full"
    assert config.show_sql
    assert config.max_rows == 100

    monkeypatch.setenv("TTSQL_RETRIEVAL_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="inmemory or opensearch"):
        ui.UiConfig.from_env()
