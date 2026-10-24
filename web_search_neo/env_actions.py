"""env: a browser game (or any page) as a reinforcement-learning environment.

A policy that outputs probabilities over a fixed set of actions - an RL agent, a JEPA-style
world model with a policy head, a small classifier - cannot write web_action JSON and should
not have to. ``env`` gives such a model the loop it expects, on top of the frame-gated game
support (``render`` step mode: the page advances only when a step releases frames):

* ``define`` - the action set (each a name with keys to hold, keys to tap, or a pointer click),
  ``frames_per_step``, what to observe (a small pixel grid of the game element, a state
  script), and ``reward`` / ``done`` JavaScript expressions;
* ``reset`` - open (or reload) the page, freeze its clock in step mode, first observation;
* ``step`` - one action: ``act`` (a name or an index), or ``probs`` (a probability per action, sampled
  or argmaxed here, with temperature), then ``frames_per_step`` frames; answers observation,
  reward, done, the action taken and its probability;
* per-key mode - ``define`` with ``keys`` instead of ``actions``, then each ``step`` says which keys
  are down, the way a person (or an LLM's ``input`` + ``step``) would: ``key_probs`` (one per key,
  each held with that probability, or when >= 0.5 with ``choose='argmax'``) or ``pressed`` (key
  names). ``frames`` on a step overrides ``frames_per_step``: how long to keep them down;
* ``spec`` - action names and observation shape; ``close`` - release inputs, normal mode.

scripts/wsn_env.py wraps this as a Gymnasium-style ``reset()`` / ``step()`` client.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
from typing import Any, Literal

from web_search_neo import browser_tools, env_pixels

_ENVS: dict[str, dict[str, Any]] = {}


def _facade() -> Any:
    return sys.modules["web_search_neo.main"]


def _clean_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list) or not actions or len(actions) > 64:
        raise ValueError("env define needs actions: a list of 1-64 {name, hold?, tap?, click?} objects")
    cleaned = []
    for index, item in enumerate(actions):
        if isinstance(item, str):
            item = {"name": item, "hold": [item]}
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError(f"action {index} needs a name")
        entry = {"name": str(item["name"]), "hold": [str(key) for key in item.get("hold") or []],
                 "tap": [str(key) for key in item.get("tap") or []]}
        click = item.get("click")
        if click is not None:
            if not isinstance(click, dict) or not all(isinstance(click.get(axis), (int, float)) for axis in ("x", "y")):
                raise ValueError(f"action {entry['name']}: click needs numeric x and y (viewport CSS pixels)")
            entry["click"] = {"x": float(click["x"]), "y": float(click["y"])}
        cleaned.append(entry)
    names = [entry["name"] for entry in cleaned]
    if len(set(names)) != len(names):
        raise ValueError("action names must be unique")
    return cleaned


def _choose(env: dict[str, Any], action: Any, probs: Any, choose: str, temperature: float) -> tuple[int, str, float | None]:
    names = [entry["name"] for entry in env["actions"]]
    if probs is not None:
        weights = [float(value) for value in probs]
        if len(weights) != len(names) or any(not math.isfinite(value) or value < 0 for value in weights):
            raise ValueError(f"probs needs {len(names)} non-negative numbers, one per action {names}")
        total = sum(weights)
        if total <= 0:
            raise ValueError("probs must not all be zero")
        weights = [value / total for value in weights]
        if choose == "argmax":
            index = max(range(len(weights)), key=weights.__getitem__)
        else:
            tempered = [value ** (1.0 / max(temperature, 1e-3)) for value in weights]
            index = env["rng"].choices(range(len(names)), weights=tempered)[0]
        return index, ("argmax" if choose == "argmax" else "sample"), weights[index]
    if isinstance(action, bool) or action is None:
        raise ValueError(f"step needs act (an action name or index) or probs; actions: {names}")
    if isinstance(action, (int, float)):
        index = int(action)
        if not 0 <= index < len(names):
            raise ValueError(f"action index {index} is outside 0..{len(names) - 1}")
        return index, "index", None
    if str(action) not in names:
        raise ValueError(f"unknown action {action!r}; actions: {names}")
    return names.index(str(action)), "name", None


def _choose_keys(env: dict[str, Any], key_probs: Any, pressed: Any, choose: str) -> tuple[list[str], list[float] | None]:
    """Per-key mode: the keys held this step, from a probability per key or from their names."""
    keys = env["keys"]
    if pressed is not None:
        wanted = [str(key) for key in (pressed if isinstance(pressed, list) else [pressed])]
        unknown = [key for key in wanted if key not in keys]
        if unknown:
            raise ValueError(f"unknown keys {unknown}; keys: {keys}")
        return [key for key in keys if key in wanted], None
    if key_probs is None:
        raise ValueError(f"step in per-key mode needs key_probs (one per key) or pressed (names); keys: {keys}")
    weights = [float(value) for value in key_probs]
    if len(weights) != len(keys) or any(not math.isfinite(value) or not 0 <= value <= 1 for value in weights):
        raise ValueError(f"key_probs needs {len(keys)} probabilities in 0..1, one per key {keys}")
    if choose == "argmax":
        return [key for key, value in zip(keys, weights) if value >= 0.5], weights
    return [key for key, value in zip(keys, weights) if env["rng"].random() < value], weights


def _script(session_id: str, source: str | None) -> Any:
    if not source:
        return None
    body = source if "return" in source else f"return ({source});"
    answer = browser_tools.execute_js(body, session_id=session_id)
    return answer.get("value")


def _pixels(session_id: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    """The observed element (a canvas by default) captured small, then box-averaged to width x height."""
    selector = str(spec.get("selector") or "canvas")
    rect = browser_tools.execute_js(
        "const nodes = [...document.querySelectorAll(arguments[0])];"
        " const node = nodes.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];"
        " if (!node) return null; const box = node.getBoundingClientRect();"
        " return {x: box.left + scrollX, y: box.top + scrollY, width: box.width, height: box.height};",
        args=[selector], session_id=session_id).get("value")
    if not rect or rect["width"] < 1 or rect["height"] < 1:
        return None
    width, height = int(spec.get("width") or 64), int(spec.get("height") or 64)
    scale = max(0.05, min(1.0, 2.0 * max(width / rect["width"], height / rect["height"])))
    session = browser_tools._get_session(session_id)
    with session.lock:
        capture = session.driver.execute_cdp_cmd("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": True,
            "clip": {"x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"], "scale": scale}})
    import base64
    return env_pixels.shrink(base64.b64decode(capture["data"]), width, height, gray=spec.get("gray", True))


def _observe(session_id: str, env: dict[str, Any]) -> dict[str, Any]:
    observe = env["observe"]
    observation: dict[str, Any] = {}
    if observe.get("script"):
        observation["state"] = _script(session_id, observe["script"])
    if observe.get("pixels"):
        observation["pixels"] = _pixels(session_id, observe["pixels"])
    return observation


def _apply(session_id: str, env: dict[str, Any], entry: dict[str, Any]) -> None:
    """Hold this action's keys (releasing what the last action held), tap, click."""
    held = set(env["held"])
    wanted = set(entry["hold"])
    if held - wanted:
        browser_tools.press_keys(sorted(held - wanted), session_id, action="release", focus_mode="none",
                                 wait_seconds=0, include_summary=False)
    if wanted - held:
        browser_tools.press_keys(sorted(wanted - held), session_id, action="hold", focus_mode="none",
                                 wait_seconds=0, include_summary=False)
    env["held"] = sorted(wanted)
    if entry["tap"]:
        browser_tools.press_keys(entry["tap"], session_id, action="tap", focus_mode="none", wait_seconds=0,
                                 include_summary=False)
    if entry.get("click"):
        browser_tools.pointer_action("click", entry["click"]["x"], entry["click"]["y"], session_id=session_id)


def _step_sync(session_id: str, env: dict[str, Any], entry: dict[str, Any], frames: int) -> dict[str, Any]:
    _apply(session_id, env, entry)
    stepped = browser_tools.render_step(frames, session_id, include_summary=False)
    env["steps"] += 1
    reward = _script(session_id, env.get("reward"))
    done = bool(_script(session_id, env.get("done"))) if env.get("done") else False
    reward = float(reward) if isinstance(reward, (int, float)) and not isinstance(reward, bool) else 0.0
    env["total_reward"] += reward
    return {"observation": _observe(session_id, env), "reward": reward, "done": done,
            "frame": stepped.get("frame") or stepped.get("frames_released"), "step_count": env["steps"],
            "total_reward": round(env["total_reward"], 6)}


async def browser_env(
    op: Literal["define", "reset", "step", "spec", "close"] = "spec",
    session_id: str = "env",
    actions: list[Any] | None = None,
    frames_per_step: int = 4,
    observe: dict[str, Any] | None = None,
    reward: str | None = None,
    done: str | None = None,
    url: str | None = None,
    profile_mode: str = "isolated",
    act: Any = None,
    probs: list[float] | None = None,
    choose: Literal["sample", "argmax"] = "sample",
    temperature: float = 1.0,
    seed: int | None = None,
    keys: list[str] | None = None,
    key_probs: list[float] | None = None,
    pressed: list[str] | None = None,
    frames: int | None = None,
) -> dict[str, Any]:
    """A page as an RL environment: define actions/observation/reward, reset, step (act=name|index, or probs).

    Per-key mode: define keys=[...] instead of actions; step with key_probs (one per key) or pressed (names),
    and frames (how long, default frames_per_step).
    observe: {"pixels": {"selector": "canvas", "width": 64, "height": 64, "gray": true}, "script": "return {...}"};
    reward / done: JavaScript expressions run in the page after each step.
    """
    if op == "define":
        if keys is not None and actions is not None:
            raise ValueError("env define takes actions (named actions) or keys (per-key mode), not both")
        if keys is not None and (not isinstance(keys, list) or not 1 <= len(keys) <= 32
                                 or len({str(key) for key in keys}) != len(keys)):
            raise ValueError("env define keys needs a list of 1-32 distinct key names, e.g. ['LEFT', 'RIGHT', 'SPACE']")
        _ENVS[session_id] = {
            "actions": None if keys is not None else _clean_actions(actions),
            "keys": [str(key) for key in keys] if keys is not None else None,
            "frames_per_step": max(1, min(int(frames_per_step), 120)),
            "observe": dict(observe or {"pixels": {"width": 64, "height": 64, "gray": True}}),
            "reward": reward, "done": done, "url": url, "held": [], "steps": 0, "total_reward": 0.0,
            "rng": random.Random(seed),
        }
        return {"success": True, **_spec(session_id)}
    env = _ENVS.get(session_id)
    if env is None:
        raise ValueError(f"no env defined for session '{session_id}'; call op='define' first")
    if op == "spec":
        return {"success": True, **_spec(session_id)}
    if op == "reset":
        target = url or env.get("url")
        if target:
            opened = await _facade()._execute_actions(
                [{"action": "open", "url": target, "session_id": session_id, "profile_mode": profile_mode}], False, "min")
            if not opened.get("success"):
                raise ValueError(f"could not open {target}: {opened['results'][0].get('error')}")
        await asyncio.to_thread(browser_tools.set_render_control, "step", session_id)
        env.update(held=[], steps=0, total_reward=0.0)
        if seed is not None:
            env["rng"] = random.Random(seed)
        observation = await asyncio.to_thread(_observe, session_id, env)
        return {"success": True, "observation": observation, **_spec(session_id)}
    if op == "step":
        length = max(1, min(int(frames), 120)) if frames is not None else env["frames_per_step"]
        if (env["keys"] is not None) == (act is not None or probs is not None) or (
                env["keys"] is None and (key_probs is not None or pressed is not None)):
            raise ValueError("this env is in " + ("per-key mode: step with key_probs or pressed"
                             if env["keys"] is not None else "actions mode: step with act or probs"))
        if env["keys"] is not None:
            held, weights = _choose_keys(env, key_probs, pressed, choose)
            entry = {"name": "+".join(held) or "none", "hold": held, "tap": []}
            result = await asyncio.to_thread(_step_sync, session_id, env, entry, length)
            return {"success": True, "held": held, "chosen_by": "pressed" if weights is None else choose,
                    "key_probs": weights, "frames": length, **result}
        index, how, probability = _choose(env, act, probs, choose, temperature)
        result = await asyncio.to_thread(_step_sync, session_id, env, env["actions"][index], length)
        return {"success": True, "action": env["actions"][index]["name"], "action_index": index,
                "chosen_by": how, "probability": probability, "frames": length, **result}
    if op == "close":
        await asyncio.to_thread(browser_tools.release_inputs, session_id)
        await asyncio.to_thread(browser_tools.set_render_control, "normal", session_id)
        _ENVS.pop(session_id, None)
        return {"success": True, "closed": session_id}
    raise ValueError(f"env op must be define, reset, step, spec or close, not {op!r}")


def _spec(session_id: str) -> dict[str, Any]:
    env = _ENVS[session_id]
    pixels = env["observe"].get("pixels")
    per_key = env["keys"] is not None
    return {"session_id": session_id, "mode": "keys" if per_key else "actions",
            "actions": None if per_key else [entry["name"] for entry in env["actions"]],
            "keys": env["keys"], "action_count": len(env["keys"] if per_key else env["actions"]),
            "frames_per_step": env["frames_per_step"],
            "observation_spec": {"pixels": ({"width": pixels.get("width", 64), "height": pixels.get("height", 64),
                                        "channels": 1 if pixels.get("gray", True) else 3} if pixels else None),
                            "state": bool(env["observe"].get("script"))},
            "reward": env.get("reward"), "done": env.get("done"), "steps": env["steps"]}
