"""OmniRoute client (OpenAI-compatible), used only for structured output.

The model never produces SQL, identifiers, or authority. It fills a Pydantic schema whose
every field is an enumerated operator or a catalog ID, and the binder re-resolves all of it.

Model pinning: the planner and critic are pinned to a concrete model id. An `auto/*` alias
lets the router swap models between calls, so identical input can yield different plans and
different grades -- which makes evaluation numbers meaningless. Narration may use an alias.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TypeVar

from langsmith.wrappers import wrap_openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.contracts.errors import GovernedError, ReasonCode

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

# KNOWN REPRODUCIBILITY GAP (see DEVIATIONS.md).
#
# The planner SHOULD be pinned to a concrete model id: an `auto/*` alias lets the router pick
# a different model per request, so the same question can yield different plan proposals and
# the same grader can return different verdicts. That makes evaluation numbers noise.
#
# As of this build every concrete backend on the local router returns 402/401 (credits
# exhausted); only `auto/*` aliases respond. So the default is an alias, under protest.
# Set TTSQL_PLANNER_MODEL to a concrete id as soon as one has credit -- that is the whole fix.
DEFAULT_PLANNER_MODEL = "auto/smart"
DEFAULT_NARRATOR_MODEL = "auto/best-fast"


def planner_is_pinned(model: str) -> bool:
    """False when the planner is routed by alias, which makes proposals non-reproducible."""
    return not model.startswith("auto/")


@dataclass
class LlmConfig:
    base_url: str
    api_key: str
    planner_model: str
    narrator_model: str
    temperature: float = 0.0
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> LlmConfig:
        return cls(
            base_url=os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1"),
            api_key=os.getenv("OMNIROUTE_API_KEY", "none"),
            planner_model=os.getenv("TTSQL_PLANNER_MODEL", DEFAULT_PLANNER_MODEL),
            narrator_model=os.getenv("TTSQL_NARRATOR_MODEL", DEFAULT_NARRATOR_MODEL),
        )


class LlmClient:
    def __init__(self, config: LlmConfig | None = None) -> None:
        self.config = config or LlmConfig.from_env()
        raw_client = OpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
            max_retries=1,
        )
        self._client = _instrument_openai(raw_client)

    def health(self) -> bool:
        try:
            self._client.models.list()
            return True
        except Exception:
            return False

    def structured(
        self,
        schema: type[T],
        system: str,
        user: str,
        *,
        model: str | None = None,
        max_attempts: int = 2,
    ) -> T:
        """Ask for one JSON object matching `schema`.

        Structured output helps parsing. It never establishes truth: the result is a
        *proposal*, validated here for shape and re-resolved downstream for authority.
        """
        target = model or self.config.planner_model
        json_schema = schema.model_json_schema()
        instructions = (
            f"{system}\n\nReply with ONE JSON object and nothing else. It must validate "
            f"against this JSON Schema:\n{json.dumps(json_schema, indent=2)}"
        )

        last_error: str | None = None
        for _attempt in range(max_attempts):
            prompt = (
                user
                if last_error is None
                else (
                    f"{user}\n\nYour previous reply was rejected: {last_error}\n"
                    f"Return corrected JSON only."
                )
            )
            try:
                response = self._client.chat.completions.create(
                    model=target,
                    temperature=self.config.temperature,
                    messages=[
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content or ""
            except Exception as exc:
                raise GovernedError(
                    ReasonCode.PROVIDER_UNAVAILABLE, f"Language model unavailable: {exc}"
                ) from exc

            try:
                return schema.model_validate_json(_strip_fences(content))
            except ValidationError as exc:
                last_error = str(exc)[:600]

        raise GovernedError(
            ReasonCode.UNSUPPORTED_CAPABILITY,
            f"Model did not produce a valid {schema.__name__} after {max_attempts} "
            f"attempt(s): {last_error}",
        )

    def narrate(self, system: str, user: str) -> str:
        """Free text, used only where every number is already rendered deterministically."""
        response = self._client.chat.completions.create(
            model=self.config.narrator_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end != -1 else stripped


def _instrument_openai(client: OpenAI) -> OpenAI:
    """Add nested LangSmith model spans when tracing is enabled.

    Instrumentation is observability-only: an unavailable or misconfigured tracing client
    must never stop the governed query path from using the underlying model client.
    """

    if not (_env_enabled("LANGSMITH_TRACING") or _env_enabled("LANGCHAIN_TRACING_V2")):
        return client
    try:
        return wrap_openai(client)
    except Exception:
        logger.warning("LangSmith OpenAI instrumentation could not be enabled", exc_info=True)
        return client


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
