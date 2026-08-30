"""Plugin registry tests: no Chrome needed - registration and schema wiring only."""

import asyncio
from pathlib import Path

import pytest

from web_search_neo import main, msp_search, plugins

PLUGIN_SOURCE = '''
from web_search_neo import plugins


async def echo_ping(text: str = "pong") -> dict:
    """Echo text back for plugin testing."""
    return {"echo": text}


plugins.register_action("echo_ping", echo_ping, group="plugin-test", summary="Test echo action.")


async def plugin_probe(limit: int = 3) -> dict:
    """Probe topic for plugin tests."""
    return {"limit": limit, "ok": True}


plugins.register_topic("plugin_probe", plugin_probe)
'''

BROKEN_MODULE_SOURCE = 'raise ValueError("boom in module")'


@pytest.fixture()
def clean_registries():
    actions_before = dict(main._ACTIONS)
    topics_before = dict(main._TOPIC_HANDLERS)
    info_before = dict(main._INFO_TOPICS)
    providers_before = dict(msp_search.SEARCH_PROVIDERS)
    order_before = list(msp_search.ENGINE_ORDER)
    status_cache_before = msp_search._status_cache
    plugins.reset_loaded()

    yield

    main._ACTIONS.clear()
    main._ACTIONS.update(actions_before)
    main._TOPIC_HANDLERS.clear()
    main._TOPIC_HANDLERS.update(topics_before)
    main._INFO_TOPICS.clear()
    main._INFO_TOPICS.update(info_before)
    msp_search.SEARCH_PROVIDERS.clear()
    msp_search.SEARCH_PROVIDERS.update(providers_before)
    msp_search.ENGINE_ORDER[:] = order_before
    msp_search._status_cache = status_cache_before


def _write_plugin(tmp_path: Path, source: str = PLUGIN_SOURCE) -> Path:
    path = tmp_path / "plugin_under_test.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_register_seams_do_not_fork_the_registries() -> None:
    """The plugin provider seam must land in the real registry."""
    assert plugins.register_search_provider is msp_search.register_search_provider
    assert "duckduckgo" in msp_search.SEARCH_PROVIDERS  # sanity: default engines present


def test_plugin_search_provider_lands_in_registry(clean_registries) -> None:
    def fake_engine(query: str, num: int, timeout_seconds: float):
        return [{"title": "t", "url": f"https://x/{i}", "snippet": "s"} for i in range(num)]

    provider = msp_search.FunctionSearchProvider(
        name="plugin_test_engine", implementation=fake_engine
    )
    plugins.register_search_provider(provider)
    assert msp_search.SEARCH_PROVIDERS["plugin_test_engine"] is provider
    assert "plugin_test_engine" in msp_search.ENGINE_ORDER


def test_load_plugin_registers_action_and_topic(
    tmp_path: Path, monkeypatch, clean_registries
) -> None:
    path = _write_plugin(tmp_path)
    monkeypatch.setenv(plugins.ENV_VAR, str(path))

    loaded = plugins.load_plugins()

    assert loaded == [str(path)]
    spec = main._ACTIONS["echo_ping"]
    assert spec.group == "plugin-test" and spec.summary == "Test echo action."
    # The schema is generated like any built-in: one optional parameter.
    model = main.legacy_mcp._tool_manager._tools["echo_ping"].fn_metadata.arg_model
    fields = list(model.model_fields)
    assert fields == ["text"] and not model.model_fields["text"].is_required()

    assert "plugin_probe" in main._TOPIC_HANDLERS
    assert main._INFO_TOPICS["plugin_probe"] == "Probe topic for plugin tests."


def test_web_action_dispatches_plugin_action(tmp_path: Path, monkeypatch, clean_registries):
    path = _write_plugin(tmp_path)
    monkeypatch.setenv(plugins.ENV_VAR, str(path))
    plugins.load_plugins()

    result = asyncio.run(main.web_action(actions=[{"action": "echo_ping", "text": "hello"}]))
    assert result["success"] is True
    assert result["results"][0]["data"]["echo"] == "hello"


def test_plugin_action_refuses_unknown_parameter(
    tmp_path: Path, monkeypatch, clean_registries
) -> None:
    path = _write_plugin(tmp_path)
    monkeypatch.setenv(plugins.ENV_VAR, str(path))
    plugins.load_plugins()

    result = asyncio.run(main.web_action(actions=[{"action": "echo_ping", "wrong_key": 1}]))
    assert result["success"] is False
    assert "unknown parameter" in result["results"][0]["error"].lower()


def test_web_info_dispatches_plugin_topic_and_publishes_it(
    tmp_path: Path, monkeypatch, clean_registries
) -> None:
    path = _write_plugin(tmp_path)
    monkeypatch.setenv(plugins.ENV_VAR, str(path))
    plugins.load_plugins()

    payload = asyncio.run(main.web_info(topic="plugin_probe", params={"limit": 7}))
    assert payload["limit"] == 7 and payload["ok"] is True

    # The compact surface re-published web_info with the new topic allowed.
    model = main.mcp._tool_manager._tools["web_info"].fn_metadata.arg_model
    annotation = model.model_fields["topic"].annotation
    allowed = set(getattr(annotation, "__args__", ())) or set(
        str(v) for v in getattr(annotation, "args", ())
    )
    assert "plugin_probe" in {str(value) for value in allowed}


def test_register_action_collision_and_overwrite(clean_registries) -> None:
    async def first() -> dict:
        return {"n": 1}

    async def second() -> dict:
        return {"n": 2}

    try:
        plugins.register_action("collision_probe", first, group="plugin-test")
        with pytest.raises(ValueError, match="overwrite"):
            plugins.register_action("collision_probe", second)
        assert asyncio.run(main._ACTIONS["collision_probe"].handler()) == {"n": 1}
        plugins.register_action("collision_probe", second, overwrite=True)
        assert asyncio.run(main._ACTIONS["collision_probe"].handler()) == {"n": 2}
    finally:
        main._ACTIONS.pop("collision_probe", None)


def test_load_plugins_fail_fast_and_idempotent(tmp_path: Path, monkeypatch, clean_registries):
    broken_dir = tmp_path / "broken_dir"
    broken_dir.mkdir()
    (broken_dir / "bad.py").write_text(BROKEN_MODULE_SOURCE, encoding="utf-8")
    monkeypatch.setenv(plugins.ENV_VAR, str(broken_dir))

    with pytest.raises(RuntimeError) as excinfo:
        plugins.load_plugins()
    assert "failed to load" in str(excinfo.value) and "boom in module" in str(excinfo.value)

    good = tmp_path / "good.py"
    good.write_text("def register():\n    pass\n", encoding="utf-8")
    monkeypatch.setenv(plugins.ENV_VAR, str(good))
    loaded = plugins.load_plugins()
    assert loaded == [str(good)]
    # Same origin twice: the second load is a no-op.
    assert plugins.load_plugins() == []

    missing = tmp_path / "never_there.py"
    monkeypatch.setenv(plugins.ENV_VAR, str(missing))
    with pytest.raises(RuntimeError, match="failed to load"):
        plugins.load_plugins()


def test_load_plugins_unconfigured_is_empty(monkeypatch, clean_registries) -> None:
    monkeypatch.delenv(plugins.ENV_VAR, raising=False)
    assert plugins.load_plugins() == []
