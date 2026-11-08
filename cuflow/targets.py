"""Turning "the element the model (or a human) acted on" into a robust, replayable Target.

At record time the surface proposes candidate locators for the element. We keep a
candidate only if, *right now*, it resolves to exactly that element (verified by structural
path). A locator that is already ambiguous at record time can only get worse later.

We also generalize while recording:
* values that came from inputs are replaced by ``{param}`` placeholders (a link named
  after the member number, a row identified by it, an href carrying it),
* other dynamic URL query values (per-render tokens) become ``*``,
* for extraction targets, names that *are* the data (a cell whose text is the balance)
  are dropped; the cell is addressed by column header + row key instead.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from pydantic import TypeAdapter

from .schema import BBox, Locator, Target
from .surface.web import substitute

_LOC = TypeAdapter(Locator)
RANK = {"role": 0, "table_cell": 1, "attr": 2, "text": 3, "css": 4}
_NUMERIC = re.compile(r"^[\s$()\-+,.\d]*$")


def parametrize(s: str, examples: dict[str, str]) -> str:
    """Replace example input values with {param} placeholders (whole tokens only)."""
    for value, param in sorted(examples.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"(?<![\w]){re.escape(value)}(?![\w])", "{" + param + "}", s)
    return s


def canonical_location(url_or_path: str, examples: dict[str, str]) -> str:
    """'/work/member?m=10042&t=9cc643' -> '/work/member?m={member_id}&t=*'."""
    u = urllib.parse.urlsplit(url_or_path)
    path = "/".join(parametrize(seg, examples) for seg in (u.path or "/").split("/"))
    q = []
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        q.append(f"{k}={'{' + examples[v] + '}' if v in examples else '*'}")
    return path + ("?" + "&".join(q) if q else "")


def describe_element(info: dict[str, Any], table_key: dict[str, str] | None = None) -> str:
    role, name = info.get("role"), (info.get("name") or "").strip()
    t = info.get("table") or {}
    if role == "cell" and t:
        col = (t.get("header") or [""])[t.get("col", 0)] if t.get("header") else ""
        if table_key:
            return f'"{col}" cell of the row where {table_key["column"]} = "{table_key["value"]}"'
        if t.get("col", 0) >= 1 and t.get("row_cells"):
            return f'value next to "{t["row_cells"][0]}"'
        return f'"{col}" cell'
    noun = {"textbox": "text box", "combobox": "drop-down", "link": "link", "button": "button"}.get(role or "", role or "element")
    return f"{name} {noun}".strip()


def role_in_row(info: dict[str, Any]) -> bool:
    return info.get("role") != "cell" and bool((info.get("table") or {}).get("row_cells"))


def build_target(
    surface: Any,
    frame_key: str,
    info: dict[str, Any],
    candidates: list[dict[str, Any]],
    examples: dict[str, str],
    *,
    row_hint: str | None = None,
    for_extract: bool = False,
    verify: bool = True,
) -> Target:
    concrete = {p: v for v, p in examples.items()}
    me = info.get("css")

    def check(loc: dict[str, Any]) -> tuple[bool, int | None]:
        if not verify:
            return True, None
        found = surface.matches(frame_key, substitute(loc, concrete))
        paths = [m.get("css") for m in found]
        idx = paths.index(me) if me in paths else None
        return (len(found) == 1 and idx == 0), idx

    kept: list[dict[str, Any]] = []
    table_key: dict[str, str] | None = None
    t = info.get("table") or {}
    for cand in candidates:
        c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cand.items()}
        by = c["by"]
        if for_extract and by in ("role", "text"):
            continue  # the "name" of a data cell is the data itself
        if by == "table_cell" and c.get("column"):
            header, cells, col = t.get("header") or [], t.get("row_cells") or [], t.get("col")
            order: list[int] = []
            if row_hint:
                order += [i for i, v in enumerate(cells) if row_hint.lower() in v.lower()]
            order += [i for i, v in enumerate(cells) if v and not _NUMERIC.match(v) and i not in order]
            order += [i for i, v in enumerate(cells) if v and i not in order]
            for k in order:
                if k == col or k >= len(header) or not header[k]:
                    continue
                key = {"column": header[k], "value": parametrize(cells[k], examples)}
                loc = {"by": "table_cell", "column": c["column"], "row_key": key}
                if check(loc)[0]:
                    kept.append(loc)
                    table_key = key
                    break
            continue
        if by == "table_cell":
            c.pop("row_key", None)
            if check(c)[0]:
                kept.append(c)
            continue
        if by == "role":
            c["name"] = parametrize(c["name"], examples)
            param_cells = [v for v in (t.get("row_cells") or []) if v in examples]
            if param_cells and role_in_row(info):
                # a control in a row that carries an input value: always qualify by it, so
                # the step still picks the right row when a search returns several
                c2 = {**c, "row_text": parametrize(param_cells[0], examples)}
                if check(c2)[0]:
                    kept.append(c2)
                    continue
            ok, _ = check(c)
            if ok:
                kept.append(c)
            elif t.get("row_cells"):
                # repeated control ("Select" in every row): qualify by the row it sits in,
                # preferring a cell that holds an input value (=> generalizes as {param})
                cells = t["row_cells"]
                pref = [v for v in cells if v in examples] + [v for v in cells if v and v not in examples]
                for v in pref:
                    c2 = {**c, "row_text": parametrize(v, examples)}
                    if check(c2)[0]:
                        kept.append(c2)
                        break
            continue
        if by == "attr" and "href" in c.get("attrs", {}):
            c["attrs"]["href"] = canonical_location(c["attrs"]["href"], examples)
        ok, idx = check(c)
        if ok:
            kept.append(c)
        elif by == "css" and idx is not None:
            kept.append({**c, "nth": idx})  # positional: last resort only
    if not kept:
        kept = [{"by": "css", "css": me}]
    kept.sort(key=lambda loc: RANK[loc["by"]])
    bbox = info.get("bbox")
    return Target(
        description=describe_element(info, table_key),
        frame=None if frame_key == "top" else frame_key,
        locators=[_LOC.validate_python(loc) for loc in kept],
        bbox_hint=BBox(**bbox) if bbox else None,
    )
