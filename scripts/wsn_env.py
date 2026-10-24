"""A Gymnasium-style client for web-search-neo's ``env`` action - for policies that output probabilities.

An RL policy, a JEPA-style world model with a policy head or any classifier over a fixed action
set drives a browser game (or any page) through a running daemon (``mcp_cli.py serve``)::

    from wsn_env import WsnEnv
    env = WsnEnv(url="http://127.0.0.1:8797/platformer.html",
                 actions=[{"name": "idle"}, {"name": "right", "hold": ["RIGHT"]},
                          {"name": "jump", "hold": ["RIGHT"], "tap": ["SPACE"]}],
                 reward="window.__game.x / 100", done="window.__game.won || window.__game.deaths > 0",
                 observe={"pixels": {"selector": "#game", "width": 64, "height": 48},
                          "script": "return {x: window.__game.x, onGround: window.__game.onGround}"})
    obs, info = env.reset(seed=1)
    obs, reward, terminated, truncated, info = env.step(probs=[0.1, 0.6, 0.3])   # sampled by the server
    obs, reward, terminated, truncated, info = env.step(2)                       # or an index / a name
    env.close()

Per-key mode - the model says which keys are down, and for how long::

    env = WsnEnv(url=..., keys=["LEFT", "RIGHT", "SPACE"], observe={...}, reward=..., done=...)
    env.reset()
    env.step(key_probs=[0.05, 0.9, 0.4])         # each key held with its probability (choose="argmax": >= 0.5)
    env.step(pressed=["RIGHT", "SPACE"], frames=8)  # or by name, for 8 frames

``obs`` is a dict: ``pixels`` (a numpy uint8 array H x W [x 3] when numpy is installed, else bytes) and
``state`` (what the observe script returned). The page only advances when ``step`` releases frames, so
the model may think as long as it likes. ``gym_env(...)`` returns a ``gymnasium.Env`` (Discrete actions,
Box pixels) when gymnasium is installed.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_cli  # noqa: E402

DEFAULT_PORT = 47812


class EnvError(RuntimeError):
    pass


class WsnEnv:
    def __init__(self, *, actions: list[Any] | None = None, keys: list[str] | None = None,
                 url: str | None = None, session_id: str = "env",
                 frames_per_step: int = 4, observe: dict[str, Any] | None = None, reward: str | None = None,
                 done: str | None = None, port: int = DEFAULT_PORT, profile_mode: str = "isolated",
                 max_steps: int | None = None) -> None:
        self.port, self.session_id, self.profile_mode, self.max_steps = port, session_id, profile_mode, max_steps
        self.url = url
        self.spec = self._call(op="define", actions=actions, keys=keys, frames_per_step=frames_per_step,
                               observe=observe, reward=reward, done=done, url=url)
        self.keys: list[str] | None = self.spec.get("keys")
        self.action_names: list[str] = self.spec["actions"] or []

    def _call(self, **params: Any) -> dict[str, Any]:
        params = {key: value for key, value in params.items() if value is not None}
        answer = mcp_cli.send(self.port, {"tool": "web_action", "args": {
            "actions": [{"action": "env", "session_id": self.session_id, **params}]}}, timeout=600.0)
        try:
            result = answer["content"][0]["value"]["results"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise EnvError(f"unexpected answer: {answer}") from exc
        if not result.get("success"):
            raise EnvError(result.get("error") or "env call failed")
        return result["data"]

    @staticmethod
    def _observation(raw: dict[str, Any]) -> dict[str, Any]:
        pixels = raw.get("pixels")
        if pixels:
            data = base64.b64decode(pixels["data"])
            try:
                import numpy as np
                shape = (pixels["height"], pixels["width"]) + ((pixels["channels"],) if pixels["channels"] > 1 else ())
                data = np.frombuffer(data, dtype=np.uint8).reshape(shape)
            except ImportError:
                pass
            return {**raw, "pixels": data}
        return raw

    def reset(self, *, seed: int | None = None, url: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        self.steps = 0
        data = self._call(op="reset", url=url or self.url, seed=seed, profile_mode=self.profile_mode)
        return self._observation(data["observation"]), {"spec": {key: value for key, value in data.items()
                                                                 if key not in ("observation", "success")}}

    def step(self, action: int | str | None = None, *, probs: list[float] | None = None,
             key_probs: list[float] | None = None, pressed: list[str] | None = None,
             frames: int | None = None, choose: str = "sample", temperature: float = 1.0):
        data = self._call(op="step", act=action, probs=probs, key_probs=key_probs, pressed=pressed,
                          frames=frames, choose=choose, temperature=temperature)
        self.steps += 1
        truncated = bool(self.max_steps and self.steps >= self.max_steps and not data["done"])
        info = {key: data[key] for key in ("action", "action_index", "held", "chosen_by", "probability",
                                           "key_probs", "frames", "frame", "step_count", "total_reward")
                if key in data}
        return self._observation(data["observation"]), data["reward"], data["done"], truncated, info

    def close(self) -> None:
        try:
            self._call(op="close")
        except EnvError:
            pass


def gym_env(**kwargs: Any):
    """The same environment as a gymnasium.Env: Discrete actions (MultiBinary keys in per-key mode), Box uint8
    pixels; needs gymnasium + numpy."""
    import gymnasium as gym
    import numpy as np

    class _GymEnv(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self) -> None:
            self.inner = WsnEnv(**kwargs)
            pixels = self.inner.spec["observation_spec"]["pixels"] or {"width": 64, "height": 64, "channels": 1}
            shape = (pixels["height"], pixels["width"]) + ((pixels["channels"],) if pixels["channels"] > 1 else ())
            keys = self.inner.keys
            self.action_space = (gym.spaces.MultiBinary(len(keys)) if keys
                                 else gym.spaces.Discrete(len(self.inner.action_names)))
            self.observation_space = gym.spaces.Box(0, 255, shape=shape, dtype=np.uint8)

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            observation, info = self.inner.reset(seed=seed)
            return observation["pixels"], {**info, "state": observation.get("state")}

        def step(self, action):
            if self.inner.keys:
                down = [key for key, bit in zip(self.inner.keys, action) if int(bit)]
                observation, reward, terminated, truncated, info = self.inner.step(pressed=down)
            else:
                observation, reward, terminated, truncated, info = self.inner.step(int(action))
            return observation["pixels"], reward, terminated, truncated, {**info, "state": observation.get("state")}

        def close(self):
            self.inner.close()

    return _GymEnv()
