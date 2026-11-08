"""Playwright implementation of the Surface protocol (web: modern, legacy, framesets).

Perception and locator resolution run *in the page* (perception.js, injected into every
frame), so recording and replay use literally the same code to name and find elements.
Python only orchestrates: which frame, which strategy, uniqueness, and acting through
Playwright (which adds actionability waits: visible, stable, enabled, receiving events).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Browser, BrowserContext, Error, Frame, Page, Playwright, Route, sync_playwright

from ..schema import Locator, Target
from .base import FrameView, Observation, Resolved, Unresolved

PERCEPTION_JS = (Path(__file__).parent / "perception.js").read_text()
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def substitute(obj: Any, params: dict[str, str]) -> Any:
    """Replace {param} placeholders inside a JSON-like structure."""
    if isinstance(obj, str):
        return PLACEHOLDER.sub(lambda m: str(params.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, dict):
        return {k: substitute(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, params) for v in obj]
    return obj


def describe_locator(loc: Locator) -> str:
    d = loc.model_dump(exclude_none=True, exclude_defaults=False)
    by = d.pop("by")
    d.pop("match", None) if d.get("match") == "exact" else None
    if d.get("offset") == 1:
        d.pop("offset")
    return f"{by}(" + ", ".join(f"{k}={v!r}" for k, v in d.items()) + ")"


class WebSurface:
    def __init__(
        self,
        *,
        url_allowed: Callable[[str], tuple[bool, str]],
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 860),
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self._url_allowed = url_allowed
        self._headless = headless
        self._viewport = viewport
        self._on_event = on_event or (lambda *a, **k: None)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._inflight: set[Any] = set()
        self.dialogs: list[dict[str, Any]] = []
        self.blocked_requests: list[str] = []
        self._human: list[dict[str, Any]] = []

    # ---- lifecycle -------------------------------------------------------------------

    def start(self) -> "WebSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self.context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]}, accept_downloads=False
        )
        self.context.add_init_script(PERCEPTION_JS)
        self.context.expose_binding("__cuHumanEvent", self._on_human_event)
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()
        self.page.on("dialog", self._on_dialog)
        self.page.on("request", lambda r: self._inflight.add(r) if r.resource_type == "document" else None)
        self.page.on("requestfinished", lambda r: self._inflight.discard(r))
        self.page.on("requestfailed", lambda r: self._inflight.discard(r))
        return self

    def close(self) -> None:
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass

    def __enter__(self) -> "WebSurface":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- guards & hooks --------------------------------------------------------------

    def _route(self, route: Route) -> None:
        ok, why = self._url_allowed(route.request.url)
        if ok:
            route.continue_()
            return
        self.blocked_requests.append(route.request.url)
        self._on_event("network_blocked", url=route.request.url, reason=why, resource=route.request.resource_type)
        route.abort("blockedbyclient")

    def _on_dialog(self, dialog: Any) -> None:
        # Native dialogs freeze the page until handled. Alerts are informational: accept.
        # confirm()/prompt() could commit something: dismiss (the conservative answer).
        accepted = dialog.type == "alert"
        self.dialogs.append({"type": dialog.type, "message": dialog.message, "accepted": accepted, "ts": time.time()})
        self._on_event("dialog", dialog_type=dialog.type, message=dialog.message, accepted=accepted)
        try:
            dialog.accept() if accepted else dialog.dismiss()
        except Error:
            pass

    def _on_human_event(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        payload["received_at"] = time.time()
        self._human.append(payload)

    def drain_human_events(self) -> list[dict[str, Any]]:
        out, self._human = self._human, []
        return out

    def pump(self, ms: int) -> None:
        assert self.page
        self.page.wait_for_timeout(ms)

    # ---- frames ----------------------------------------------------------------------

    def _frames(self) -> list[tuple[str, Frame]]:
        assert self.page
        out = []
        for i, f in enumerate(self.page.frames):
            if f.is_detached():
                continue
            key = "top" if f == self.page.main_frame else (f.name or f"frame{i}")
            out.append((key, f))
        return out

    def _frame(self, key: str | None) -> Frame | None:
        for k, f in self._frames():
            if k == (key or "top"):
                return f
        return None

    def _eval(self, frame: Frame, js: str, arg: Any = None) -> Any:
        return frame.evaluate(js, arg)

    # ---- perception ------------------------------------------------------------------

    def observe(self, screenshot: bool = True) -> Observation:
        frames: list[FrameView] = []
        for key, f in self._frames():
            try:
                snap = self._eval(f, "p => window.__cu ? window.__cu.snapshot(p) : null", f"{key}:")
            except Error:
                continue
            if snap:
                frames.append(FrameView(key, snap["url"], snap["title"], snap["lines"], snap["elements"]))
        shot = None
        if screenshot and self.page:
            try:
                shot = self.page.screenshot(type="jpeg", quality=60)
            except Error:
                shot = None
        recent = [d["message"] for d in self.dialogs[-3:]]
        return Observation(frames=frames, screenshot=shot, dialogs=recent)

    def element_handle(self, ref: str) -> tuple[Frame, Any] | None:
        key = ref.split(":", 1)[0]
        f = self._frame(key)
        if f is None:
            return None
        try:
            h = f.evaluate_handle("r => window.__cu.el(r)", ref).as_element()
        except Error:
            return None
        return (f, h) if h else None

    def describe(self, ref: str) -> tuple[Frame, Any, dict[str, Any], list[dict[str, Any]]] | None:
        """Element handle + info + candidate locators (unverified) for a snapshot ref."""
        fh = self.element_handle(ref)
        if not fh:
            return None
        f, h = fh
        d = self._eval(f, "r => window.__cu.describe(r)", ref)
        if not d:
            return None
        return f, h, d["info"], d["locators"]

    def matches(self, frame_key: str | None, loc: dict[str, Any]) -> list[dict[str, Any]]:
        f = self._frame(frame_key)
        if f is None:
            return []
        try:
            return self._eval(f, "l => window.__cu.resolve(l)", loc) or []
        except Error:
            return []

    # ---- resolution ------------------------------------------------------------------

    def resolve(self, target: Target, params: dict[str, str]) -> Resolved | Unresolved:
        f = self._frame(target.frame)
        if f is None:
            return Unresolved("frame_missing", [f"frame {target.frame!r} not present"])
        attempts: list[str] = []
        ambiguous = False
        for i, loc in enumerate(target.locators):
            spec = substitute(loc.model_dump(exclude_none=True), params)
            try:
                found = self._eval(f, "l => window.__cu.resolve(l)", spec) or []
            except Error:
                return Unresolved("navigating", attempts + ["frame is navigating"])
            if loc.nth is not None:
                chosen = found[loc.nth] if len(found) > loc.nth else None
            else:
                chosen = found[0] if len(found) == 1 else None
            if chosen is None:
                ambiguous |= len(found) > 1
                attempts.append(f"{describe_locator(loc)} -> {len(found)} matches")
                continue
            try:
                h = f.evaluate_handle("r => window.__cu.el(r)", chosen["ref"]).as_element()
            except Error:
                return Unresolved("navigating", attempts + ["frame navigated during resolution"])
            if h is None:
                attempts.append(f"{describe_locator(loc)} -> element vanished")
                continue
            return Resolved(handle=h, info=chosen, frame=f, index=i, locator=loc, attempts=attempts)
        return Unresolved("ambiguous" if ambiguous else "not_found", attempts)

    # ---- actuation -------------------------------------------------------------------

    def click(self, r: Resolved) -> None:
        r.handle.click(timeout=5000)

    def fill(self, r: Resolved, value: str) -> None:
        r.handle.fill(value, timeout=5000)

    def select(self, r: Resolved, option: str) -> None:
        try:
            r.handle.select_option(label=option, timeout=5000)
        except Error:
            r.handle.select_option(value=option, timeout=5000)

    def press(self, key: str, r: Resolved | None = None) -> None:
        assert self.page
        if r is not None:
            r.handle.press(key, timeout=5000)
        else:
            self.page.keyboard.press(key)

    def navigate(self, url: str, frame: str | None = None) -> None:
        f = self._frame(frame)
        if f is None:
            raise Error(f"frame {frame!r} not present")
        f.goto(url, wait_until="commit")

    def read(self, r: Resolved) -> str:
        tag = (r.info.get("tag") or "").lower()
        if tag in ("input", "textarea", "select"):
            return r.handle.input_value()
        return re.sub(r"\s+", " ", r.handle.inner_text()).strip()

    # ---- state probes ----------------------------------------------------------------

    def text_present(self, pattern: str, regex: bool = False, frame: str | None = None) -> bool:
        frames = [(frame, self._frame(frame))] if frame else self._frames()
        for _, f in frames:
            if f is None:
                continue
            try:
                if self._eval(f, "a => window.__cu ? window.__cu.textPresent(a[0], a[1]) : false", [pattern, regex]):
                    return True
            except Error:
                continue
        return False

    def location(self, frame: str | None = None) -> str | None:
        f = self._frame(frame)
        return f.url if f else None

    def locations(self) -> dict[str, str]:
        return {k: f.url for k, f in self._frames()}

    def settled(self, quiet_ms: int = 350, frames: set[str] | None = None) -> bool:
        """No document request in flight, every frame loaded, and the frames we care about
        quiet for quiet_ms. Scoping the quiet check matters: a ticking clock in a banner
        frame must not make the work frame look "busy" forever."""
        if self._inflight:
            return False
        for key, f in self._frames():
            try:
                st = self._eval(f, "() => window.__cu ? [document.readyState, window.__cu.quietMs()] : ['loading', 0]")
            except Error:
                return False
            if st[0] != "complete":
                return False
            if (frames is None or key in frames) and st[1] < quiet_ms:
                return False
        return True

    def visible_alerts(self) -> list[str]:
        out: list[str] = []
        for _, f in self._frames():
            try:
                out += self._eval(f, "() => window.__cu ? window.__cu.alerts() : []")
            except Error:
                pass
        return out

    def sensitive_texts(self) -> list[str]:
        out: list[str] = []
        for _, f in self._frames():
            try:
                out += self._eval(f, "() => window.__cu ? window.__cu.sensitiveTexts() : []")
            except Error:
                pass
        return out

    # ---- evidence --------------------------------------------------------------------

    def screenshot(self, path: str, mask: list[Resolved] | None = None, mask_texts: list[str] | None = None) -> str:
        """Evidence screenshot: PII-labeled values, known sensitive values, money amounts
        and the given elements are blacked out. (Observations sent to the model during
        discovery are taken separately and are not masked: it has to read the screen.)"""
        assert self.page
        frames = self._frames()
        try:
            for _, f in frames:
                try:
                    self._eval(
                        f,
                        "v => { if (window.__cu) { window.__cu.maskStyle(true); window.__cu.maskPii(true); window.__cu.maskData(v); } }",
                        list(mask_texts or []),
                    )
                except Error:
                    pass
            for r in mask or []:
                try:
                    r.handle.evaluate("el => el.setAttribute('data-cu-mask', '1')")
                except Error:
                    pass
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=path)
        finally:
            for _, f in frames:
                try:
                    self._eval(f, "() => { if (window.__cu) { window.__cu.unmaskAll(); window.__cu.maskStyle(false); } }")
                except Error:
                    pass
        return path

    def dump_structure(self) -> dict[str, str]:
        out = {}
        for k, f in self._frames():
            try:
                # Clone, so evidence sanitization cannot alter the live form.
                out[k] = f.evaluate("""() => {
                    const doc = document.documentElement.cloneNode(true);
                    doc.querySelectorAll('script').forEach(el => el.remove());
                    doc.querySelectorAll('input, textarea').forEach(el => {
                        if (!['submit', 'button', 'reset'].includes(el.type)) {
                            el.setAttribute('value', '[redacted]');
                            if (el.tagName === 'TEXTAREA') el.textContent = '[redacted]';
                        }
                    });
                    return '<!doctype html>\\n' + doc.outerHTML;
                }""")
            except Error:
                pass
        return out
