"""US-layout keyboard table shared by the Selenium and companion input paths.

Chrome's ``Input.dispatchKeyEvent`` only behaves like a real keyboard when
``key``, ``code``, ``windowsVirtualKeyCode`` and ``location`` agree with each
other. Games read all four in different combinations, so they are resolved from
one table instead of being guessed per call site.
"""

from __future__ import annotations


MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

# Selenium's private-use key characters, kept here so the game key aliases and
# the CDP resolver never drift apart.
SELENIUM_KEYS = {
    "BACKSPACE": "",
    "TAB": "",
    # Selenium's ENTER (U+E007) is the NUMPAD Enter to chromedriver; the main Enter
    # key is RETURN (U+E006), which is what a keyboard sends for "Enter".
    "ENTER": "",
    "RETURN": "",
    "NUMPAD_ENTER": "",
    "SHIFT": "",
    "CONTROL": "",
    "ALT": "",
    "ESCAPE": "",
    "SPACE": "",
    "PAGEUP": "",
    "PAGEDOWN": "",
    "END": "",
    "HOME": "",
    "LEFT": "",
    "UP": "",
    "RIGHT": "",
    "DOWN": "",
    "INSERT": "",
    "DELETE": "",
    "NUMPAD0": "",
    "NUMPAD1": "",
    "NUMPAD2": "",
    "NUMPAD3": "",
    "NUMPAD4": "",
    "NUMPAD5": "",
    "NUMPAD6": "",
    "NUMPAD7": "",
    "NUMPAD8": "",
    "NUMPAD9": "",
    "MULTIPLY": "",
    "ADD": "",
    "SUBTRACT": "",
    "DECIMAL": "",
    "DIVIDE": "",
    "META": "",
    **{f"F{index}": chr(0xE030 + index) for index in range(1, 13)},
}

# raw character -> (key, code, windowsVirtualKeyCode, location)
_SPECIAL_KEYS: dict[str, tuple[str, str, int, int]] = {
    "": ("Backspace", "Backspace", 8, 0),
    "": ("Tab", "Tab", 9, 0),
    "": ("Enter", "Enter", 13, 0),
    "": ("Enter", "NumpadEnter", 13, 3),
    "": ("Shift", "ShiftLeft", 16, 1),
    "": ("Control", "ControlLeft", 17, 1),
    "": ("Alt", "AltLeft", 18, 1),
    "": ("Escape", "Escape", 27, 0),
    "": (" ", "Space", 32, 0),
    "": ("PageUp", "PageUp", 33, 0),
    "": ("PageDown", "PageDown", 34, 0),
    "": ("End", "End", 35, 0),
    "": ("Home", "Home", 36, 0),
    "": ("ArrowLeft", "ArrowLeft", 37, 0),
    "": ("ArrowUp", "ArrowUp", 38, 0),
    "": ("ArrowRight", "ArrowRight", 39, 0),
    "": ("ArrowDown", "ArrowDown", 40, 0),
    "": ("Insert", "Insert", 45, 0),
    "": ("Delete", "Delete", 46, 0),
    "": ("*", "NumpadMultiply", 106, 3),
    "": ("+", "NumpadAdd", 107, 3),
    "": ("-", "NumpadSubtract", 109, 3),
    "": (".", "NumpadDecimal", 110, 3),
    "": ("/", "NumpadDivide", 111, 3),
    "": ("Meta", "MetaLeft", 91, 1),
    **{
        chr(0xE01A + digit): (str(digit), f"Numpad{digit}", 96 + digit, 3)
        for digit in range(10)
    },
    **{
        chr(0xE030 + index): (f"F{index}", f"F{index}", 111 + index, 0)
        for index in range(1, 13)
    },
}

# Unshifted punctuation on a US keyboard.
_PUNCTUATION: dict[str, tuple[str, int]] = {
    "`": ("Backquote", 192),
    "-": ("Minus", 189),
    "=": ("Equal", 187),
    "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221),
    "\\": ("Backslash", 220),
    ";": ("Semicolon", 186),
    "'": ("Quote", 222),
    ",": ("Comma", 188),
    ".": ("Period", 190),
    "/": ("Slash", 191),
    " ": ("Space", 32),
}

# Shifted character -> the physical key that produces it.
_SHIFTED_TO_BASE = {
    "~": "`",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
}


def physical_key(raw: str) -> str:
    """Identify the physical key behind one spelling of it.

    The same key reaches this module under several names - ``LEFT`` and
    ``ARROW_LEFT``, ``CTRL`` and ``CONTROL``, ``w`` and ``W``, a literal space
    and ``SPACE`` - and a caller that presses under one name and releases under
    another must lift the key it really pressed. The US-layout ``code`` is that
    identity; characters with no physical key on this layout fall back to
    themselves, folded to upper case so letter case alone never splits a key.
    """
    code = resolve_key(raw)[1]
    return code or raw.upper()


def resolve_key(raw: str, *, shifted: bool = False) -> tuple[str, str, int, int]:
    """Resolve one key into ``(key, code, windowsVirtualKeyCode, location)``.

    ``shifted`` reports whether Shift is currently held, so that a letter is
    reported the way a real browser reports it: ``key='w'`` with ``code='KeyW'``
    when Shift is up, ``key='W'`` when it is down.
    """
    special = _SPECIAL_KEYS.get(raw)
    if special is not None:
        return special
    if len(raw) != 1:
        # Named keys such as "ArrowLeft" or "F5" may arrive spelled out.
        named = _SPECIAL_KEYS.get(SELENIUM_KEYS.get(raw.upper(), ""))
        if named is not None:
            return named
        return (raw, raw, 0, 0)
    if raw.isalpha():
        upper = raw.upper()
        if len(upper) != 1 or not ("A" <= upper <= "Z"):
            # Non-Latin letters have no US-layout physical key; report the
            # character itself and leave the physical code empty.
            return (raw, "", 0, 0)
        return (upper if shifted else raw.lower(), f"Key{upper}", ord(upper), 0)
    if raw.isdigit():
        return (raw, f"Digit{raw}", ord(raw), 0)
    base = _SHIFTED_TO_BASE.get(raw)
    if base is not None:
        if base.isdigit():
            return (raw, f"Digit{base}", ord(base), 0)
        code, key_code = _PUNCTUATION[base]
        return (raw, code, key_code, 0)
    punctuation = _PUNCTUATION.get(raw)
    if punctuation is not None:
        return (raw, punctuation[0], punctuation[1], 0)
    return (raw, "", 0, 0)


# The text a real keyboard's keyDown carries besides printable characters: Enter
# types a carriage return. Without it Chrome raises no keypress and no
# beforeinput insertLineBreak, so a textarea or a canvas text field that builds
# its text from keypress never gets the new line.
_KEY_TEXT = {"Enter": "\r"}


def key_text(key: str) -> str | None:
    """The ``text`` a keyDown of ``key`` carries, or None for a key that types nothing."""
    return key if len(key) == 1 else _KEY_TEXT.get(key)


# DOM ``key``/``code`` spellings accepted as key names (upper-cased, no separators),
# mapped to the names the alias table already knows.
DOM_KEY_NAMES = {
    "ARROWLEFT": "LEFT", "ARROWRIGHT": "RIGHT", "ARROWUP": "UP", "ARROWDOWN": "DOWN",
    "SHIFTLEFT": "SHIFT", "SHIFTRIGHT": "SHIFT", "CONTROLLEFT": "CONTROL",
    "CONTROLRIGHT": "CONTROL", "ALTLEFT": "ALT", "ALTRIGHT": "ALT", "METALEFT": "META",
    "METARIGHT": "META", "PAGEUP": "PAGE_UP", "PAGEDOWN": "PAGE_DOWN", "NUMPADENTER": "NUMPAD_ENTER",
    # KeyboardEvent.code of the US punctuation keys: the unshifted character.
    "MINUS": "-", "EQUAL": "=", "BRACKETLEFT": "[", "BRACKETRIGHT": "]", "BACKSLASH": "\\",
    "SEMICOLON": ";", "QUOTE": "'", "COMMA": ",", "PERIOD": ".", "SLASH": "/", "BACKQUOTE": "`",
    "NUMPADADD": "ADD", "NUMPADSUBTRACT": "SUBTRACT", "NUMPADMULTIPLY": "MULTIPLY",
    "NUMPADDIVIDE": "DIVIDE", "NUMPADDECIMAL": "DECIMAL",
    **{f"NUMPAD{digit}": f"NUMPAD{digit}" for digit in range(10)},
}

# Keys a real keyboard has and WebDriver cannot send; named in the refusal.
UNSENDABLE_KEYS = {
    "CAPSLOCK": "CapsLock has no WebDriver or CDP key event that toggles it; send Shift+<letter> for capitals",
    "NUMLOCK": "NumLock cannot be toggled through WebDriver; the NUMPAD0-9 names always send digits",
    "SCROLLLOCK": "ScrollLock cannot be sent through WebDriver",
}


def dom_key_name(name: str) -> str | None:
    """``KeyW`` -> ``w``, ``Digit1`` -> ``1``, ``ArrowLeft`` -> ``LEFT``; None when unknown."""
    compact = name.replace("-", "").replace("_", "").replace(" ", "")
    if len(compact) == 4 and compact[:3].lower() == "key" and compact[3].isalpha():
        return compact[3].lower()
    if len(compact) == 6 and compact[:5].lower() == "digit" and compact[5].isdigit():
        return compact[5]
    return DOM_KEY_NAMES.get(compact.upper())


def expand_chords(keys: list[str]) -> list[str]:
    """``["Control+Shift+K"]`` -> ``["Control", "Shift", "K"]``; a lone ``+`` stays a key.

    The keys of one press_keys call are one chord either way: all go down in
    order, then all come up in reverse.
    """
    expanded: list[str] = []
    for key in keys or []:
        text = str(key)
        plus_key = len(text) > 2 and text.endswith("++")  # "Shift++": Shift and the + key
        body = text[:-2] if plus_key else text
        if plus_key or (len(body) > 1 and "+" in body.strip("+")):
            expanded.extend(part for part in body.split("+") if part)
            if plus_key:
                expanded.append("+")
        else:
            expanded.append(text)
    return expanded
