"""The env action's pure parts: action sets, choosing from probabilities, and the pixel observation."""

from __future__ import annotations

import asyncio
import base64
import random
import struct
import zlib

import pytest

from web_search_neo import env_actions, env_pixels


def _png(width: int, height: int, pixel) -> bytes:
    """A real RGB PNG (filter 0 rows), the shape Chrome's captures have."""
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def test_a_capture_shrinks_to_a_box_averaged_gray_grid():
    png = _png(8, 4, lambda x, y: (255, 255, 255) if x < 4 else (0, 0, 0))
    width, height, channels, raw = env_pixels.decode_png(png)
    assert (width, height, channels, len(raw)) == (8, 4, 3, 96)
    small = env_pixels.shrink(png, 2, 2)
    assert small["width"] == 2 and small["height"] == 2 and small["channels"] == 1
    assert list(base64.b64decode(small["data"])) == [255, 0, 255, 0]
    rgb = env_pixels.shrink(png, 1, 1, gray=False)
    assert list(base64.b64decode(rgb["data"])) == [127, 127, 127]


def test_an_unsupported_png_is_refused():
    with pytest.raises(ValueError):
        env_pixels.decode_png(b"GIF89a")


def test_actions_are_checked_and_strings_become_holds():
    cleaned = env_actions._clean_actions(["RIGHT", {"name": "jump", "tap": ["SPACE"]},
                                          {"name": "fire", "click": {"x": 10, "y": 20}}])
    assert cleaned[0] == {"name": "RIGHT", "hold": ["RIGHT"], "tap": []}
    assert cleaned[2]["click"] == {"x": 10.0, "y": 20.0}
    for bad in ([], [{"hold": ["A"]}], [{"name": "a"}, {"name": "a"}], [{"name": "c", "click": {"x": 1}}]):
        with pytest.raises(ValueError):
            env_actions._clean_actions(bad)


def test_a_policy_s_probabilities_pick_the_action():
    env = {"actions": env_actions._clean_actions(["a", "b", "c"]), "rng": random.Random(3)}
    assert env_actions._choose(env, None, [0.1, 0.7, 0.2], "argmax", 1.0) == (1, "argmax", 0.7)
    picks = [env_actions._choose(env, None, [0.0, 0.0, 5.0], "sample", 1.0)[0] for _ in range(20)]
    assert picks == [2] * 20  # a zero probability is never sampled; weights need not sum to 1
    sampled = {env_actions._choose(env, None, [1, 1, 1], "sample", 1.0)[0] for _ in range(60)}
    assert sampled == {0, 1, 2}
    assert env_actions._choose(env, "c", None, "sample", 1.0) == (2, "name", None)
    assert env_actions._choose(env, 1, None, "sample", 1.0) == (1, "index", None)
    for act, probs in ((None, None), ("zzz", None), (7, None), (None, [1, 2]), (None, [0, 0, 0]), (None, [-1, 1, 1])):
        with pytest.raises(ValueError):
            env_actions._choose(env, act, probs, "sample", 1.0)


def test_step_before_define_says_what_to_do():
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(env_actions.browser_env(op="step", session_id="nowhere", act=0))
    assert "op='define' first" in str(excinfo.value)


def test_define_and_spec_describe_the_space():
    defined = asyncio.run(env_actions.browser_env(
        op="define", session_id="spec-test", actions=["L", "R"], frames_per_step=500,
        observe={"pixels": {"width": 32, "height": 24, "gray": False}, "script": "return 1"}))
    assert defined["actions"] == ["L", "R"] and defined["frames_per_step"] == 120
    assert defined["observation_spec"] == {"pixels": {"width": 32, "height": 24, "channels": 3}, "state": True}
    assert asyncio.run(env_actions.browser_env(op="spec", session_id="spec-test"))["action_count"] == 2


def test_per_key_mode_holds_each_key_by_its_own_probability():
    env = {"keys": ["LEFT", "RIGHT", "SPACE"], "rng": random.Random(5)}
    assert env_actions._choose_keys(env, [0.1, 0.9, 0.5], None, "argmax") == (["RIGHT", "SPACE"], [0.1, 0.9, 0.5])
    assert env_actions._choose_keys(env, None, ["SPACE", "LEFT"], "sample") == (["LEFT", "SPACE"], None)
    sampled = [env_actions._choose_keys(env, [0.0, 1.0, 0.5], None, "sample")[0] for _ in range(80)]
    assert all("RIGHT" in held and "LEFT" not in held for held in sampled)
    assert {"SPACE" in held for held in sampled} == {True, False}
    for probs, pressed in (([0.5, 0.5], None), ([0.1, 1.2, 0.0], None), (None, ["UP"]), (None, None)):
        with pytest.raises(ValueError):
            env_actions._choose_keys(env, probs, pressed, "sample")


def test_define_in_per_key_mode_and_mixing_modes_is_refused():
    spec = asyncio.run(env_actions.browser_env(op="define", session_id="keys-test", keys=["LEFT", "RIGHT"]))
    assert spec["mode"] == "keys" and spec["keys"] == ["LEFT", "RIGHT"] and spec["actions"] is None
    for bad in ({"act": 0}, {"probs": [0.5, 0.5]}, {}):
        with pytest.raises(ValueError) as excinfo:
            asyncio.run(env_actions.browser_env(op="step", session_id="keys-test", **bad))
        assert "per-key mode" in str(excinfo.value)
    with pytest.raises(ValueError):
        asyncio.run(env_actions.browser_env(op="define", session_id="keys-test", keys=["A"], actions=["B"]))
    asyncio.run(env_actions.browser_env(op="define", session_id="keys-test", actions=["L", "R"]))
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(env_actions.browser_env(op="step", session_id="keys-test", key_probs=[1.0, 0.0]))
    assert "actions mode" in str(excinfo.value)
