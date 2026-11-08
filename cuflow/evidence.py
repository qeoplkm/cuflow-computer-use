"""Run evidence: a structured, redacted event log plus richer artifacts on demand.

Each run gets a directory:

    runs/<run_id>/events.jsonl        every decision/action/state change, redacted
    runs/<run_id>/screens/*.png       screenshots (PII-labeled values blacked out)
    runs/<run_id>/failure/            screenshot + per-frame DOM (redacted) on failure
    runs/<run_id>/interventions/*.json
    runs/<run_id>/result.json         the result contract (outputs redacted by sensitivity)

Raw Playwright traces are intentionally *not* recorded: they embed unredacted DOM and
network bodies, which is exactly the regulated data we must not persist.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from .redaction import Redactor

CONSOLE_SKIP = {"observation"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class RunRecorder:
    def __init__(self, root: Path, mode: str, redactor: Redactor, console: bool = True) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{mode}-{stamp}-{secrets.token_hex(2)}"
        self.dir = Path(root) / self.run_id
        (self.dir / "screens").mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.console = console
        self._seq = 0
        self._shots = 0
        self._log = (self.dir / "events.jsonl").open("a")

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        if "type" in data:
            data["detail_type"] = data.pop("type")
        rec = {**self.redactor.obj(_jsonable(data)), "seq": self._seq, "ts": now_iso(), "type": type_}
        self._log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._log.flush()
        if self.console and type_ not in CONSOLE_SKIP:
            print(_summary(rec), file=sys.stderr, flush=True)

    def shot(self, surface: Any, label: str, mask: list[Any] | None = None) -> str | None:
        self._shots += 1
        rel = f"screens/{self._shots:03d}-{_slug(label)}.png"
        try:
            self.redactor.add_many(surface.sensitive_texts(), "pii")
            surface.screenshot(str(self.dir / rel), mask=mask, mask_texts=self.redactor.known_values)
        except Exception as e:  # evidence must never crash the run
            self.event("evidence_error", what="screenshot", error=str(e))
            return None
        return rel

    def failure_bundle(self, surface: Any, label: str) -> tuple[str | None, list[str]]:
        shot = self.shot(surface, f"failure-{label}")
        dom_files: list[str] = []
        try:
            # Learn values on this screen before serializing its HTML, including
            # failures reached before discovery/extraction ever observed it.
            self.redactor.add_many(surface.sensitive_texts(), "pii")
            for frame, content in surface.dump_structure().items():
                rel = f"failure/{_slug(label)}-{_slug(frame)}.html"
                (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.dir / rel).write_text(self.redactor.text(content))
                dom_files.append(rel)
        except Exception as e:
            self.event("evidence_error", what="dom", error=str(e))
        return shot, dom_files

    def write_json(self, rel: str, data: Any, redact: bool = True) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = self.redactor.obj(_jsonable(data)) if redact else _jsonable(data)
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        return p

    def write_text(self, rel: str, text: str, redact: bool = True) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.redactor.text(text) if redact else text)
        return p

    def close(self) -> None:
        self._log.close()


def _jsonable(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json", exclude_none=True)
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in s)[:48].strip("-") or "x"


def _summary(rec: dict[str, Any]) -> str:
    skip = {"seq", "ts", "type"}
    parts = []
    for k, v in rec.items():
        if k in skip:
            continue
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        parts.append(f"{k}={s[:140]}")
    return f"  [{rec['seq']:03d}] {rec['type']:<22} " + "  ".join(parts)[:400]
