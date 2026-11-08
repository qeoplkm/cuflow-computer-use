"""MockCore Teller Workstation — an intentionally hostile "legacy core banking" web app.

This is the proxy target for the take-home. It imitates the surfaces we actually meet in
credit-union back offices:

* server-rendered HTML, a frameset (banner / nav / work), layout tables everywhere
* no ids, no test ids, no <label>s, cryptic field names (f01, fld_mbr_no), <td><b> "headers"
* JavaScript-driven links and buttons, red-<font> error messages, per-render URL tokens
* runtime conditions that a replay must survive: interstitial notices, native alerts,
  session expiry, slow responses, HTTP 500s, permission denials, and one *unknown*
  interstitial that no profile knows about (to exercise human escalation)

Faults are injected via ``--faults`` or at runtime via ``/__admin/faults`` (an endpoint the
automation policy explicitly denies). Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .data import MEMBERS, Member
from .tenants import TENANTS, Tenant

DEFAULT_USER = "teller01"


def operator_password() -> str:
    # Mock credential for a local mock app. Real deployments never see this code path.
    return os.environ.get("MOCKCORE_PASSWORD", "demo-teller-pass")


# --------------------------------------------------------------------------------------
# Faults
# --------------------------------------------------------------------------------------


@dataclass
class Faults:
    maintenance_notice: bool = False  # interstitial before the first work screen until acknowledged
    alert_on_home: bool = False  # native JS alert() when the home screen loads
    expire_session_after: int = 0  # expire the *first* session after N authenticated requests
    slow_member_ms: int = 0  # delay member-detail responses (transient slowness)
    member_errors: int = 0  # next N member-detail requests return HTTP 500 (-1 = always)
    survey_popup: bool = False  # unknown interstitial (in no profile) before member detail

    @classmethod
    def parse(cls, spec: str) -> "Faults":
        f = cls()
        for part in filter(None, (p.strip() for p in spec.split(","))):
            key, _, val = part.partition("=")
            kinds = {fl.name: fl.type for fl in fields(cls)}
            if key not in kinds:
                raise ValueError(f"unknown fault {key!r}; known: {sorted(kinds)}")
            setattr(f, key, int(val) if kinds[key] in (int, "int") else (val or "1") not in ("0", "false"))
        return f


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


@dataclass
class Session:
    user: str
    requests: int = 0
    notice_acked: bool = False
    survey_done: bool = False
    expired: bool = False
    pending: dict[str, str] | None = None


class AppState:
    def __init__(self, tenant: Tenant, faults: Faults) -> None:
        self.tenant = tenant
        self.faults = faults
        self.sessions: dict[str, Session] = {}
        self.first_session_expired = False
        self.next_suffix = 21
        self.lock = threading.Lock()


# --------------------------------------------------------------------------------------
# HTML helpers (deliberately 1999-era)
# --------------------------------------------------------------------------------------

e = html.escape


def page(title: str, body: str, bg: str = "#ffffff") -> str:
    return (
        f"<html><head><title>{e(title)}</title></head>"
        f'<body bgcolor="{bg}"><font face="Arial" size="2">{body}</font></body></html>'
    )


def err(code: str, msg: str) -> str:
    return f'<p><font color="red"><b>{e(code)} {e(msg)}</b></font></p>'


def money_ok(v: str) -> float | None:
    try:
        x = float(v.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return x if x > 0 else None


# --------------------------------------------------------------------------------------
# Request handler
# --------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "MockCoreHTTP/4.2"
    app: AppState  # injected

    # ---- plumbing -------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        if os.environ.get("MOCKCORE_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _sid(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "MCSID":
                return v
        return None

    def _session(self) -> Session | None:
        sid = self._sid()
        return self.app.sessions.get(sid) if sid else None

    # ---- routing --------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}

        if path.startswith("/__admin"):
            return self._admin(path, q)
        if path == "/" and method == "GET":
            return self._login_page()
        if path == "/login" and method == "POST":
            return self._login()
        if path == "/logout":
            sid = self._sid()
            if sid:
                self.app.sessions.pop(sid, None)
            return self._redirect("/")

        sess = self._session()
        if sess is None:
            return self._send(200, self._expired_page())
        with self.app.lock:
            sess.requests += 1
            limit = self.app.faults.expire_session_after
            if limit and not self.app.first_session_expired and sess.requests > limit:
                sess.expired = True
                self.app.first_session_expired = True
        if sess.expired:
            return self._send(200, self._expired_page())

        if path == "/main":
            return self._send(200, self._frameset())
        if path == "/banner":
            return self._send(200, self._banner(sess))
        if path == "/nav":
            return self._send(200, self._nav())

        if path.startswith("/work/"):
            if self.app.faults.maintenance_notice and not sess.notice_acked:
                if path == "/work/notice/ack" and method == "POST":
                    sess.notice_acked = True
                    return self._redirect(self._form().get("next") or "/work/home")
                nxt = path + (("?" + url.query) if url.query else "") if method == "GET" else "/work/home"
                return self._send(200, self._notice(nxt))
            return self._work(method, path, q, sess)

        self._send(404, page("Not Found", err("H404", "PAGE NOT FOUND")))

    # ---- admin (fault injection; denied by automation policy) -----------------------

    def _admin(self, path: str, q: dict[str, str]) -> None:
        if path == "/__admin/faults":
            if "set" in q:
                self.app.faults = Faults.parse(q["set"])
                self.app.first_session_expired = False
            return self._send(200, json.dumps(asdict(self.app.faults)), {"Content-Type": "application/json"})
        if path == "/__admin/reset":
            self.app.faults = Faults()
            self.app.sessions.clear()
            self.app.first_session_expired = False
            return self._send(200, "ok")
        self._send(404, "unknown admin endpoint")

    # ---- auth -----------------------------------------------------------------------

    def _login_page(self, error: str = "") -> None:
        t = self.app.tenant
        err_row = f'<tr><td colspan="2"><font color="red">{e(error)}</font></td></tr>' if error else ""
        body = f"""
<center><br><br>
<form method="post" action="/login">
<table border="1" cellpadding="0" cellspacing="0" bgcolor="#ffffff"><tr><td>
<table border="0" cellpadding="3" cellspacing="0" width="380">
<tr><td colspan="2" bgcolor="{t.color}"><font color="#ffffff"><b>{e(t.name)}</b><br>
<small>MockCore Teller Workstation {e(t.version)}</small></font></td></tr>
{err_row}
<tr><td>User ID:</td><td><input type="text" name="f01" size="14" maxlength="10"></td></tr>
<tr><td>Password:</td><td><input type="password" name="f02" size="14"></td></tr>
<tr><td>&nbsp;</td><td><input type="submit" value="Sign On"></td></tr>
</table></td></tr></table></form>
<p><font size="1" color="#666666">Authorized use only. All activity is monitored.</font></p>
</center>"""
        self._send(200, page("MockCore Teller - Sign On", body, bg="#d4d0c8"))

    def _login(self) -> None:
        f = self._form()
        if f.get("f01", "").strip().lower() != DEFAULT_USER or f.get("f02") != operator_password():
            return self._login_page("E101 INVALID USER ID OR PASSWORD")
        sid = secrets.token_hex(12)
        self.app.sessions[sid] = Session(user=DEFAULT_USER)
        self._redirect("/main", {"Set-Cookie": f"MCSID={sid}; Path=/; HttpOnly"})

    def _expired_page(self) -> str:
        return page(
            "Session",
            err("S900", "SESSION EXPIRED")
            + 'Your session has ended. Please <a href="/" target="_top">Sign On</a> again.',
        )

    # ---- chrome ---------------------------------------------------------------------

    def _frameset(self) -> str:
        return (
            "<html><head><title>MockCore Teller</title></head>"
            '<frameset rows="58,*" border="1">'
            '<frame name="banner" src="/banner" scrolling="no" noresize>'
            '<frameset cols="170,*">'
            '<frame name="nav" src="/nav">'
            '<frame name="work" src="/work/home">'
            "</frameset></frameset></html>"
        )

    def _banner(self, sess: Session) -> str:
        t = self.app.tenant
        return page(
            "banner",
            f'<table width="100%" cellpadding="3"><tr><td><font color="white"><b>{e(t.name)}</b></font></td>'
            f'<td align="right"><font color="white" size="1">OPR: {e(sess.user.upper())} &nbsp; BR: 001 &nbsp; '
            f"MockCore {e(t.version)}</font></td></tr></table>",
            bg=self.app.tenant.color,
        )

    def _nav(self) -> str:
        t = self.app.tenant
        body = f"""
<table width="100%" cellpadding="4" cellspacing="0">
<tr><td bgcolor="#999999"><font color="white"><b>FUNCTIONS</b></font></td></tr>
<tr><td><a href="/work/inquiry" target="work">{e(t.inquiry_menu)}</a></td></tr>
<tr><td><a href="javascript:void(0)" onclick="parent.frames['work'].location.href='/work/subacct'">New Sub-Account</a></td></tr>
<tr><td><a href="/work/transfers" target="work">Transfers</a></td></tr>
<tr><td><a href="/work/reports" target="work">Reports</a></td></tr>
<tr><td><br><a href="/logout" target="_top">Sign Off</a></td></tr>
</table>"""
        return page("nav", body, bg="#e8e8e8")

    def _notice(self, nxt: str) -> str:
        body = f"""
<table border="2" cellpadding="8" bgcolor="#ffffcc"><tr><td>
<b>SYSTEM NOTICE</b><br><br>
Core processing will be unavailable 11:00 PM - 2:00 AM ET tonight for scheduled maintenance.<br>
Transactions entered during this window will be queued.<br><br>
<form method="post" action="/work/notice/ack"><input type="hidden" name="next" value="{e(nxt)}">
<input type="submit" value="Acknowledge"></form>
</td></tr></table>"""
        return page("notice", body)

    # ---- work frame -----------------------------------------------------------------

    def _work(self, method: str, path: str, q: dict[str, str], sess: Session) -> None:
        if path == "/work/home":
            return self._send(200, self._home(sess))
        if path == "/work/inquiry":
            return self._inquiry(self._form() if method == "POST" else {})
        if path == "/work/member":
            return self._member(q.get("m", ""), sess)
        if path == "/work/survey":
            sess.survey_done = True
            return self._redirect(f"/work/member?m={urllib.parse.quote(q.get('m', ''))}")
        if path == "/work/subacct" and method == "GET":
            return self._subacct_form(q.get("m", ""), {}, "")
        if path == "/work/subacct" and method == "POST":
            return self._subacct_post(self._form(), sess)
        if path == "/work/subacct/submit" and method == "POST":
            return self._subacct_submit(self._form(), sess)
        if path == "/work/transfers":
            return self._send(
                200,
                page("transfers", "<b>TRANSFERS</b><hr>" + err("A403", "FUNCTION NOT AUTHORIZED FOR OPERATOR CLASS T1")),
            )
        if path == "/work/reports":
            return self._send(200, page("reports", "<b>REPORTS</b><hr>No reports scheduled for branch 001."))
        self._send(404, page("Not Found", err("H404", "PAGE NOT FOUND")))

    def _home(self, sess: Session) -> str:
        alert = (
            "<script>alert('Your password will expire in 3 days. Contact your administrator.');</script>"
            if self.app.faults.alert_on_home
            else ""
        )
        return page(
            "home",
            f"<b>TELLER WORKSTATION</b><hr>Welcome, {e(sess.user.upper())}. Branch 001 - MAIN.<br>"
            "Select a function from the menu at left.<p>"
            '<table border="1" cellpadding="3"><tr><td bgcolor="#ffffcc"><b>BULLETIN</b><br>'
            "Holiday Club enrollment is open through 11/30.</td></tr></table>" + alert,
        )

    def _inquiry(self, form: dict[str, str]) -> None:
        t = self.app.tenant
        mbr = form.get("fld_mbr_no", "").strip()
        lname = form.get("fld_lname", "").strip().upper()
        msg = ""
        results: list[Member] = []
        if form:
            if not mbr and not lname:
                msg = err("E1043", "ENTER MEMBER NUMBER OR LAST NAME")
            elif mbr and not (mbr.isdigit() and len(mbr) == 5):
                msg = err("E1044", "MEMBER NUMBER MUST BE 5 DIGITS")
            else:
                results = [
                    m for m in MEMBERS.values() if (mbr and m.number == mbr) or (lname and m.name.startswith(lname))
                ]
                if not results:
                    msg = '<p><font color="#990000">I2001 NO RECORDS MATCH SEARCH CRITERIA</font></p>'
        rows = "".join(
            f"<tr><td>{m.number}</td><td>{e(m.name)}</td><td>001</td>"
            f'<td><a href="/work/member?m={m.number}&amp;t={secrets.token_hex(3)}">Select</a></td></tr>'
            for m in results
        )
        table = (
            '<table border="1" cellpadding="3" cellspacing="0">'
            '<tr bgcolor="#cccccc"><td><b>Mbr No</b></td><td><b>Name</b></td><td><b>Branch</b></td><td>&nbsp;</td></tr>'
            f"{rows}</table>"
            if results
            else ""
        )
        body = f"""<b>MEMBER INQUIRY</b><hr>
<form method="post" action="/work/inquiry">
<table border="0" cellpadding="2">
<tr><td>{e(t.member_no_label)}</td><td><input type="text" name="fld_mbr_no" size="8" maxlength="6" value="{e(mbr)}"></td></tr>
<tr><td>Last Name:</td><td><input type="text" name="fld_lname" size="20" value="{e(lname)}"></td></tr>
<tr><td></td><td><input type="submit" name="btn_srch" value="{e(t.search_button)}"> <input type="reset" value="Clear"></td></tr>
</table></form>{msg}{table}"""
        self._send(200, page("inquiry", body))

    def _member(self, number: str, sess: Session) -> None:
        f = self.app.faults
        if f.slow_member_ms:
            time.sleep(f.slow_member_ms / 1000)
        if f.member_errors:
            if f.member_errors > 0:
                f.member_errors -= 1
            return self._send(
                500,
                page("Error", err("MCX-500", "SYSTEM ERROR. TRANSACTION ABORTED.") + "Reference 0x7A3F. Contact the help desk."),
            )
        if f.survey_popup and not sess.survey_done:
            body = f"""<table border="1" cellpadding="8" bgcolor="#eef6ff"><tr><td>
<b>MEMBER EXPERIENCE PROGRAM</b><br><br>This member is eligible for a satisfaction survey invitation.<br><br>
<input type="button" value="Send Survey" onclick="location.href='/work/survey?m={e(number)}&amp;send=1'">
<input type="button" value="Not Now" onclick="location.href='/work/survey?m={e(number)}'">
</td></tr></table>"""
            return self._send(200, page("program", body))
        m = MEMBERS.get(number)
        if m is None:
            return self._send(200, page("member", "<b>MEMBER DETAIL</b><hr>" + err("I2002", "MEMBER NOT ON FILE")))
        if m.restricted:
            return self._send(
                200,
                page(
                    "member",
                    "<b>MEMBER DETAIL</b><hr>" + err("R17", "ACCESS DENIED - RESTRICTED ACCOUNT. SUPERVISOR OVERRIDE REQUIRED."),
                ),
            )
        bal_hdr = self.app.tenant.balance_header
        acct_rows = "".join(
            f"<tr><td>{a.suffix}</td><td>{a.type}</td><td>{e(a.description)}</td>"
            f'<td align="right">{a.balance}</td><td align="right">{a.available}</td></tr>'
            for a in m.accounts
        )
        body = f"""<b>MEMBER DETAIL</b><hr>
<table border="0" cellpadding="2">
<tr><td>Member No:</td><td>{m.number}</td></tr>
<tr><td>Name:</td><td>{e(m.name)}</td></tr>
<tr><td>Tax ID:</td><td>***-**-{m.tax_id_last4}</td></tr>
<tr><td>Address:</td><td>{e(m.address)}</td></tr>
<tr><td>Phone:</td><td>{e(m.phone)}</td></tr>
<tr><td>Member Since:</td><td>{m.since}</td></tr>
</table><br>
<b>SHARE / LOAN ACCOUNTS</b>
<table border="1" cellpadding="3" cellspacing="0">
<tr bgcolor="#cccccc"><td><b>Sfx</b></td><td><b>Type</b></td><td><b>Description</b></td><td><b>{e(bal_hdr)}</b></td><td><b>Available</b></td></tr>
{acct_rows}</table><br>
<input type="button" value="Open Sub-Account" onclick="location.href='/work/subacct?m={m.number}'">
<input type="button" value="New Inquiry" onclick="location.href='/work/inquiry'">"""
        self._send(200, page("member", body))

    # ---- sub-account opening (multi-field form -> review -> irreversible submit) -----

    def _subacct_form(self, number: str, form: dict[str, str], msg: str) -> None:
        m = MEMBERS.get(number)
        if m is None or m.restricted:
            return self._send(
                200, page("subacct", "<b>OPEN SUB-ACCOUNT</b><hr>" + err("E2210", "SELECT A MEMBER FIRST VIA MEMBER INQUIRY"))
            )
        types = [("", "-- Select --"), ("HC", "Holiday Club"), ("VC", "Vacation Club"), ("C12", "Share Certificate 12M")]
        type_opts = "".join(
            f'<option value="{v}"{" selected" if form.get("fld_typ") == v and v else ""}>{e(label)}</option>'
            for v, label in types
        )
        src_opts = "".join(
            f'<option value="{a.suffix}"{" selected" if form.get("fld_src") == a.suffix else ""}>{a.suffix} {e(a.description)}</option>'
            for a in m.accounts
            if a.type in ("SHR", "DFT")
        )
        body = f"""<b>OPEN SUB-ACCOUNT</b> - MEMBER {m.number}<hr>{msg}
<form method="post" action="/work/subacct"><input type="hidden" name="m" value="{m.number}">
<table border="0" cellpadding="2">
<tr><td>Account Type:</td><td><select name="fld_typ">{type_opts}</select></td></tr>
<tr><td>Nickname:</td><td><input type="text" name="fld_nick" size="20" maxlength="20" value="{e(form.get("fld_nick", ""))}"></td></tr>
<tr><td>Initial Deposit:</td><td><input type="text" name="fld_amt" size="10" value="{e(form.get("fld_amt", ""))}"></td></tr>
<tr><td>Funding Source:</td><td><select name="fld_src">{src_opts}</select></td></tr>
<tr><td></td><td><input type="submit" name="btn_cont" value="Continue">
<input type="button" value="Cancel" onclick="location.href='/work/member?m={m.number}'"></td></tr>
</table></form>"""
        self._send(200, page("subacct", body))

    def _subacct_post(self, form: dict[str, str], sess: Session) -> None:
        number = form.get("m", "")
        m = MEMBERS.get(number)
        if m is None or m.restricted:
            return self._subacct_form(number, form, "")
        type_names = {"HC": "Holiday Club", "VC": "Vacation Club", "C12": "Share Certificate 12M"}
        typ = form.get("fld_typ", "")
        amt = money_ok(form.get("fld_amt", ""))
        src = next((a for a in m.accounts if a.suffix == form.get("fld_src")), None)
        if typ not in type_names:
            return self._subacct_form(number, form, err("E3101", "ACCOUNT TYPE REQUIRED"))
        if amt is None:
            return self._subacct_form(number, form, err("E3105", "INITIAL DEPOSIT MUST BE A POSITIVE AMOUNT"))
        if typ == "C12" and amt < 500:
            return self._subacct_form(number, form, err("E3110", "MINIMUM DEPOSIT FOR CERTIFICATE IS 500.00"))
        if src is None or amt > float(src.available.replace(",", "")):
            return self._subacct_form(number, form, err("E3107", "INSUFFICIENT AVAILABLE FUNDS IN SOURCE"))
        key = secrets.token_hex(4)
        sess.pending = {
            "key": key,
            "member": number,
            "type": type_names[typ],
            "nick": form.get("fld_nick", "").strip() or "-",
            "amount": f"{amt:,.2f}",
            "source": f"{src.suffix} {src.description}",
        }
        p = sess.pending
        body = f"""<b>REVIEW SUB-ACCOUNT REQUEST</b><hr>
Please verify the information below. Select SUBMIT to open the account. This action cannot be reversed.<br><br>
<table border="1" cellpadding="3" cellspacing="0">
<tr><td>Member</td><td>{e(p["member"])}</td></tr>
<tr><td>Account Type</td><td>{e(p["type"])}</td></tr>
<tr><td>Nickname</td><td>{e(p["nick"])}</td></tr>
<tr><td>Initial Deposit</td><td>{e(p["amount"])}</td></tr>
<tr><td>Funding Source</td><td>{e(p["source"])}</td></tr>
</table><br>
<form method="post" action="/work/subacct/submit"><input type="hidden" name="k" value="{key}">
<input type="submit" value="Submit">
<input type="button" value="Back" onclick="history.back()"></form>"""
        self._send(200, page("review", body))

    def _subacct_submit(self, form: dict[str, str], sess: Session) -> None:
        p = sess.pending
        if not p or form.get("k") != p["key"]:
            return self._send(200, page("subacct", err("E3199", "NO PENDING REQUEST OR REQUEST ALREADY SUBMITTED")))
        sess.pending = None
        with self.app.lock:
            suffix = f"{self.app.next_suffix:04d}"
            self.app.next_suffix += 1
        conf = f"SA-{secrets.randbelow(90000) + 10000}"
        body = f"""<b>SUB-ACCOUNT OPENED</b><hr>
<table border="0" cellpadding="2">
<tr><td>Confirmation No:</td><td>{conf}</td></tr>
<tr><td>New Suffix:</td><td>{suffix}</td></tr>
<tr><td>Account Type:</td><td>{e(p["type"])}</td></tr>
<tr><td>Initial Deposit:</td><td>{e(p["amount"])}</td></tr>
</table><br>
<input type="button" value="New Inquiry" onclick="location.href='/work/inquiry'">"""
        self._send(200, page("confirm", body))


# --------------------------------------------------------------------------------------
# Entrypoints
# --------------------------------------------------------------------------------------


def make_server(host: str, port: int, tenant: str = "harbor", faults: str = "") -> ThreadingHTTPServer:
    state = AppState(TENANTS[tenant], Faults.parse(faults))
    handler = type("BoundHandler", (Handler,), {"app": state})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve_in_thread(port: int = 0, tenant: str = "harbor", faults: str = "") -> tuple[ThreadingHTTPServer, str]:
    """Start a server on a background thread (tests). Returns (server, base_url)."""
    srv = make_server("127.0.0.1", port, tenant, faults)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="MockCore Teller — hostile legacy core-banking mock")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--tenant", choices=sorted(TENANTS), default="harbor")
    ap.add_argument("--faults", default="", help="e.g. maintenance_notice,expire_session_after=8,slow_member_ms=4000")
    a = ap.parse_args(argv)
    srv = make_server(a.host, a.port, a.tenant, a.faults)
    print(f"MockCore [{a.tenant}] listening on http://{a.host}:{a.port}  faults={a.faults or 'none'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
