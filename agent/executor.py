from __future__ import annotations
import asyncio
import json
import logging
import time
from config import (
    EXECUTOR_EXACT_DUPLICATE_LIMIT,
    EXECUTOR_TOOL_TIMEOUT_SEC,
    EXECUTOR_MAX_ITER_DEFAULT,
)
from agent.idempotency import (
    is_side_effect, DUPLICATE_BLOCK_MESSAGE, check_cooldown, mark_called,
)
from agent.audit import AuditLogger
from agent import zones, approval, sessions, questions, hanging
from llm.tiers import generation_chat
from llm.exceptions import AllKeysExhausted
from bus.events import ToolCalled, ToolFinished, WorkStarted, WorkCompleted, LLMExhausted

log = logging.getLogger("rubedo.executor")

EXACT_DUPLICATE_LIMIT = EXECUTOR_EXACT_DUPLICATE_LIMIT
_TOOL_TIMEOUT = float(EXECUTOR_TOOL_TIMEOUT_SEC)


async def run(*args, task_session_id: int | None = None, **kwargs) -> tuple[str, list]:
    """Thin wrapper around `_run_inner`: sets agent.sessions' current-
    session contextvar for the duration of this tool loop, so plan()/
    report() (agent/tools/sessions.py) — which have no other way to
    know which task session their call belongs to — read the right one
    even when a second session is genuinely concurrent (§2 phase 2).
    Always reset, success or exception, so it can never leak into
    whatever runs next on this same asyncio Task."""
    token = sessions.set_current(task_session_id)
    try:
        return await _run_inner(*args, task_session_id=task_session_id, **kwargs)
    finally:
        sessions.reset_current(token)


async def _run_inner(
    messages: list,
    tools_schema: list,
    tools_map: dict,
    session_id: str,
    bus_client,
    max_iterations: int = EXECUTOR_MAX_ITER_DEFAULT,
    audit: AuditLogger | None = None,
    full_tools_schema: list | None = None,
    full_tools_map: dict | None = None,
    task_session_id: int | None = None,
    tool_categories: list[str] | None = None,
    resumable_on_pause: bool = False,
) -> tuple[str, list]:
    """OpenAI-compatible tool loop. Returns (final_reply, updated_messages).

    Before a yellow/red-zone tool call (techspec §1, agent/zones.py) is
    ever executed, the loop halts and hands back a confirmation question
    as this turn's reply instead — agent/approval.py stores the pending
    call; agent/controller.py intercepts the owner's next message to
    either run it for real or cancel it. Only GREEN tools run inline
    here, same as every tool did before this stage.

    `tools_schema`/`tools_map` are usually the classifier's category
    pick (§13), not the full ~90-tool set. If the model asks for a real
    tool that just isn't in that pick, `full_tools_schema`/
    `full_tools_map` — the complete set, if the caller has one to fall
    back to — get swapped in for the rest of this run rather than
    failing outright. Lighter interim version of the spec's "one
    follow-up category request, then honest refusal"; the fuller
    version needs the reflective cycle (§3), a later stage.

    `task_session_id` (§2 phase 1) — if the caller opened a task session
    for this run (agent/controller.py, route == "deep"), every tool call
    and its outcome is appended to that session's decision journal, and
    the session is completed/failed/paused as the loop resolves. `None`
    means "no session for this turn" — every session.* call below is a
    no-op in that case, so plain "simple" turns pay nothing extra.
    `tool_categories` is only needed alongside `task_session_id`, so an
    `ask_user` pause can be resumed later with the same tool set.

    `resumable_on_pause` (§2 phase 2) — set only by agent/queue_runner.py.
    A queue session can be displaced by someone else entirely mid-run —
    paused outright or blocked on a resource-tag conflict, agent/
    scheduler.py — while this loop is still live; when that happens,
    this run stashes its in-flight history via agent/hanging.py (kind
    "session_displaced") instead of just abandoning it, so a later tick
    can resume the exact same reasoning rather than starting the task
    over. Chat-lane calls leave this False: a displaced chat session was
    Lin starting a new
    conversation, not something to auto-resume later.
    """
    history = list(messages)
    tool_counts: dict[str, int] = {}
    exact_counts: dict[str, int] = {}
    first_tool = True
    _widened = False

    for _ in range(max_iterations):
        # §2 phase 2: with two sessions genuinely concurrent, this run's
        # own session can be displaced by someone else entirely (agent/
        # scheduler.py pausing it outright, or blocking it on a
        # resource-tag conflict — 'paused' or 'waiting_dependency', never
        # 'waiting_user': that one is this same loop's own ask_user/
        # approval halt, which already returns in the same iteration, no
        # need to notice it a turn later) while this loop is still
        # executing — the DB update alone can't reach into a live
        # coroutine. Checked once per round-trip (not more often —
        # coarse is fine here) so a displaced run notices and stops
        # instead of finishing and calling sessions.complete()/fail(),
        # which would silently overwrite the displacement. Bounded, not
        # instant: at most one more full round of tool calls can land
        # before this is noticed.
        if task_session_id is not None:
            _live = sessions.get(task_session_id)
            if _live and _live["status"] in ("paused", "waiting_dependency"):
                log.info(f"Session #{task_session_id} displaced externally ({_live['status']}) — halting mid-run")
                if resumable_on_pause:
                    hanging.create(
                        "session_displaced",
                        {
                            "task_session_id": task_session_id,
                            "history": history,
                            "tool_categories": tool_categories or [],
                            "max_iterations": max_iterations,
                        },
                        task_session_id=task_session_id,
                    )
                return "", history
        if audit:
            audit.llm_request(message_count=len(history), has_tools=True)
        try:
            response = await generation_chat(history, tools=tools_schema)
        except AllKeysExhausted:
            await bus_client.publish(LLMExhausted(session_id=session_id))
            raise
        except Exception as _e:
            if "maximum context length" in str(_e) or "context_length_exceeded" in str(_e):
                # Trim oldest non-system tool messages and retry once
                trimmed = [m for m in history if m.get("role") == "system"]
                non_sys = [m for m in history if m.get("role") != "system"]
                non_sys = non_sys[max(0, len(non_sys) - 6):]
                history = trimmed + non_sys
                if audit:
                    audit.exception("executor.context_trim", str(_e)[:120])
                try:
                    response = await generation_chat(history, tools=tools_schema)
                except Exception as _e2:
                    raise _e2
            else:
                raise

        msg = response.choices[0].message
        if audit:
            audit.llm_response(
                content=msg.content,
                tool_call_count=len(msg.tool_calls or []),
            )

        if not msg.tool_calls:
            reply = msg.content or ""
            if audit:
                audit.final_reply(reply)
            sessions.complete(task_session_id, result=reply[:300])
            return reply, history

        asst: dict = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        }
        if msg.content:
            asst["content"] = msg.content
        history.append(asst)

        if first_tool:
            await bus_client.publish(WorkStarted(session_id=session_id))
            first_tool = False

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
                log.warning(f"Tool '{name}': malformed args JSON, using empty dict")

            # ask_user (§2 phase 1) — halts the loop like a yellow/red
            # zone call does, but for a free-text question rather than a
            # yes/no. Only meaningful with a task session to pause and
            # resume; without one there's nothing to come back to, so it
            # degrades to answering inline like any other green tool.
            if name == "ask_user" and task_session_id is not None:
                question = str(args.get("question", "")).strip() or "Уточни, пожалуйста."
                history.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": "Ожидаю ответ пользователя...",
                })
                await bus_client.publish(ToolCalled(name=name, args_preview=str(args)[:120]))
                if audit:
                    audit.tool_called(name=name, args=args)
                sessions.wait_user(task_session_id, reason=question)
                sessions.log_decision(task_session_id, "ask_user", question)
                questions.ask(
                    task_session_id, question, history,
                    tool_categories or [], max_iterations,
                )
                if audit:
                    audit.final_reply(question)
                await bus_client.publish(WorkCompleted(session_id=session_id))
                return question, history

            zone = zones.resolve_zone(name, args)
            if zone is not zones.Zone.GREEN:
                preview = approval.preview_for(name, args)
                approval.request(name, args, preview, task_session_id=task_session_id)
                await bus_client.publish(ToolCalled(name=name, args_preview=str(args)[:120]))
                if audit:
                    audit.tool_called(name=name, args=args)
                reply = (
                    f"Нужно подтверждение ({zone.value}-зона):\n{preview}\n\n"
                    "Выполнять? (да/нет)"
                )
                if audit:
                    audit.final_reply(reply)
                sessions.wait_user(task_session_id, reason=preview)
                sessions.log_decision(task_session_id, "approval_pending", preview)
                await bus_client.publish(WorkCompleted(session_id=session_id))
                return reply, history

            exact_key = f"{name}:{json.dumps(args, sort_keys=True)}"
            exact_counts[exact_key] = exact_counts.get(exact_key, 0) + 1
            tool_counts[name] = tool_counts.get(name, 0) + 1

            if exact_counts[exact_key] >= EXACT_DUPLICATE_LIMIT:
                log.warning(f"Loop detected: '{name}' called with identical args twice")
                if audit:
                    audit.loop_detected(name=name, args=args)
                history.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": "Обнаружена петля, останавливаю."}
                )
                await bus_client.publish(WorkCompleted(session_id=session_id))
                reply = "Обнаружила, что хожу по кругу, остановилась."
                if audit:
                    audit.final_reply(reply)
                sessions.fail(task_session_id, "обнаружена петля (повторяющийся вызов инструмента)")
                return reply, history

            await bus_client.publish(ToolCalled(name=name, args_preview=str(args)[:120]))
            if audit:
                audit.tool_called(name=name, args=args)

            if is_side_effect(name) and exact_counts[exact_key] > 1:
                log.warning(
                    f"Idempotency block: '{name}' already called this turn with same args"
                )
                result = DUPLICATE_BLOCK_MESSAGE
                success = True
                await bus_client.publish(ToolFinished(name=name, success=success))
                if audit:
                    audit.idempotency_block(name=name, args=args)
                    audit.tool_finished(name=name, result=result, success=success, duration_ms=0)
                history.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
                continue

            blocked, cooldown_msg = check_cooldown(name)
            if blocked:
                log.warning(f"Cooldown block: '{name}' — {cooldown_msg}")
                result = cooldown_msg or DUPLICATE_BLOCK_MESSAGE
                success = True
                await bus_client.publish(ToolFinished(name=name, success=success))
                if audit:
                    audit.idempotency_block(name=name, args=args)
                    audit.tool_finished(name=name, result=result, success=success, duration_ms=0)
                history.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
                continue

            fn = tools_map.get(name)
            if fn is None and not _widened and full_tools_map and name in full_tools_map:
                log.info(
                    f"Tool '{name}' not in the loaded categories — widening to "
                    "the full tool set for the rest of this run"
                )
                tools_map = full_tools_map
                tools_schema = full_tools_schema or tools_schema
                fn = tools_map.get(name)
                _widened = True

            t_start = time.monotonic()
            if fn is None:
                result = f"Инструмент '{name}' не найден."
                success = False
            else:
                try:
                    coro = (
                        fn(**args)
                        if asyncio.iscoroutinefunction(fn)
                        else asyncio.to_thread(fn, **args)
                    )
                    result = await asyncio.wait_for(coro, timeout=_TOOL_TIMEOUT)
                    success = True
                except asyncio.TimeoutError:
                    result = f"Инструмент '{name}' превысил лимит времени ({_TOOL_TIMEOUT:.0f}с)."
                    success = False
                    log.warning(f"Tool '{name}' timed out after {_TOOL_TIMEOUT}s")
                except Exception as e:
                    result = f"Ошибка: {e}"
                    success = False
                    log.error(f"Tool '{name}' error: {e}", exc_info=True)

            duration_ms = int((time.monotonic() - t_start) * 1000)
            if success:
                mark_called(name)
            await bus_client.publish(ToolFinished(name=name, success=success))
            if audit:
                audit.tool_finished(
                    name=name, result=str(result), success=success, duration_ms=duration_ms,
                )
            history.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
            sessions.log_decision(
                task_session_id, "tool_call",
                f"{name}({json.dumps(args, ensure_ascii=False)}) -> {str(result)[:200]}",
            )

    await bus_client.publish(WorkCompleted(session_id=session_id))
    try:
        final = await generation_chat(history)
        reply = final.choices[0].message.content or "Готово."
        if audit:
            audit.final_reply(reply)
        sessions.complete(task_session_id, result=reply[:300])
        return reply, history
    except AllKeysExhausted:
        raise
    except Exception as e:
        if audit:
            audit.exception("executor.final_chat", str(e))
        sessions.fail(task_session_id, "исчерпан лимит шагов")
        return "Исчерпала лимит шагов.", history
