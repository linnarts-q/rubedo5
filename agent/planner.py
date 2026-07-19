from __future__ import annotations
import json
import logging
from datetime import datetime
from llm.groq import chat as groq_chat
from agent.prompts import build_analytics_system

log = logging.getLogger("rubedo.planner")

_PLAN_PROMPT = """
Составь план выполнения задачи.
Доступные инструменты: {tools}

Отвечай строго JSON, без markdown:
{{"steps": ["шаг 1", "шаг 2"], "max_iterations": <число 5-20>}}
Задача: {task}
"""


async def make_plan(task: str, tool_names: list[str]) -> dict:
    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = _PLAN_PROMPT.format(tools=", ".join(tool_names), task=task)
    messages = [
        {"role": "system", "content": build_analytics_system(dt)},
        {"role": "user", "content": prompt},
    ]

    def _parse(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(raw)

    try:
        resp = await groq_chat(messages, temperature=0.2)
        return _parse(resp.choices[0].message.content)
    except Exception as e:
        log.warning(f"Planner Groq failed: {e}")
        return {"steps": [task], "max_iterations": 10}
