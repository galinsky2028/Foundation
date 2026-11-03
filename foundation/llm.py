"""Claude access with spend tracking and a monthly cap (optional for each project).

    from foundation import llm
    text = llm.complete_text("summarise_bill", system="...", user="...")
    data = llm.complete_json("extract_dates", system="...", user="...")

APP_LLM_PROVIDER=mock returns canned answers (tests, offline work); tests can set
llm.MockProvider.overrides["task_name"] = lambda user_text: "answer".

Prices in USD per million tokens, checked 2026-11-01 at
https://platform.claude.com/docs/en/about-claude/pricing
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .config import settings

log = logging.getLogger(__name__)

PRICES = {  # model: (input, output)
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = PRICES.get(model, PRICES["claude-fable-5-1"])  # unknown model: highest listed rate
    return round((input_tokens * p_in + output_tokens * p_out) / 1_000_000, 6)


def month_spend_usd() -> float:
    from sqlalchemy import func, select

    from .db import SessionLocal
    from .models import LLMUsage

    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as s:
        return float(s.scalar(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.created_at >= start)) or 0)


def _record(task: str, model: str, tin: int, tout: int) -> None:
    from .db import SessionLocal
    from .models import LLMUsage

    with SessionLocal() as s:
        s.add(LLMUsage(task=task[:80], model=model, input_tokens=tin, output_tokens=tout, cost_usd=cost_usd(model, tin, tout)))
        s.commit()


def complete_text(task: str, system: str, user: str, max_tokens: int | None = None) -> str:
    if settings.monthly_budget_usd > 0 and month_spend_usd() >= settings.monthly_budget_usd:
        raise BudgetExceeded(f"Monthly AI spend cap of ${settings.monthly_budget_usd:.2f} reached")
    text, tin, tout = _provider().text(task, system, user, max_tokens or settings.llm_max_tokens)
    _record(task, settings.llm_model if settings.llm_provider != "mock" else "mock", tin, tout)
    return text


def complete_json(task: str, system: str, user: str, max_tokens: int | None = None) -> Any:
    raw = complete_text(task, system + "\n\nRespond with one JSON value only: no prose, no code fences.", user, max_tokens)
    return parse_json(raw)


def parse_json(raw: str) -> Any:
    text = raw.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for a, b in (("{", "}"), ("[", "]")):
            i, j = text.find(a), text.rfind(b)
            if i != -1 and j > i:
                try:
                    return json.loads(text[i:j + 1])
                except json.JSONDecodeError:
                    pass
    raise LLMError("The model did not return valid JSON")


class AnthropicProvider:
    def __init__(self):
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4, timeout=300)

    def text(self, task, system, user, max_tokens):
        t0 = time.time()
        msg = self.client.messages.create(model=settings.llm_model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": user}])
        log.info("llm[%s] %.1fs", task, time.time() - t0)
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"Model output for '{task}' was cut off at {max_tokens} tokens")
        return "".join(b.text for b in msg.content if b.type == "text"), msg.usage.input_tokens, msg.usage.output_tokens


class MockProvider:
    overrides: dict[str, Callable[[str], str]] = {}

    def text(self, task, system, user, max_tokens):
        fn = self.overrides.get(task.split(":")[0])
        out = fn(user) if fn else json.dumps({"mock": True, "task": task})
        return out, len(user) // 4, len(out) // 4


_instance = None


def _provider():
    global _instance
    if _instance is None:
        _instance = MockProvider() if settings.llm_provider == "mock" else AnthropicProvider()
    return _instance
