"""The seam between "how we perceive/act on a surface" and "the recorded flow".

Everything above this line (agent loop, compiler, replay engine, conditions, outcomes,
policy, escalation) talks only to ``Surface``. A surface adapter owns:

* perception   — produce an ``Observation`` (text rendering + element list + screenshot)
* description  — turn an element the model picked into ranked candidate ``Locator``s
* resolution   — find a ``Target`` again, strategy by strategy, requiring uniqueness
* actuation    — click / fill / select / press / navigate / read on a resolved element
* state probes — text present, location, settled?, visible error text, dialogs
* evidence     — screenshots (masked), raw structure dumps
* handoff hooks— pump the event loop while a human drives; report human input events

The web adapter (``web.py``) implements this with Playwright + an injected script. A
Windows desktop adapter would implement the same protocol over UI Automation (role =
ControlType, name = Name/LabeledBy, attr = AutomationId, table_cell = Grid/Table pattern,
frame = top-level window); a screenshot-only adapter (Citrix, terminal emulators) would
implement ``describe``/``resolve`` with OCR text anchors + relative offsets. Locator kinds a
surface does not understand simply fail to resolve and the next strategy is tried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schema import Locator, Target


@dataclass
class FrameView:
    key: str  # frame name or "top"
    url: str
    title: str
    lines: list[str]
    elements: list[dict[str, Any]]


@dataclass
class Observation:
    frames: list[FrameView]
    screenshot: bytes | None = None
    dialogs: list[str] = field(default_factory=list)

    @property
    def elements(self) -> dict[str, dict[str, Any]]:
        return {e["ref"]: e for f in self.frames for e in f.elements}

    def render(self, path_only: bool = True) -> str:
        out: list[str] = []
        for f in self.frames:
            url = f.url
            if path_only and "://" in url:
                url = "/" + url.split("://", 1)[1].split("/", 1)[-1] if "/" in url.split("://", 1)[1] else "/"
            out.append(f'## frame "{f.key}" {url}')
            out.extend(f.lines or ["(empty)"])
        if self.dialogs:
            out.append("## native dialogs seen: " + " / ".join(self.dialogs))
        return "\n".join(out)


@dataclass
class Resolved:
    handle: Any
    info: dict[str, Any]
    frame: Any
    index: int  # which locator strategy matched (0 = primary)
    locator: Locator
    attempts: list[str] = field(default_factory=list)  # strategies tried before this one


@dataclass
class Unresolved:
    reason: str  # "frame_missing" | "not_found" | "ambiguous" | "navigating"
    attempts: list[str] = field(default_factory=list)


class Surface(Protocol):
    def observe(self, screenshot: bool = True) -> Observation: ...
    def describe(self, ref: str) -> tuple[Any, dict[str, Any], list[Locator]] | None: ...
    def resolve(self, target: Target, params: dict[str, str]) -> Resolved | Unresolved: ...
    def click(self, r: Resolved) -> None: ...
    def fill(self, r: Resolved, value: str) -> None: ...
    def select(self, r: Resolved, option: str) -> None: ...
    def press(self, key: str, r: Resolved | None = None) -> None: ...
    def navigate(self, url: str, frame: str | None = None) -> None: ...
    def read(self, r: Resolved) -> str: ...
    def text_present(self, pattern: str, regex: bool = False, frame: str | None = None) -> bool: ...
    def location(self, frame: str | None = None) -> str | None: ...
    def locations(self) -> dict[str, str]: ...
    def settled(self) -> bool: ...
    def visible_alerts(self) -> list[str]: ...
    def sensitive_texts(self) -> list[str]: ...
    def screenshot(self, path: str, mask: list[Resolved] | None = None, mask_texts: list[str] | None = None) -> str: ...
    def dump_structure(self) -> dict[str, str]: ...
    def pump(self, ms: int) -> None: ...
    def drain_human_events(self) -> list[dict[str, Any]]: ...
