"""Повтор шага в диспетчере: только наблюдение, только когда страница просит подождать.

``web_action``, повтор макроса и ``test_run`` идут одним циклом (dispatch), поэтому
здесь проверяется именно он: сколько раз шаг был попытан, что помечает отказ
повторять и что выключает это целиком (WEB_SEARCH_NEO_RETRY=0).
Шаг подкладывается чужим обработчиком - без Chrome и без сети.
"""

from __future__ import annotations

import asyncio
from typing import Any

import web_search_neo.browser_tools as browser_tools
import web_search_neo.main as main
from web_search_neo import dispatch, obstacle_recovery


class _Facade:
    """Минимальная замена main: тот же реестр и те же помощники, что читает dispatch.

    Имя шага подкладывается намеренно: `submit` пишет и отправляет, поэтому повтор
    ему запрещён, `wait` только наблюдает - ровно то различие, которое dispatch
    должен соблюсти.
    """

    _ACTION_NAME_ALIASES = {}

    def __init__(self, handlers: dict[str, Any]) -> None:
        class _Spec:
            group = "page"
            tool_name = "browser_wait"

            def __init__(self, handler: Any) -> None:
                self.handler = handler

        self._ACTIONS = {name: _Spec(handler) for name, handler in handlers.items()}

    def _unsupported_action_error(self, action_name: str, arguments: dict[str, Any]) -> str:
        return f"Unknown action: {action_name}"

    def _argument_model(self, tool_name: str) -> Any:
        class _Model:
            model_fields = {"session_id": type("F", (), {"default": "default"})()}

        return _Model

    def _validate_arguments(self, tool_name: str, what: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)

    def _step_session(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        return arguments.get("session_id")


def _run(handler: Any, actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Прогнать шаги через настоящий dispatch; handler(номер попытки) - что вернуть."""
    calls = {"n": 0}

    async def counted_handler(**_arguments: Any) -> Any:
        # Обработчик вызывается заново на каждой попытке - именно так dispatch
        # повторяет шаг, и счётчик попыток есть то, что здесь проверяется.
        calls["n"] += 1
        return await handler(calls["n"])

    async def _observe(action_name: str, session_id: str | None, step: Any) -> Any:
        # dispatch передаёт уже запущенный вызов обработчика - так же, как его
        # запускает настоящий tab_actions.observe_step.
        return await step

    facade = _Facade({name: counted_handler for name in {str(a["action"]) for a in actions}})
    original = dispatch.tab_actions.observe_step
    dispatch.tab_actions.observe_step = _observe
    try:
        result = asyncio.run(dispatch.execute_actions(facade, actions, continue_on_error=True))
    finally:
        dispatch.tab_actions.observe_step = original
    result["attempts_made"] = calls["n"]
    return result


async def _no_wait(_seconds: float) -> None:
    """Пауза повтора в тесте не нужна: проверяется правило, а не длительность ожидания."""
    return None


def test_an_observing_step_is_tried_again_after_the_page_asked_to_wait(monkeypatch):
    async def handler(attempt: int) -> dict[str, Any]:
        if attempt == 1:
            return {"success": False, "error": "element still loading, please wait"}
        return {"success": True, "waited": True}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "retry-session"}])
    step = result["results"][0]
    assert result["success"] is True
    assert result["attempts_made"] == 2
    assert step["attempts"] == 2
    assert "retry_stopped" not in step


def test_a_step_that_changes_the_page_is_attempted_once_and_says_why_it_stopped(monkeypatch):
    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": False, "error": "timed out waiting for the click"}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    # submit не наблюдает: его повтор отправил бы форму второй раз.
    result = _run(handler, [{"action": "submit", "session_id": "submit-session"}])
    step = result["results"][0]
    assert step["success"] is False
    assert "attempts" not in step
    assert result["attempts_made"] == 1
    assert step["retry_stopped"]["why"] == "action_changes_page_state"


def test_the_same_failure_without_a_wait_from_the_page_is_not_tried_a_third_time(monkeypatch):
    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": False, "error": "connection reset"}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "reset-session"}])
    step = result["results"][0]
    assert result["attempts_made"] == 2
    assert step["retry_stopped"]["why"] == "same_failure_without_a_wait"
    assert step["retry_stopped"]["attempts_used"] == 2


def test_a_step_that_raises_without_asking_to_wait_is_tried_once_more_and_stops(monkeypatch):
    async def handler(attempt: int) -> dict[str, Any]:
        raise RuntimeError("the tab is gone")

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "dead-tab-session"}])
    step = result["results"][0]
    # Страница ничего не просила — второй попытки хватает, третьей dispatch не даёт.
    assert result["attempts_made"] == 2
    assert step["error"].startswith("RuntimeError:")
    assert step["retry_stopped"]["why"] == "same_failure_without_a_wait"


def test_the_ceiling_ends_the_repeats_even_while_the_page_keeps_asking(monkeypatch):
    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": False, "error": "please wait"}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "patient-session"}])
    # первая попытка плюс RETRY_LIMIT_DEFAULT повторов - и ни одной больше.
    assert result["attempts_made"] == 1 + obstacle_recovery.RETRY_LIMIT_DEFAULT
    assert result["results"][0]["retry_stopped"]["why"] == "retry_limit_reached"
    assert result["results"][0]["retry_stopped"]["retry_limit"] == 1 + obstacle_recovery.RETRY_LIMIT_DEFAULT


def test_the_retry_is_off_when_the_environment_says_so(monkeypatch):
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "0")
    assert dispatch.retry_limit() == 1

    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": False, "error": "please wait"}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "no-retry-session"}])
    assert result["attempts_made"] == 1
    assert result["results"][0]["retry_stopped"]["why"] == "retry_limit_reached"


def test_an_environment_value_names_the_ceiling_and_a_silly_one_is_clamped(monkeypatch):
    # Число в переменной среды — это разрешённые ПОВТОРЫ; первая попытка не считается.
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "4")
    assert dispatch.retry_limit() == 5
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "900")
    assert dispatch.retry_limit() == 1 + obstacle_recovery.RETRY_LIMIT_MAX
    for off in ("off", "no", "0"):
        monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, off)
        assert dispatch.retry_limit() == 1
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "plenty")
    assert dispatch.retry_limit() == 1 + obstacle_recovery.RETRY_LIMIT_DEFAULT
    monkeypatch.delenv(dispatch.RETRY_LIMIT_ENV)
    assert dispatch.retry_limit() == 1 + obstacle_recovery.RETRY_LIMIT_DEFAULT


def test_a_higher_ceiling_from_the_environment_is_used_by_the_loop(monkeypatch):
    monkeypatch.setenv(dispatch.RETRY_LIMIT_ENV, "3")

    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": False, "error": "please wait"}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "three-session"}])
    assert result["attempts_made"] == 4
    assert result["results"][0]["retry_stopped"]["retry_limit"] == 4


def test_every_step_reports_what_it_cost_even_when_it_succeeded_first_time():
    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": True}

    result = _run(handler, [{"action": "wait", "session_id": "timed-session"}])
    step = result["results"][0]
    assert isinstance(step["duration_ms"], int) and step["duration_ms"] >= 0
    assert "attempts" not in step


def test_a_step_that_raises_is_retried_only_if_it_observed_and_the_page_asked(monkeypatch):
    async def handler(attempt: int) -> dict[str, Any]:
        if attempt == 1:
            raise RuntimeError("page still loading, please wait")
        return {"success": True}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "raise-session"}])
    step = result["results"][0]
    assert step["success"] is True
    assert step["attempts"] == 2


def test_the_retry_rule_lives_in_the_shared_loop_not_in_a_caller():
    # web_action, повтор макроса и test_run вызывают один и тот же цикл dispatch:
    # правило повтора живёт в нём, а не в трёх местах, которые могли бы разойтись.
    facade_source = open(main.__file__, encoding="utf-8").read()
    loop_source = open(dispatch.__file__, encoding="utf-8").read()
    assert "_execute_actions" in facade_source
    assert "retry_stopped" not in facade_source
    assert "retry_stopped" in loop_source
    assert "obstacle_recovery.retry_plan" in loop_source


def test_an_action_the_facade_registry_does_not_have_is_never_retried():
    async def handler(attempt: int) -> dict[str, Any]:
        return {"success": True}

    calls = {"n": 0}

    async def counted_handler(**_arguments: Any) -> Any:
        calls["n"] += 1
        return await handler(calls["n"])

    async def _observe(action_name: str, session_id: str | None, step: Any) -> Any:
        return await step

    # Обработчик подставлен под `wait`. Шага с именем, которого нет ни в реестре
    # facade, ни в его псевдонимах, dispatch не запускает вовсе - и не повторяет.
    facade = _Facade({"wait": counted_handler})
    original = dispatch.tab_actions.observe_step
    dispatch.tab_actions.observe_step = _observe
    try:
        result = asyncio.run(dispatch.execute_actions(
            facade, [{"action": "no_such_action", "session_id": "unknown-session"}],
            continue_on_error=True))
    finally:
        dispatch.tab_actions.observe_step = original
    assert calls["n"] == 0
    assert result["results"][0]["success"] is False
    assert "retry_stopped" not in result["results"][0]


def test_the_presence_signal_is_left_alone_by_the_retry(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(browser_tools, "note_agent_activity", lambda *args: seen.append(args) or True)

    async def handler(attempt: int) -> dict[str, Any]:
        if attempt == 1:
            return {"success": False, "error": "please wait"}
        return {"success": True}

    monkeypatch.setattr(dispatch.asyncio, "sleep", _no_wait)
    result = _run(handler, [{"action": "wait", "session_id": "presence-retry"}])
    assert result["success"] is True
    # Каждая попытка видна на вкладке: первая как неудача, вторая как успех.
    assert [item[3] for item in seen] == [False, True]
