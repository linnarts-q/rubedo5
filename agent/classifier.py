from __future__ import annotations
import json
import logging
from datetime import datetime
from llm.groq import chat as groq_chat
from agent.tool_categories import CATEGORY_NAMES

log = logging.getLogger("rubedo.classifier")

_VALID_ROUTES = {"skill", "simple", "deep", "command"}
_VALID_CONTEXTS = {"task", "plan", "info", "chat", "emotional", "urgent", "day_review"}
_VALID_SKILLS = {"weather", "reminder", "system", "logs", "music", "news"}
_VALID_CATEGORIES = set(CATEGORY_NAMES)

_CATEGORY_DESCRIPTIONS = """Tool categories (pick every one the message plausibly needs — §13, over-including is cheap, missing one isn't):
- memory: notes, facts about the owner, profile (owner/self), searching memory
- tasks: today's tasks, reminders, week events, recurring tasks, alarm
- queue: autonomous task queue, untimed backlog (pool)
- files: read/write/archive/convert files in the workspace
- web: web search, weather, navigation, calculator, research
- system: system diagnostics, shell/sudo/python exec, processes, volume, screenshot
- server: SpotRent control, commands on the separate server
- agent_self: self-update/restart, display restart, OS package update
- diagnostics: recent iterations/audit logs, archiving logs, rolling back the last change
- send: sending a file/photo to the owner
- wishes: Rubedo's own wish list"""

_CLASSIFY = f"""Classify the user message. Return strictly JSON (no markdown).

Routes:
- skill: deterministic action matching an available skill topic
- simple: regular conversation, quick answer, file/code/process work
- deep: complex multi-step task requiring planning (rarely used)
- command: ONLY messages starting with /

Available skills:
- weather: weather, forecast, temperature outside
- reminder: reminders, timers, \"remind me in/at...\"
- system: ip-address, cpu/ram/disk load, ssh-key, system metrics ONLY
- logs: ONLY the raw service log file ("дай лог-файл", "скинь log", "журнал"). Requests like "последние итерации", "трейс", "что ты делала", "что было" must go to `simple` — the agent has a dedicated `iterations_recent` tool for them, the logs skill would just dump the generic logfile.
- music: music, playlist, pause, next track
- news: news, what's happening, what's new, latest news

Context types:
- task: user wants something done / executed
- plan: planning, scheduling, discussing what to do
- info: factual question, information request
- chat: casual conversation, small talk
- emotional: emotional topic, venting, feelings, support
- urgent: urgent or critical request
- day_review: reflecting on the day, evening review, how was the day

Routing rules:
- \"show metrics / cpu load / how much memory / system info\" → skill: system
- \"how many files in X / what's in directory / list files / show files\" → simple (use file_list tool, NOT skill: system)
- \"delete/create/read file\", \"run process/program\", \"write code\" → simple (NOT system)
- command route ONLY if message starts with /
- \"add/delete/clear/update/edit task\", \"list tasks\", \"change time in task\" → simple (NOT reminder)
- reminder skill ONLY when user explicitly says \"напомни\", \"напоминание\", \"remind me\", \"таймер\", \"через N минут/часов\"
- messages starting with [голосовое] are voice transcriptions — classify by the transcribed content, not the prefix
- Choose context based on the intent behind the message
- Rule of thumb for `system` skill: if user asks a question that needs CURRENT live values of CPU/RAM/disk/temperature/network → skill:system. Anything else file/directory/process/code-related → simple (the agent has dedicated tools).

missing_info field: populate ONLY when critical info is missing and cannot be inferred.
Max 5 questions in Russian.
Populate when:
- Weather request with NO city in message or history → ["Для какого города?"]
- Research/analysis request with extremely vague scope → focused questions
Do NOT populate for: task management, reminders, chat, commands, any request with enough context.

{_CATEGORY_DESCRIPTIONS}
For route=chat/emotional with no concrete action, tool_categories can be empty — those routes get no tools regardless (§11).

Return JSON only:
{{\"route\": \"skill|simple|deep|command\", \"context\": \"task|plan|info|chat|emotional|urgent|day_review\", \"intent\": \"brief description in Russian\", \"skill\": \"weather|reminder|system|logs|music|news|null\", \"missing_info\": [], \"tool_categories\": []}}
"""


async def classify(message: str, history: list | None = None) -> dict:
    dt = datetime.now().strftime("%d.%m.%Y %H:%M")

    ctx_lines = ""
    if history:
        lines = []
        for m in history[-6:]:
            role = "Пользователь" if m["role"] == "user" else "Рубедо"
            lines.append(f"{role}: {m['content'][:120]}")
        ctx_lines = "\nКонтекст:\n" + "\n".join(lines) + "\n"

    messages = [
        {
            "role": "system",
            "content": f"Ты аналитический модуль Рубедо. Время: {dt}\n{_CLASSIFY}{ctx_lines}",
        },
        {"role": "user", "content": message},
    ]

    try:
        response = await groq_chat(messages, temperature=0.0)
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)

        # Schema validation
        if result.get("route") not in _VALID_ROUTES:
            log.warning(f"[classify] invalid route={result.get('route')!r}, defaulting simple")
            result["route"] = "simple"
        if result.get("context") not in _VALID_CONTEXTS:
            log.warning(f"[classify] invalid context={result.get('context')!r}, defaulting chat")
            result["context"] = "chat"
        skill = result.get("skill")
        if skill and skill not in _VALID_SKILLS:
            log.warning(f"[classify] invalid skill={skill!r}, clearing")
            result["skill"] = None
        if not result.get("intent"):
            result["intent"] = message[:60]
        if not isinstance(result.get("missing_info"), list):
            result["missing_info"] = []
        result["missing_info"] = [
            q for q in result["missing_info"] if isinstance(q, str) and q.strip()
        ]

        # tool_categories (§13) — chat/emotional legitimately want an
        # empty list (executor gets no tools regardless, per §11 layer
        # 3). Anything else with an empty/invalid list is treated as
        # under-classification, not a deliberate "no tools needed" — a
        # missing category silently starves the model of a real tool,
        # which is worse than a few thousand unnecessary prompt tokens,
        # so the safe default on doubt is "give everything".
        cats = result.get("tool_categories")
        if not isinstance(cats, list):
            cats = []
        cats = [c for c in cats if isinstance(c, str) and c in _VALID_CATEGORIES]
        if not cats and result["context"] not in ("chat", "emotional"):
            log.debug("[classify] no valid tool_categories, defaulting to all")
            cats = list(_VALID_CATEGORIES)
        result["tool_categories"] = cats

        log.info(
            f"[classify] «{message[:60]}» → route={result.get('route')} "
            f"context={result.get('context')} skill={result.get('skill')} | {result.get('intent', '')[:60]}"
        )
        return result
    except Exception as e:
        log.warning(f"Classifier failed ({type(e).__name__}): {e}, defaulting simple/chat")
        return {
            "route": "simple", "context": "chat", "intent": message, "skill": None,
            "missing_info": [], "tool_categories": [],
        }


async def extract_clarification_answer(questions: list[str], answer: str) -> str:
    """Extract relevant data from a conversational clarification reply using LLM."""
    q_text = "\n".join(f"— {q}" for q in questions) if questions else answer
    prompt = (
        f"Вопросы были:\n{q_text}\n\n"
        f"Ответ пользователя: «{answer}»\n\n"
        "Извлеки только суть ответа — конкретные данные без лишних слов. "
        "Если пользователь согласился с вариантом из вопроса, верни этот вариант. "
        "Верни только данные, без пояснений."
    )
    try:
        resp = await groq_chat(
            [
                {"role": "system", "content": "Ты модуль извлечения данных. Отвечай кратко."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"extract_clarification_answer failed: {e}")
        return answer
