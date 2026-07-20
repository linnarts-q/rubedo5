from __future__ import annotations
import asyncio
import logging
import os
import re
from datetime import datetime
from config import (
    OWNER_NAME, HISTORY_LIMIT, SUMMARIZE_EVERY,
    EXECUTOR_MAX_ITER_SIMPLE, EXECUTOR_MAX_ITER_DEEP,
    EXECUTOR_MAX_ITER_DEFAULT, EXECUTOR_MAX_ITER_HARD_CAP,
    now_local,
)
from bus.events import AgentStarted, AgentThinking, AgentFinished, AgentReplied, AgentError
from agent.classifier import classify
from agent.planner import make_plan
from agent.executor import run as executor_run
from agent.audit import AuditLogger, check_normal_threshold, NORMAL_DIR
from agent.prompts import build_gpt_system, build_analytics_system
from agent.tools import TOOLS_SCHEMA, TOOLS_MAP, set_context
from agent import approval, stopword, outcomes, sessions, questions
from agent.reflect import reflect_on_failure
from agent.tool_categories import get_tools_for_categories
from memory.db import (
    load_history, save_message, load_facts, search_events,
    load_latest_summary, save_summary, save_event, load_recent_events,
    count_messages_since_last_summary, load_messages_since_last_summary,
    get_last_message_time, save_meta, load_meta, profile_get_all,
    search_experience,
)
from llm.groq import chat as groq_chat
from llm.exceptions import AllKeysExhausted

log = logging.getLogger("rubedo.controller")

_ROUTE_MAX_ITER = {"simple": EXECUTOR_MAX_ITER_SIMPLE, "deep": EXECUTOR_MAX_ITER_DEEP}
_TS_RE = re.compile(r"^\[?\d{1,2}:\d{2}\]?\s+")
_SYSPFX_RE = re.compile(r"^\[RUBeOS\][^\n]*\n?", re.MULTILINE)
# Some non-OpenAI-native models (e.g. Nemotron) occasionally emit
# tool-call XML in the reply *content* instead of the structured
# `tool_calls` field. Strip the artifact so it doesn't leak to chat;
# the executor already handled the actual call (or didn't, in which
# case we log this for follow-up investigation).
_TOOLCALL_XML_RE = re.compile(
    r"<tool_call>.*?</tool_call>", re.DOTALL | re.IGNORECASE,
)

_session_started: set[str] = set()

_ACK_SET = {
    "понял", "поняла", "ладно", "окей", "ок", "ok", "okay", "хорошо",
    "угу", "ага", "ясно", "отлично", "супер", "класс", "got it",
    "понятно", "принял", "принято", "договорились", "👍", "✅", "👌",
    "хок", "лады", "ладно", "пойдет", "пойдёт",
}


def _is_ack(text: str) -> bool:
    return text.strip().lower().rstrip("!.,):") in _ACK_SET


def _intercept_expired(armed_meta_key: str, hours: float) -> bool:
    """True if the timestamp at `armed_meta_key` is older than `hours`,
    or missing/invalid. Used to evict stale intercepts (wrapup
    verification, plan response, T+60 reply) so they don't hijack the
    user's first conversation after an unrelated delay."""
    raw = load_meta(armed_meta_key) or ""
    if not raw:
        return True  # no timestamp = unsafe to intercept
    try:
        armed = datetime.fromisoformat(raw)
    except Exception:
        return True
    return (datetime.now() - armed).total_seconds() > hours * 3600


def _clean_reply(text: str) -> str:
    text = _SYSPFX_RE.sub("", text)
    text = _TS_RE.sub("", text)
    if _TOOLCALL_XML_RE.search(text):
        log.warning(
            "Stripped raw <tool_call> XML from model reply — model leaked "
            "tool-call markup as content (first 200 chars): %s",
            text[:200],
        )
        text = _TOOLCALL_XML_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _get_day_state() -> dict | None:
    try:
        from day.state import get_today_state, get_today_tasks
        state = get_today_state() or {}
        tasks = get_today_tasks()
        return {**state, "tasks": tasks}
    except ImportError:
        return None
    except Exception as e:
        log.debug(f"Could not load day state: {e}")
        return None


async def _run_approved_tool(name: str, args: dict) -> str:
    """Execute a previously yellow/red-zone-gated tool call for real,
    after the owner confirmed it. Mirrors the dispatch in
    agent/executor.py (sync vs coroutine tools) but outside the normal
    tool-calling loop, since this isn't a continuation of the original
    LLM turn — it's a direct, one-off execution of a stored request."""
    fn = TOOLS_MAP.get(name)
    if fn is None:
        return f"Инструмент '{name}' не найден."
    if name in ("file_write", "file_delete"):
        # Undo snapshot (§15) — only reached here for the yellow-zone
        # case (path outside workspace/); in-workspace writes are green
        # and never come through this approval path at all.
        try:
            from agent.tools import _resolve_path
            from agent import undo
            target = _resolve_path(str(args.get("filename", "")))
            undo.snapshot_before_write(target)
        except Exception as e:
            log.warning(f"undo snapshot skipped for {name}: {e}")
    try:
        coro = fn(**args) if asyncio.iscoroutinefunction(fn) else asyncio.to_thread(fn, **args)
        return str(await coro)
    except Exception as e:
        log.error(f"Approved tool '{name}' failed: {e}", exc_info=True)
        return f"Ошибка: {e}"


async def summarize_session(session_id: str) -> None:
    msgs = load_messages_since_last_summary(session_id)
    if not msgs:
        return
    conv = "\n".join(f"{m['role']}: {m['content'][:200]}" for m in msgs)
    dt = now_local().strftime("%d.%m.%Y %H:%M")
    try:
        resp = await groq_chat(
            [
                {"role": "system", "content": build_analytics_system(dt)},
                {
                    "role": "user",
                    "content": (
                        f"Сократи этот диалог до 2-3 предложений на русском, "
                        f"сохранив ключевые факты и договорённости:\n\n{conv}"
                    ),
                },
            ],
            temperature=0.3,
        )
        summary = resp.choices[0].message.content.strip()
        save_summary(session_id, summary)
        log.info(f"Session {session_id} summarized ({len(msgs)} messages)")
    except Exception as e:
        log.warning(f"Summarization failed: {e}")


async def handle_message(
    user_id: int,
    text: str,
    bus_client,
    send_fn,
    send_file_fn=None,
    send_photo_fn=None,
    skip_save_user: bool = False,
) -> None:
    session_id = "lin"
    _first_after_restart = session_id not in _session_started
    _session_started.add(session_id)
    dt = now_local().strftime("%d.%m.%Y %H:%M")

    # Stop-phrase (techspec §15) — plain string comparison, before any
    # LLM call of any kind, so it still works if the models are down or
    # looping. Checked before the audit logger even sees this turn.
    if stopword.is_stop_phrase(text):
        stopword.freeze()
        reply = "Остановилась. Ничего не делаю, пока не скажешь возобновить."
        if not skip_save_user:
            save_message(session_id, "user", text)
        save_message(session_id, "assistant", reply)
        await send_fn(reply)
        await bus_client.publish(AgentReplied(session_id=session_id))
        return
    if stopword.is_resume_phrase(text):
        stopword.unfreeze()
        reply = "Возобновляю."
        if not skip_save_user:
            save_message(session_id, "user", text)
        save_message(session_id, "assistant", reply)
        await send_fn(reply)
        await bus_client.publish(AgentReplied(session_id=session_id))
        return

    audit = AuditLogger(session_id=session_id)
    audit.user_message(text)

    await bus_client.publish(AgentStarted(session_id=session_id))
    await bus_client.publish(AgentThinking(session_id=session_id))

    # Pending tool approval (techspec §1/§15) — a yellow/red-zone tool
    # call from a previous turn is waiting on a yes/no. Recognizable
    # confirmations resolve it here, before routing even runs; anything
    # else falls through to the normal flow and leaves it pending.
    _pending = approval.pending()
    if _pending:
        _tsid = _pending.get("task_session_id")
        confirmed = approval.is_confirmation(text)
        if confirmed is True:
            approval.clear()
            if not skip_save_user:
                save_message(session_id, "user", text)
            sessions.resume(_tsid)
            result = await _run_approved_tool(_pending["name"], _pending["args"])
            reply = result
            if str(result).startswith("Ошибка"):
                sessions.fail(_tsid, result)
            else:
                sessions.complete(_tsid, result=str(result)[:300])
            await send_fn(reply)
            save_message(session_id, "assistant", reply)
            await bus_client.publish(AgentReplied(session_id=session_id))
            return
        if confirmed is False:
            approval.clear()
            if not skip_save_user:
                save_message(session_id, "user", text)
            sessions.resume(_tsid)
            sessions.cancel(_tsid, reason="владелец отменил подтверждение")
            reply = "Отменила, не выполняю."
            await send_fn(reply)
            save_message(session_id, "assistant", reply)
            await bus_client.publish(AgentReplied(session_id=session_id))
            return
        # Not a recognizable yes/no — leave it pending, handle this
        # message normally (owner may be asking something about it, or
        # about something else entirely).

    # Intercept a session question answer (§2 phase 1, `ask_user` tool)
    # — a task session paused mid-reasoning waiting on a free-text
    # answer. Resuming means continuing the SAME executor run with the
    # owner's answer appended, not starting a fresh turn from scratch —
    # that's the whole point of carrying the in-flight history through
    # agent/questions.py rather than just re-asking from zero.
    _pending_q = questions.pending()
    if _pending_q:
        questions.clear()
        if not skip_save_user:
            save_message(session_id, "user", text)
        _tsid_q = _pending_q["session_id"]
        sessions.resume(_tsid_q)
        sessions.log_decision(_tsid_q, "answer", text)
        resumed_history = _pending_q["history"] + [{"role": "user", "content": text}]
        _cats_q = _pending_q.get("tool_categories", [])
        tools_schema, tools_map = get_tools_for_categories(_cats_q)
        try:
            reply, _ = await executor_run(
                resumed_history, tools_schema, tools_map, session_id, bus_client,
                _pending_q.get("max_iter", EXECUTOR_MAX_ITER_DEFAULT),
                audit=audit,
                full_tools_schema=TOOLS_SCHEMA, full_tools_map=TOOLS_MAP,
                task_session_id=_tsid_q, tool_categories=_cats_q,
            )
            reply = _clean_reply(reply)
        except AllKeysExhausted:
            reply = "Все API-ключи на лимите, попробуй позже."
            sessions.fail(_tsid_q, "AllKeysExhausted")
        except Exception as e:
            reply = "Что-то пошло не так, попробуй ещё раз."
            sessions.fail(_tsid_q, f"{type(e).__name__}: {e}")
            log.exception(f"Session-resume error [{type(e).__name__}]: {e}")
        await send_fn(reply)
        save_message(session_id, "assistant", reply)
        await bus_client.publish(AgentReplied(session_id=session_id))
        return

    # Intercept wrapup plan response (TTL 20h — covers wrapup-at-23:00
    # plus reply any time before next-day evening). Briefing intentionally
    # does NOT clear this on morning rollover; only the TTL evicts.
    if load_meta("wrapup_awaiting_plan") == "1":
        if _intercept_expired("wrapup_plan_armed_at", hours=20):
            log.info("Wrapup plan intercept stale, clearing")
            save_meta("wrapup_awaiting_plan", "0")
            save_meta("wrapup_plan_armed_at", "")
        else:
            save_meta("wrapup_awaiting_plan", "0")
            save_meta("wrapup_plan_armed_at", "")
            try:
                from day.planner import handle_plan_response
                reply = await handle_plan_response(text)
                await send_fn(reply)
                save_message(session_id, "user", text)
                save_message(session_id, "assistant", reply)
                await bus_client.publish(AgentReplied(session_id=session_id))
                return
            except ImportError:
                pass
            except Exception as e:
                log.warning(f"handle_plan_response failed: {e}")

    # Intercept wrapup verification response (TTL ~4h — same evening only).
    if load_meta("wrapup_awaiting_verification") == "1":
        if _intercept_expired("wrapup_armed_at", hours=4):
            log.info("Wrapup verification intercept stale, clearing")
            save_meta("wrapup_awaiting_verification", "0")
            save_meta("wrapup_armed_at", "")
            save_meta("wrapup_unverified_ids", "")
        else:
            save_meta("wrapup_awaiting_verification", "0")
            save_meta("wrapup_armed_at", "")
            try:
                from day.wrapup import handle_verification_response
                await handle_verification_response(text, self_send=send_fn)
                save_message(session_id, "user", text)
                await bus_client.publish(AgentReplied(session_id=session_id))
                return
            except Exception as e:
                log.warning(f"verification response handler failed: {e}")

    # Intercept T+60 awaiting-response (TTL ~2h — fresh question only).
    # Meta value is JSON-encoded list of task IDs (or legacy bare int).
    t60_raw = load_meta("awaiting_t60_response")
    if t60_raw:
        if _intercept_expired("awaiting_t60_armed_at", hours=2):
            log.info("T+60 intercept stale, clearing")
            save_meta("awaiting_t60_response", "")
            save_meta("awaiting_t60_armed_at", "")
        else:
            save_meta("awaiting_t60_response", "")
            save_meta("awaiting_t60_armed_at", "")
            try:
                from day.proactive import handle_t60_response
                reply_text = await handle_t60_response(t60_raw, text)
                if reply_text:
                    await send_fn(reply_text)
                    save_message(session_id, "user", text)
                    save_message(session_id, "assistant", reply_text)
                    await bus_client.publish(AgentReplied(session_id=session_id))
                    return
            except Exception as e:
                log.warning(f"T+60 intercept failed: {e}")

    # Intercept clarification answer (TTL 30 min — user is expected to reply quickly).
    _cl_answer_saved = False
    _pending_cl_raw = load_meta("pending_clarification_intent")
    if _pending_cl_raw:
        if _intercept_expired("pending_clarification_armed_at", hours=0.5):
            log.info("Clarification intercept stale, clearing")
            save_meta("pending_clarification_intent", "")
            save_meta("pending_clarification_armed_at", "")
        else:
            import json as _cj_imp
            save_meta("pending_clarification_intent", "")
            save_meta("pending_clarification_armed_at", "")
            try:
                _pend = _cj_imp.loads(_pending_cl_raw)
            except Exception:
                _pend = {"text": _pending_cl_raw, "route": "simple", "skill": None}
            _orig_text = _pend.get("text", "")
            _orig_skill = _pend.get("skill") or ""
            if not skip_save_user:
                save_message(session_id, "user", text)
                _cl_answer_saved = True
            from agent.classifier import extract_clarification_answer as _extract_cl
            _questions = _pend.get("questions", [])
            _extracted = await _extract_cl(_questions, text)
            if _orig_skill == "weather":
                text = f"Погода в {_extracted}: {_orig_text}"
            else:
                text = f"{_orig_text}\n(Уточнение: {_extracted})"

    if _is_ack(text):
        log.info(f"[{session_id}] ack-only message, skipping reply")
        if not skip_save_user and not _cl_answer_saved:
            save_message(session_id, "user", text)
        await bus_client.publish(AgentReplied(session_id=session_id))
        return

    ctx_history = load_history(session_id, limit=6)
    route_info = await classify(text, history=ctx_history)
    route = route_info.get("route", "simple")
    context_type = route_info.get("context", "chat")
    intent = route_info.get("intent", text)

    if route == "command" and not text.strip().startswith("/"):
        route = "simple"

    # Frozen (stop-phrase armed, techspec §15): keep talking, but no
    # autonomous action — skill dispatch and tool access are both off
    # until the resume phrase is seen.
    _frozen = stopword.is_frozen()
    if _frozen and route == "skill":
        route = "simple"

    log.info(f"[{session_id}] route={route} context={context_type} intent={intent[:60]}")
    audit.route(route=route, context_type=context_type, intent=intent)

    if not skip_save_user and not _cl_answer_saved:
        save_message(session_id, "user", text)

    # Clarification gate: if classifier detected missing info, ask before proceeding.
    _missing_info = route_info.get("missing_info", [])
    if _missing_info and route != "command" and not load_meta("pending_clarification_intent"):
        import json as _cj_gate
        from config import DEFAULT_CITY as _dc
        if route == "skill" and route_info.get("skill") == "weather" and _dc:
            _missing_info = [
                q for q in _missing_info
                if "город" not in q.lower() and "city" not in q.lower()
            ]
        if _missing_info:
            _q = (
                "\n".join(f"— {q}" for q in _missing_info)
                if len(_missing_info) > 1
                else _missing_info[0]
            )
            save_meta("pending_clarification_intent", _cj_gate.dumps({
                "text": text, "route": route, "skill": route_info.get("skill"),
                "questions": _missing_info,
            }))
            save_meta("pending_clarification_armed_at", datetime.now().isoformat())
            await send_fn(_q)
            save_message(session_id, "assistant", _q)
            await bus_client.publish(AgentReplied(session_id=session_id))
            return

    if route == "command":
        reply = await _handle_command(text, session_id)
        await send_fn(reply)
        save_message(session_id, "assistant", reply)
        await bus_client.publish(AgentReplied(session_id=session_id))
        return

    if route == "skill":
        try:
            import skills  # noqa: F401
            from skills.registry import dispatch as _dispatch, match_skill as _match
        except ImportError:
            # skills/ isn't ported yet at this rework stage — fall back
            # to the normal tool-loop path rather than crash the turn.
            log.debug("skills package not available yet, routing as simple")
            route = "simple"
        else:
            skill_name = route_info.get("skill") or ""
            if not skill_name or skill_name == "null":
                skill_name = _match(text) or ""
            if skill_name:
                reply = await _dispatch(skill_name, text, session_id)
                save_message(session_id, "assistant", reply)
                if reply.startswith("FILE:") and send_file_fn:
                    file_path = reply[5:]
                    if os.path.exists(file_path):
                        await send_file_fn(file_path)
                    else:
                        await send_fn(f"Файл не найден: {file_path}")
                else:
                    await send_fn(reply)
                asyncio.create_task(_post_process(session_id, text, reply, context_type))
                await bus_client.publish(AgentFinished(session_id=session_id, reply=reply[:100]))
                await bus_client.publish(AgentReplied(session_id=session_id))
                return
            route = "simple"

    facts = load_facts(session_id, limit=5)
    events_raw = search_events(text, limit=3)
    event_snippets = [e["content"] for e in events_raw]
    recent_actions = load_recent_events(limit=5)
    summary = load_latest_summary(session_id)
    history = load_history(session_id, limit=HISTORY_LIMIT)
    history = outcomes.annotate(history)  # §11 layer 1
    day_state = _get_day_state()
    owner_profile = profile_get_all("owner") or None
    self_profile = profile_get_all("self") or None

    plan_text = ""
    max_iter = _ROUTE_MAX_ITER.get(route, 8)
    task_session_id = None
    similar_experience = None

    if route == "deep":
        plan = await make_plan(intent, list(TOOLS_MAP.keys()))
        steps = plan.get("steps", [])
        max_iter = min(plan.get("max_iterations", EXECUTOR_MAX_ITER_DEFAULT), EXECUTOR_MAX_ITER_HARD_CAP)
        if steps:
            plan_text = "План:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
        if len(steps) >= 2:
            await send_fn("Приступаю к задаче — это займёт немного времени.")
        # Task session (§2 phase 1) — only "deep", multi-step work gets
        # one; "simple" tool turns and pure chat stay unsessioned by
        # design (see agent/sessions.py docstring).
        _tsess = sessions.start(intent or text, origin="chat")
        task_session_id = _tsess["id"]
        if steps:
            sessions.log_decision(task_session_id, "plan", plan_text)
        # Experience revival (§9, stage 3) — surface how similar past
        # attempts went before this one starts, not after it fails the
        # same way again.
        try:
            similar_experience = search_experience(intent or text, limit=3) or None
        except Exception as e:
            log.debug(f"experience search skipped: {e}")

    if _first_after_restart:
        last_time = get_last_message_time(session_id)
        if last_time:
            restart_note = (
                f"[Перезапуск системы. Последняя активность: {last_time[:16]}. "
                f"Резюме: {summary or 'нет'}]"
            )
            plan_text = restart_note + ("\n" + plan_text if plan_text else "")

    system = build_gpt_system(
        interlocutor=OWNER_NAME,
        context_type=context_type,
        summary=summary,
        facts=facts,
        recent_events=event_snippets,
        datetime_str=dt,
        plan=plan_text,
        recent_actions=recent_actions,
        day_state=day_state,
        owner_profile=owner_profile,
        self_profile=self_profile,
        similar_experience=similar_experience,
    )

    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    set_context(
        session_id=session_id,
        interlocutor=OWNER_NAME,
        send_file_fn=send_file_fn,
        send_photo_fn=send_photo_fn,
    )

    # Tools-gate (§11 layer 3 + §15): frozen, or a route that's just
    # conversation (chat/emotional) with nothing to act on, gets no
    # tools at all — physically can't fire one, not a prompt-level
    # request to behave. Otherwise, only the classifier's picked
    # categories (§13) load, with the full set kept on hand so the
    # executor can widen if the model names a real tool outside them.
    _no_tools = _frozen or context_type in ("chat", "emotional")
    _tool_cats = route_info.get("tool_categories", [])
    if _no_tools:
        tools_schema, tools_map = [], {}
    else:
        tools_schema, tools_map = get_tools_for_categories(_tool_cats)

    try:
        reply, _ = await executor_run(
            messages, tools_schema, tools_map, session_id, bus_client, max_iter,
            audit=audit,
            full_tools_schema=TOOLS_SCHEMA, full_tools_map=TOOLS_MAP,
            task_session_id=task_session_id, tool_categories=_tool_cats,
        )
        reply = _clean_reply(reply)
    except AllKeysExhausted:
        reply = "Все API-ключи на лимите, попробуй позже."
        audit.exception("controller.handle_message", "AllKeysExhausted")
        sessions.fail(task_session_id, "AllKeysExhausted")
        log.error("All LLM keys exhausted")
    except Exception as e:
        reply = "Что-то пошло не так, попробуй ещё раз."
        audit.exception("controller.handle_message", f"{type(e).__name__}: {e}")
        sessions.fail(task_session_id, f"{type(e).__name__}: {e}")
        await bus_client.publish(AgentError(session_id=session_id, error=str(e)))
        log.exception(f"Agent error [{type(e).__name__}]: {e}")

    # Reflective cycle (§3, stage 3) — a task session that ended up
    # failed gets one look back at its own journal before the owner
    # sees a generic error: either a corrected retry, or an honest,
    # specific diagnosis instead of "что-то пошло не так".
    if task_session_id is not None:
        _final_sess = sessions.get(task_session_id)
        if _final_sess and _final_sess["status"] == "failed":
            _journal = sessions.journal(task_session_id)
            _already_reflected = any(e["kind"] == "reflect" for e in _journal)
            if not _already_reflected:
                _verdict = await reflect_on_failure(_journal, _final_sess.get("error") or "")
                sessions.log_decision(task_session_id, "reflect", _verdict["diagnosis"])
                if _verdict["retry"] and _verdict["corrected_approach"]:
                    sessions.resume(task_session_id)
                    retry_messages = messages + [{
                        "role": "user",
                        "content": f"Прошлая попытка не удалась. Скорректированный подход: {_verdict['corrected_approach']}",
                    }]
                    try:
                        reply, _ = await executor_run(
                            retry_messages, tools_schema, tools_map, session_id, bus_client, max_iter,
                            audit=audit,
                            full_tools_schema=TOOLS_SCHEMA, full_tools_map=TOOLS_MAP,
                            task_session_id=task_session_id, tool_categories=_tool_cats,
                        )
                        reply = _clean_reply(reply)
                    except Exception as e2:
                        sessions.fail(task_session_id, f"retry failed: {type(e2).__name__}: {e2}")
                        reply = _verdict["diagnosis"]
                else:
                    reply = _verdict["diagnosis"]

    await send_fn(reply)
    save_message(session_id, "assistant", reply)

    log_path = audit.close()
    if audit.is_error and log_path and send_file_fn:
        try:
            await send_file_fn(str(log_path))
        except Exception as e:
            log.warning(f"audit: error-log send failed: {e}")
    if not audit.is_error:
        try:
            new_count = check_normal_threshold()
            if new_count is not None:
                await send_fn(
                    f"Кстати, в папке успешных логов накопилось {new_count} запусков "
                    f"({NORMAL_DIR.as_posix()}). Может, пора разгрести."
                )
        except Exception as e:
            log.warning(f"audit: threshold check failed: {e}")

    asyncio.create_task(_post_process(session_id, text, reply, context_type))
    await bus_client.publish(AgentFinished(session_id=session_id, reply=reply[:100]))
    await bus_client.publish(AgentReplied(session_id=session_id))


async def _post_process(
    session_id: str, user_text: str, reply: str, context_type: str = "chat"
) -> None:
    count = count_messages_since_last_summary(session_id)
    if count >= SUMMARIZE_EVERY:
        await summarize_session(session_id)
    save_event(
        session_id=session_id,
        content=f"Запрос: {user_text[:100]} → Ответ: {reply[:100]}",
        priority=2,
        category="interaction",
    )
    if context_type in ("plan", "day_review"):
        try:
            from day.planner import adjust_plan
            adjustment = await adjust_plan(user_text, session_id)
            if adjustment:
                log.info(f"Auto plan adjustment applied: {adjustment}")
        except Exception as e:
            log.debug(f"Auto plan adjust skipped: {e}")


async def _handle_command(text: str, session_id: str) -> str:
    cmd = text.strip().lower().split()[0]
    if cmd == "/status":
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            ram = psutil.virtual_memory()
            return (
                f"CPU: {cpu}% | RAM: {ram.percent}% "
                f"({ram.used // 1024 // 1024}/{ram.total // 1024 // 1024} MB)"
            )
        except ImportError:
            return "psutil не установлен"
    if cmd == "/tasks":
        from agent.tools import list_tasks
        return list_tasks()
    if cmd == "/plan":
        try:
            from day.state import get_today_tasks
            tasks = get_today_tasks()
        except ImportError:
            return "Day engine не запущен."
        if not tasks:
            return "План на сегодня пуст."
        lines = []
        for t in tasks:
            line = f"• {t['title']}"
            if t.get("scheduled_at"):
                line += f" [{t['scheduled_at']}]"
            if t.get("status") not in (None, "pending"):
                line += f" ({t['status']})"
            lines.append(line)
        return "План на сегодня:\n" + "\n".join(lines)
    if cmd in ("/sleep_on", "/sleep_off"):
        from pathlib import Path
        flag = Path("data/.sleep_request")
        flag.parent.mkdir(parents=True, exist_ok=True)
        value = "on" if cmd == "/sleep_on" else "off"
        try:
            flag.write_text(value)
        except Exception as e:
            return f"Не получилось записать флаг: {e}"
        try:
            from bus.client import sync_publisher
            from bus.events import SleepRequested
            sync_publisher.publish(SleepRequested(mode=value))
        except Exception as e:
            log.debug(f"SleepRequested publish skipped: {e}")
        return f"Sleep mode: {value} (sent to display)"
    return f"Неизвестная команда: {cmd}"
