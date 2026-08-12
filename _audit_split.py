import page_perception as pp
cases = [
    "a >>> b",
    "div[data-op='a >>> b']",
    'div[data-op="a >>> b"] >>> span',
    "#host >>> #inner >>> button",
    "ref:5",
    "ref:0011aabb:5",
    "ref:0011AABB:5",
    "a[href='x'] >>> b",
    "a[title='unbalanced >>> quote] >>> b",   # unbalanced quote
    "a[foo >>> b",                            # unbalanced bracket
    "a >>>b",
    "a>>>b",
    "div > p >>> span",
    "[data-x='a'] [data-y='b'] >>> c",
    "a\[ >>> b",
    "*:not([x=' >>> ']) >>> y",
    "a[b='c'][d='e >>> f'] >>> g",
    "a >>> ",
    " >>> a",
    " >>> ",
    "a >>> b >>> c",
    "input[value='a >>> b'][name='q']",
]
for c in cases:
    try:
        parts = pp.split_piercing_path(c)
    except Exception as e:
        parts = f"EXC {type(e).__name__}: {e}"
    try:
        expr = pp.resolve_locator_expression(c)
        expr = (expr[:70] + "...") if expr and len(expr) > 70 else expr
    except Exception as e:
        expr = f"EXC {type(e).__name__}: {e}"
    print(repr(c), "->", parts, "| expr:", expr)
