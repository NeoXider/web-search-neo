"""Word-shape matching for image-selection captchas, with no OCR and no model.

The challenge hands us two things: the target word (as text) and a rendered picture of that
word on top of a background. Instead of recognising glyphs, compare shapes: render the target
word in the captcha's own font and correlate it against the colour mask extracted from the
picture. Two independent shape views (IoU of the binary masks and a normalized-correlation
score) must agree, otherwise the match is reported as unsure - the same rule the slider
solver uses when its measurement is weak.

numpy is required (it is what measures); Pillow is only needed to decode bytes.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

import numpy as np

# The captcha renders its instruction in the same font it uses for the word on the picture,
# so a bundled copy of that font makes the comparison local and reproducible.
FONT_CANDIDATES = (
    "word_click_font.ttf",
    "fonts/cjk-bold.ttf",
)

# When no bundled font ships with the repo, a system CJK face is the next honest source:
# without one, Pillow's default face has no CJK glyphs (tofu boxes) and every score is noise.
CJK_SYSTEM_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)

MIN_AGREE = 0.55  # both views must clear this before a candidate is offered
MIN_LEAD = 0.08  # the winner must beat the runner-up by this much to be trusted

_GLYPH_CACHE: dict[tuple[str, int], np.ndarray] = {}


@dataclass
class ShapeMatch:
    """One candidate region of the picture and how well its shape fits the target word."""

    box: tuple[int, int, int, int]
    iou: float
    correlation: float
    score: float
    evidence: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.iou >= MIN_AGREE and self.correlation >= MIN_AGREE


def glyph_mask(word: str, size: tuple[int, int], font_path: str | None = None) -> np.ndarray:
    """Render `word` as a binary mask at `size`, cached by (word, size, font)."""
    key = (word, size, font_path or "")
    cached = _GLYPH_CACHE.get(key)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw, ImageFont  # optional dependency, imported lazily

    width, height = size
    canvas = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(canvas)
    font = _load_font(word, height, font_path)
    draw.text((0, 0), word, fill=255, font=font)
    mask = np.asarray(canvas, dtype=np.uint8) > 128
    if not mask.any():
        return mask
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    cropped = mask[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]
    resized = Image.fromarray((cropped * 255).astype(np.uint8)).resize((width, height))
    out = np.asarray(resized, dtype=np.uint8) > 128
    _GLYPH_CACHE[key] = out
    return out


def _load_font(word: str, height: int, font_path: str | None):
    from PIL import ImageFont

    candidates = [font_path] if font_path else list(FONT_CANDIDATES)
    here = os.path.dirname(os.path.abspath(__file__))
    for name in candidates:
        if not name:
            continue
        path = name if os.path.isabs(name) else os.path.join(here, name)
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, height)
            except OSError:
                continue
    if not font_path:
        # No bundled face: a system CJK face keeps the glyph real; the default
        # face would draw tofu boxes for CJK and every comparison would be noise.
        for path in CJK_SYSTEM_FONTS:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, height)
                except OSError:
                    continue
    # No CJK face at all: the default face still gives a usable shape for CJK blocks.
    return ImageFont.load_default()


def font_note(font_path: str | None = None) -> str:
    """Which face glyph_mask actually renders with: a path, or 'fallback'."""
    if font_path and os.path.exists(font_path):
        return os.path.basename(font_path)
    here = os.path.dirname(os.path.abspath(__file__))
    for name in list(FONT_CANDIDATES):
        if os.path.exists(os.path.join(here, name)):
            return os.path.basename(name)
    for path in CJK_SYSTEM_FONTS:
        if os.path.exists(path):
            return os.path.basename(path)
    return "fallback"


def color_mask(pixels: np.ndarray, hue: tuple[float, float], sat_min: float) -> np.ndarray:
    """Binary mask of the pixels whose hue falls in `hue` and is saturated enough.

    Captcha words are drawn in one flat colour over a unrelated background, so the target's
    own colour is the cheapest reliable separator - no model needed to find it.
    """
    import colorsys

    rgb = pixels.astype(np.float32) / 255.0
    maxc = rgb.max(axis=2)
    minc = rgb.min(axis=2)
    delta = maxc - minc
    value = maxc
    with np.errstate(invalid="ignore", divide="ignore"):
        hue_deg = np.zeros(value.shape, dtype=np.float32)
        channel = rgb.argmax(axis=2)
        for index in range(3):
            selected = channel == index
            if not selected.any():
                continue
            other_a = rgb[..., (index + 1) % 3]
            other_b = rgb[..., (index + 2) % 3]
            offset = np.where(selected, (other_a - other_b) / delta, 0.0)
            hue_deg = np.where(selected, offset + index * 6.0, hue_deg)
        hue_deg = np.mod(hue_deg / 6.0, 1.0)
        saturation = np.where(value > 0, delta / value, 0.0)
    low, high = hue
    if low <= high:
        in_hue = (hue_deg >= low) & (hue_deg <= high)
    else:  # the band wraps through zero (a red word sits at both ends of the wheel)
        in_hue = (hue_deg >= low) | (hue_deg <= high)
    return in_hue & (saturation >= sat_min) & (value >= 0.25)


def _iou(a: bool | np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union else 0.0


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    left = a.astype(np.float32) - a.mean()
    right = b.astype(np.float32) - b.mean()
    norm = float(np.sqrt((left * left).sum() * (right * right).sum()))
    return float((left * right).sum() / norm) if norm else 0.0


def candidate_boxes(mask: np.ndarray, *, min_area: int = 400) -> list[tuple[int, int, int, int]]:
    """Connected components of the colour mask as (x, y, w, h), largest first.

    Neighbours are 8-connected: a glyph drawn with anti-aliasing touches itself diagonally,
    and 4-connectivity would split one word into several candidates that then compete.
    """
    height, width = mask.shape
    seen = np.zeros((height, width), dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    stack: list[tuple[int, int]] = []
    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y, start_x] or seen[start_y, start_x]:
                continue
            seen[start_y, start_x] = True
            stack.append((start_x, start_y))
            min_x = max_x = start_x
            min_y = max_y = start_y
            area = 0
            while stack:
                x, y = stack.pop()
                area += 1
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for nx in (x - 1, x + 1):
                    for ny in (y - 1, y + 1):
                        if 0 <= nx < width and 0 <= ny < height and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((nx, ny))
            w = max_x - min_x + 1
            h = max_y - min_y + 1
            if area >= min_area:
                boxes.append((min_x, min_y, w, h))
    boxes.sort(key=lambda box: box[2] * box[3], reverse=True)
    return _without_duplicates(boxes)


def _without_duplicates(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Same component reported twice is one candidate, not two competing ones."""
    unique: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for box in boxes:
        if box in seen:
            continue
        seen.add(box)
        unique.append(box)
    return unique


def split_cells(
    img_w: int, img_h: int, click_cfg: dict | None = None, *, cols: int = 3, rows: int = 2
) -> list[tuple[int, int, int, int]]:
    """Tile the picture into the grid the challenge publishes.

    Named select_regions in click_cfg win when valid; otherwise an even cols x rows
    split (image_select ships 3x2) that tiles the picture with no pixel in two cells.
    """
    regions = None
    if isinstance(click_cfg, dict):
        regions = click_cfg.get("select_regions") or click_cfg.get("regions")
    if isinstance(regions, list) and regions:
        boxes: list[tuple[int, int, int, int]] = []
        for region in regions:
            try:
                x, y, w, h = (int(value) for value in list(region)[:4])
            except (TypeError, ValueError):
                continue
            if w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= img_w and y + h <= img_h:
                boxes.append((x, y, w, h))
        if boxes:
            return boxes
    cell_w, cell_h = img_w // cols, img_h // rows
    return [(c * cell_w, r * cell_h, cell_w, cell_h) for r in range(rows) for c in range(cols)]


def score_cells(
    pixels: np.ndarray,
    word: str,
    cells: list[tuple[int, int, int, int]],
    *,
    hue: tuple[float, float],
    sat_min: float = 0.35,
    font_path: str | None = None,
) -> list[ShapeMatch]:
    """Score each published cell as a whole against the target word shape.

    One candidate per cell: colour components are never chased across cell borders,
    where one tile's background would merge with another tile's caption.
    """
    if not cells or not word:
        return []
    return _ranked(color_mask(pixels, hue, sat_min), cells, word, font_path)


def _ranked(
    mask: np.ndarray, boxes: list[tuple[int, int, int, int]], word: str, font_path: str | None
) -> list[ShapeMatch]:
    """Rank boxes by shape fit against the word; the shared core of both scorers."""
    results: list[ShapeMatch] = []
    for x, y, w, h in boxes:
        region = mask[y : y + h, x : x + w]
        target = glyph_mask(word, (w, h), font_path)
        if not target.any():
            continue
        iou = _iou(region, target)
        correlation = _ncc(region.astype(np.float32), target.astype(np.float32))
        score = 0.5 * iou + 0.5 * max(0.0, correlation)
        results.append(
            ShapeMatch(
                box=(x, y, w, h),
                iou=round(iou, 4),
                correlation=round(correlation, 4),
                score=round(score, 4),
                evidence=[f"region {w}x{h}", f"iou {iou:.3f}", f"ncc {correlation:.3f}"],
            )
        )
    results.sort(key=lambda item: item.score, reverse=True)
    return results


def match_word_shape(
    pixels: np.ndarray,
    word: str,
    *,
    hue: tuple[float, float],
    sat_min: float = 0.35,
    font_path: str | None = None,
) -> list[ShapeMatch]:
    """Rank every colour region of the picture by how well its shape matches `word`."""
    mask = color_mask(pixels, hue, sat_min)
    boxes = candidate_boxes(mask)
    if not boxes or not word:
        return []
    return _ranked(mask, boxes, word, font_path)


def choose_ordered(matches: list[ShapeMatch], needed: int) -> dict:
    """Decide whether the ranked shapes are trustworthy enough to click in order."""
    accepted = [item for item in matches if item.accepted]
    if len(accepted) < needed:
        return {
            "sure": False,
            "why": "fewer shapes matched than the challenge asks for",
            "accepted": len(accepted),
            "needed": needed,
        }
    best = accepted[:needed]
    rest = accepted[needed:]
    lead = best[-1].score - (rest[0].score if rest else 0.0)
    if lead < MIN_LEAD:
        return {
            "sure": False,
            "why": "the best shape does not clearly beat the next candidate",
            "accepted": len(accepted),
            "needed": needed,
            "lead": round(lead, 4),
        }
    return {
        "sure": True,
        "boxes": [item.box for item in best],
        "scores": [item.score for item in best],
        "accepted": len(accepted),
    }


def cache_key(pixels: np.ndarray, word: str) -> str:
    return hashlib.sha256(pixels.tobytes() + word.encode("utf-8")).hexdigest()[:16]
