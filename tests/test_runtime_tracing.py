from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import client as llm_client
from app.runtime import AppRuntime

pytestmark = pytest.mark.unit


class RecordingGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any], Any]] = []
        self.request_id = "req_resume_source"

    async def ainvoke(self, value: Any, config: dict[str, Any], *, context: Any) -> dict[str, Any]:
        self.calls.append((value, config, context))
        if isinstance(value, dict):
            self.request_id = value["request_id"]
            return value
        return {"request_id": self.request_id, "answer": {"status": "answered"}}

    def get_state(self, _config: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(values={"request_id": self.request_id}, tasks=())


@pytest.fixture
def runtime() -> AppRuntime:
    result = AppRuntime.build(use_llm=False, use_embeddings=False)
    result.graph = RecordingGraph()
    return result


async def test_ask_merges_callbacks_tags_run_name_and_safe_metadata(runtime: AppRuntime) -> None:
    callback = object()
    await runtime.ask(
        "Show revenue",
        "analyst_full",
        thread_id="graph-thread-1",
        callbacks=(callback,),
        trace_tags=("interface:chainlit", "interface:chainlit"),
        trace_metadata={
            "interface": "chainlit",
            "chainlit_session_id": "session-1",
            "LANGSMITH_API_KEY": "must-not-appear",
            "scope_token": "must-not-appear",
            "sql_parameters": "tenant-value-must-not-appear",
            "parameter_values": "must-not-appear",
            "principal": "forged-principal",
            "live_service": object(),
        },
    )

    _value, config, context = runtime.graph.calls[0]
    assert config["run_name"] == "texttosql.ask"
    assert config["callbacks"] == [callback]
    assert config["configurable"]["thread_id"] == "graph-thread-1"
    assert config["tags"].count("interface:chainlit") == 1
    assert f"scenario:{runtime.scenario}" in config["tags"]
    assert f"provider:{runtime.provider_name}" in config["tags"]
    assert "mode:fallback" in config["tags"]

    metadata = config["metadata"]
    assert metadata["request_id"].startswith("req_")
    assert metadata["graph_thread_id"] == "graph-thread-1"
    assert metadata["principal"] == "analyst_full"
    assert metadata["snapshot"] == context.scope.snapshot_id
    assert metadata["policy_epoch"] == context.scope.policy_epoch
    assert metadata["interface"] == "chainlit"
    assert metadata["chainlit_session_id"] == "session-1"
    serialized = repr(metadata)
    assert "must-not-appear" not in serialized
    assert "TrustedScope" not in serialized
    assert "live_service" not in metadata


async def test_resume_uses_correlated_request_and_graph_thread(runtime: AppRuntime) -> None:
    callback = object()
    await runtime.ask("revenue", thread_id="graph-thread-2")
    original_request_id = runtime.graph.request_id

    await runtime.resume(
        {"slot": "time_range", "value": "q2_2026"},
        thread_id="graph-thread-2",
        callbacks=(callback,),
        trace_tags=("interface:chainlit",),
        trace_metadata={"chainlit_session_id": "session-2"},
    )

    _value, config, _context = runtime.graph.calls[-1]
    assert config["run_name"] == "texttosql.resume"
    assert config["metadata"]["request_id"] == original_request_id
    assert config["metadata"]["graph_thread_id"] == "graph-thread-2"
    assert config["metadata"]["chainlit_session_id"] == "session-2"
    assert config["callbacks"] == [callback]


def test_openai_client_is_raw_when_tracing_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = object()
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.setattr(llm_client, "wrap_openai", lambda _client: pytest.fail("unexpected wrap"))

    assert llm_client._instrument_openai(raw) is raw


def test_openai_client_is_wrapped_when_langsmith_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = object()
    wrapped = object()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    def wrap(client: object) -> object | None:
        return wrapped if client is raw else None

    monkeypatch.setattr(llm_client, "wrap_openai", wrap)

    assert llm_client._instrument_openai(raw) is wrapped


def test_openai_wrapper_failure_falls_back_to_raw_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = object()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    def fail(_client: object) -> object:
        raise RuntimeError("tracing setup failed")

    monkeypatch.setattr(llm_client, "wrap_openai", fail)
    assert llm_client._instrument_openai(raw) is raw
