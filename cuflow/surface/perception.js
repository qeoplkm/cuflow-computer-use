// cuflow perception library — injected into every frame (add_init_script).
//
// One piece of code is used for *both* discovery (describe what the model clicked) and
// replay (find it again). Keeping them identical is what makes recorded locators replayable.
//
// It derives an accessibility-style view (role + accessible name) from markup that has
// none: legacy pages with layout tables, no <label>s and no ids. When a control has no
// accessible name we infer one the way a human would read the screen: the text in the
// preceding cell of the same table row, or the text immediately before it.
(() => {
  if (window.__cu) return;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const cleanLabel = (s) => norm(s).replace(/[\s:*]+$/, "").trim();
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "TITLE", "FRAME", "IFRAME", "OPTION"]);
  const BLOCK = new Set(["DIV", "P", "TABLE", "TBODY", "THEAD", "TFOOT", "TR", "TD", "TH", "UL", "OL", "LI", "FORM", "CENTER",
    "H1", "H2", "H3", "H4", "H5", "H6", "BLOCKQUOTE", "PRE", "CAPTION", "FIELDSET", "SECTION", "ARTICLE", "MAIN", "NAV", "HEADER", "FOOTER", "BODY", "DL", "DT", "DD"]);
  const TEXT_BLOCK = new Set(["TD", "TH", "P", "LI", "H1", "H2", "H3", "H4", "H5", "H6", "DIV", "CAPTION", "DT", "DD", "PRE"]);
  const CONTROL_SEL = "a[href], a[onclick], button, input, select, textarea, [onclick], [role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=textbox], [role=combobox]";

  // ---------------------------------------------------------------- settle tracking
  let lastMutation = Date.now();
  try {
    new MutationObserver(() => { lastMutation = Date.now(); })
      .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  } catch (e) { /* document not ready; ignore */ }

  // ---------------------------------------------------------------- primitives
  const visible = (el) => {
    if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const textOf = (el) => norm(el.innerText !== undefined ? el.innerText : el.textContent);
  const isControl = (el) => {
    if (!el.matches(CONTROL_SEL)) return false;
    if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "hidden") return false;
    if (el.tagName === "A" && !el.hasAttribute("href") && !el.hasAttribute("onclick")) return false;
    return true;
  };
  const hasControl = (el) => !!el.querySelector(CONTROL_SEL);
  const hasBlockChild = (el) => Array.from(el.querySelectorAll("*")).some((c) => BLOCK.has(c.tagName));
  const isTextBlock = (el) => TEXT_BLOCK.has(el.tagName) && !hasControl(el) && !hasBlockChild(el) && textOf(el).length > 0;

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.split(/\s+/)[0];
    const tag = el.tagName;
    const type = (el.type || "").toLowerCase();
    if (tag === "A") return "link";
    if (tag === "BUTTON") return "button";
    if (tag === "SELECT") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT") {
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (/^H[1-6]$/.test(tag)) return "heading";
    if (tag === "TD" || tag === "TH") return "cell";
    if (el.hasAttribute("onclick")) return "button";
    return "text";
  }

  function inferLabel(el) {
    // 1) table form layout: nearest preceding cell in the same row that holds only text
    const cell = el.closest("td,th");
    if (cell) {
      for (let c = cell.previousElementSibling; c; c = c.previousElementSibling) {
        if (hasControl(c)) continue;
        const t = cleanLabel(textOf(c));
        if (t) return [t, "row-label"];
      }
    }
    // 2) inline form layout: text immediately before the control
    let n = el.previousSibling;
    for (let i = 0; n && i < 6; i++, n = n.previousSibling) {
      if (n.nodeType === 3 && norm(n.textContent)) return [cleanLabel(n.textContent), "preceding-text"];
      if (n.nodeType === 1) {
        if (n.matches && n.matches(CONTROL_SEL)) break;
        const t = cleanLabel(textOf(n));
        if (t) return [t, "preceding-text"];
      }
    }
    return null;
  }

  // Simplified accessible-name computation with an explicit "where did the name come from".
  function nameOf(el) {
    const doc = el.ownerDocument;
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = norm(lb.split(/\s+/).map((id) => doc.getElementById(id)).filter(Boolean).map(textOf).join(" "));
      if (t) return [t, "aria-labelledby"];
    }
    const al = norm(el.getAttribute("aria-label"));
    if (al) return [al, "aria-label"];
    const tag = el.tagName;
    const type = (el.type || "").toLowerCase();
    if (tag === "INPUT" && ["submit", "button", "reset"].includes(type)) {
      return [norm(el.value) || (type === "reset" ? "Reset" : "Submit"), "value"];
    }
    if (tag === "INPUT" && type === "image") return [norm(el.alt), "alt"];
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
      if (el.id) {
        const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l && textOf(l)) return [cleanLabel(textOf(l)), "label"];
      }
      const wrap = el.closest("label");
      if (wrap && textOf(wrap)) return [cleanLabel(textOf(wrap)), "label"];
      if (norm(el.title)) return [norm(el.title), "title"];
      const inferred = inferLabel(el);
      if (inferred) return inferred;
      if (norm(el.placeholder)) return [norm(el.placeholder), "placeholder"];
      return ["", "none"];
    }
    const c = textOf(el);
    if (c) return [c.slice(0, 160), "content"];
    if (norm(el.title)) return [norm(el.title), "title"];
    const img = el.querySelector && el.querySelector("img[alt]");
    if (img && norm(img.alt)) return [norm(img.alt), "alt"];
    return ["", "none"];
  }

  function cssPath(el) {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n.tagName !== "HTML"; n = n.parentElement) {
      const tag = n.tagName.toLowerCase();
      const parent = n.parentElement;
      if (!parent) { parts.unshift(tag); break; }
      const same = Array.from(parent.children).filter((c) => c.tagName === n.tagName);
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(n) + 1})` : tag);
    }
    return parts.join(" > ");
  }

  function tableCtx(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    const tr = cell.parentElement;
    const table = cell.closest("table");
    if (!tr || !table) return null;
    const rows = Array.from(table.rows);
    const header = rows.length ? Array.from(rows[0].cells).map((c) => cleanLabel(textOf(c))) : [];
    const rowCells = Array.from(tr.cells).map((c) => cleanLabel(textOf(c)));
    const col = Array.from(tr.cells).indexOf(cell);
    return { row: rows.indexOf(tr), col, header, row_cells: rowCells, n_rows: rows.length };
  }

  function valueOf(el) {
    const tag = el.tagName;
    const type = (el.type || "").toLowerCase();
    if (tag === "INPUT" && type === "password") return el.value ? "<redacted:password>" : "";
    if (tag === "INPUT" && ["checkbox", "radio"].includes(type)) return el.checked ? "checked" : "unchecked";
    if (tag === "SELECT") return el.selectedIndex >= 0 ? norm(el.options[el.selectedIndex].text) : "";
    if (tag === "INPUT" || tag === "TEXTAREA") return el.value || "";
    return null;
  }

  function info(el, ref) {
    const [name, nameSource] = nameOf(el);
    const r = el.getBoundingClientRect();
    const out = {
      ref, role: roleOf(el), name, name_source: nameSource, tag: el.tagName.toLowerCase(),
      type: (el.type || "").toLowerCase() || null,
      attrs: {}, value: valueOf(el), disabled: !!el.disabled,
      bbox: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      css: cssPath(el), table: tableCtx(el), control: isControl(el),
    };
    for (const a of ["name", "id", "href", "title", "target", "onclick"]) {
      const v = el.getAttribute(a);
      if (v !== null && v !== "") out.attrs[a] = v.slice(0, 300);
    }
    if (el.tagName === "SELECT") out.options = Array.from(el.options).map((o) => norm(o.text));
    if (el.tagName === "INPUT" && ["submit", "button", "reset"].includes(out.type)) out.attrs.value = el.value;
    if (el.form) {
      const btn = el.form.querySelector("input[type=submit], button:not([type]), button[type=submit]");
      if (btn) out.form_submit_name = nameOf(btn)[0];
    }
    return out;
  }

  // ---------------------------------------------------------------- ref registry
  let refs = new Map();
  const register = (el, ref) => { refs.set(ref, el); return ref; };

  // ---------------------------------------------------------------- snapshot
  function snapshot(prefix) {
    refs = new Map();
    const elements = [];
    const lines = [];
    let cur = [];
    let n = 0;
    const flush = () => { const s = norm(cur.join(" ")); if (s) lines.push(s); cur = []; };
    const add = (el) => {
      const ref = register(el, `${prefix}${++n}`);
      const i = info(el, ref);
      elements.push(i);
      return i;
    };
    const inline = (i) => {
      let s = `[${i.ref}] ${i.role} "${i.name}"`;
      if (i.role === "textbox") s += ` value="${i.value || ""}"`;
      if (i.options) s += ` options=[${i.options.join(" | ")}] selected="${i.value}"`;
      if (i.role === "checkbox" || i.role === "radio") s += ` ${i.value}`;
      if (i.disabled) s += " (disabled)";
      return s;
    };
    const walk = (node) => {
      if (node.nodeType === 3) { const t = norm(node.textContent); if (t) cur.push(t); return; }
      if (node.nodeType !== 1) return;
      const el = node;
      if (SKIP.has(el.tagName)) return;
      if (el.tagName === "FRAMESET") { flush(); lines.push(`(frameset: ${Array.from(el.ownerDocument.querySelectorAll("frame")).map((f) => f.name || f.src).join(", ")})`); return; }
      if (!visible(el)) return;
      if (isControl(el)) { cur.push(inline(add(el))); return; }
      if (el.tagName === "TR") {
        flush();
        Array.from(el.cells).forEach((c, idx) => { if (idx > 0) cur.push("|"); walk(c); });
        flush();
        return;
      }
      if (el.tagName === "BR") { flush(); return; }
      if (el.tagName === "HR") { flush(); lines.push("────"); return; }
      const block = BLOCK.has(el.tagName) && el.tagName !== "TD" && el.tagName !== "TH";
      if (block) flush();
      if (isTextBlock(el)) cur.push(`[${add(el).ref}]`);
      for (const ch of el.childNodes) walk(ch);
      if (block) flush();
    };
    const root = document.body || document.documentElement;
    if (root) walk(root);
    flush();
    return {
      url: location.href, title: document.title, ready: document.readyState,
      quiet_ms: Date.now() - lastMutation, lines, elements,
    };
  }

  // ---------------------------------------------------------------- resolution
  function candidates() {
    const out = [];
    const all = document.querySelectorAll("*");
    for (const el of all) {
      if (SKIP.has(el.tagName)) continue;
      if ((isControl(el) || isTextBlock(el)) && visible(el)) out.push(el);
    }
    return out;
  }

  function textMatch(actual, expected, mode) {
    const a = norm(actual).toLowerCase();
    const e = norm(expected).toLowerCase();
    if (mode === "contains") return a.includes(e);
    if (mode === "regex") return new RegExp(expected, "i").test(norm(actual));
    return a === e;
  }

  function globToRegex(glob) {
    const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, "\\?");
    return new RegExp("^" + esc + "$");
  }

  function resolve(loc) {
    let found = [];
    if (loc.by === "role") {
      found = candidates().filter((el) => roleOf(el) === loc.role && textMatch(nameOf(el)[0], loc.name, loc.match || "exact"));
      if (loc.row_text) {
        // disambiguate repeated controls ("Select" in every result row) by the row they sit in
        found = found.filter((el) => { const tr = el.closest("tr"); return tr && textMatch(textOf(tr), loc.row_text, "contains"); });
      }
    } else if (loc.by === "attr") {
      found = candidates().filter((el) => Object.entries(loc.attrs).every(([k, v]) => {
        const actual = k === "value" ? el.value : el.getAttribute(k);
        if (actual === null || actual === undefined) return false;
        return k === "href" ? globToRegex(v).test(actual) : actual === v;
      }));
      if (loc.tag) found = found.filter((el) => el.tagName.toLowerCase() === loc.tag);
    } else if (loc.by === "css") {
      try { found = Array.from(document.querySelectorAll(loc.css)).filter(visible); } catch (e) { found = []; }
    } else if (loc.by === "text") {
      found = candidates().filter((el) => isTextBlock(el) && textMatch(textOf(el), loc.text, loc.match || "exact"));
    } else if (loc.by === "table_cell") {
      for (const table of document.querySelectorAll("table")) {
        if (!visible(table)) continue;
        const rows = Array.from(table.rows);
        if (rows.length < 2) continue;
        if (loc.row_label) {
          // key/value layout: "Label: | value"
          for (const tr of rows) {
            const cells = Array.from(tr.cells);
            if (cells.length >= 2 && textMatch(cleanLabel(textOf(cells[0])), loc.row_label, "exact")) found.push(cells[loc.offset || 1]);
          }
          continue;
        }
        const header = Array.from(rows[0].cells).map((c) => cleanLabel(textOf(c)).toLowerCase());
        const colIdx = header.indexOf(norm(loc.column).toLowerCase());
        if (colIdx < 0) continue;
        let keyIdx = -1;
        if (loc.row_key) { keyIdx = header.indexOf(norm(loc.row_key.column).toLowerCase()); if (keyIdx < 0) continue; }
        for (const tr of rows.slice(1)) {
          const cells = Array.from(tr.cells);
          if (keyIdx >= 0 && !(cells[keyIdx] && textMatch(textOf(cells[keyIdx]), loc.row_key.value, loc.row_key.match || "exact"))) continue;
          if (cells[colIdx]) found.push(cells[colIdx]);
        }
      }
      found = found.filter(Boolean);
    }
    refs = new Map();
    return found.slice(0, 20).map((el, i) => info(el, register(el, `r${i + 1}`)));
  }

  // ---------------------------------------------------------------- recording helpers
  // Candidate locators for an element (most semantic first). Python verifies uniqueness.
  function describe(ref) {
    const el = refs.get(ref);
    if (!el || !el.isConnected) return null;
    const i = info(el, ref);
    const locs = [];
    if (i.name) locs.push({ by: "role", role: i.role, name: i.name });
    if (i.table && i.role === "cell") {
      const t = i.table;
      if (t.row > 0 && t.header[t.col]) locs.push({ by: "table_cell", column: t.header[t.col], row_key: null });
      if (t.col >= 1 && t.row_cells[0]) locs.push({ by: "table_cell", row_label: t.row_cells[0], offset: t.col });
    }
    const stable = {};
    if (i.attrs.name) stable.name = i.attrs.name;
    if (i.attrs.id) stable.id = i.attrs.id;
    if (Object.keys(stable).length) locs.push({ by: "attr", tag: i.tag, attrs: stable });
    if (i.attrs.href && !i.attrs.href.startsWith("javascript:")) locs.push({ by: "attr", tag: i.tag, attrs: { href: i.attrs.href } });
    locs.push({ by: "css", css: i.css });
    return { info: i, locators: locs };
  }

  // Values of key/value rows whose label looks like PII; used for label-driven log redaction.
  const PII_LABEL = /\b(name|address|phone|e-?mail|tax id|ssn|social|birth|dob|license|member|account number)\b/i;
  function sensitiveTexts() {
    const out = [];
    for (const tr of document.querySelectorAll("tr")) {
      const cells = Array.from(tr.cells);
      if (cells.length === 2 && PII_LABEL.test(cleanLabel(textOf(cells[0])))) {
        const v = textOf(cells[1]);
        if (v && v.length >= 3) out.push(v);
      }
    }
    // Search-result grids carry names under a header rather than beside a label.
    for (const table of document.querySelectorAll("table")) {
      const rows = Array.from(table.rows);
      if (!rows.length) continue;
      const header = Array.from(rows[0].cells);
      if (header.length < 3 || header.some(cell => cell.querySelector('input,select,textarea,table'))) continue;
      header.forEach((cell, index) => {
        if (!PII_LABEL.test(cleanLabel(textOf(cell)))) return;
        for (const row of rows.slice(1)) {
          const value = row.cells[index] && textOf(row.cells[index]);
          if (value && value.length >= 3) out.push(value);
        }
      });
    }
    for (const input of document.querySelectorAll("input:not([type=submit]):not([type=button]):not([type=reset]), textarea")) {
      if (input.value && input.value.length >= 3) out.push(input.value);
    }
    return out;
  }

  function textPresent(pattern, isRegex) {
    const body = norm(document.body ? document.body.innerText : "");
    if (isRegex) return new RegExp(pattern, "i").test(body);
    return body.toLowerCase().includes(norm(pattern).toLowerCase());
  }

  // Visible error-ish text, to report *what was on screen* when we cannot classify a state.
  function alerts() {
    const out = [];
    for (const el of document.querySelectorAll("font[color], [role=alert], .error, .err, .msg")) {
      const c = (el.getAttribute("color") || "").toLowerCase();
      if (el.tagName === "FONT" && !/^(red|#?(ff0000|990000|cc0000|c00))$/.test(c)) continue;
      const t = textOf(el);
      if (t && visible(el)) out.push(t);
    }
    return out;
  }

  // Human-action capture (control handed to an operator): report clicks/changes with the
  // same descriptors we record for the model, so human steps become reviewable steps too.
  function humanEvent(type, el) {
    try {
      const target = el.closest(CONTROL_SEL) || el.closest("td,th") || el;
      const ref = register(target, `h${Date.now()}`);
      const d = describe(ref);
      if (!d) return;
      if (window.__cuHumanEvent) window.__cuHumanEvent({ type, frame: window.name || "", url: location.href, ...d });
    } catch (e) { /* never break the page */ }
  }
  document.addEventListener("click", (ev) => humanEvent("click", ev.target), true);
  document.addEventListener("change", (ev) => humanEvent("change", ev.target), true);

  window.__cu = {
    snapshot, resolve, describe, sensitiveTexts, textPresent, alerts,
    quietMs: () => Date.now() - lastMutation,
    ready: () => document.readyState,
    el: (ref) => refs.get(ref) || null,
    maskStyle: (on) => {
      let s = document.getElementById("__cu_mask");
      if (on && !s) { s = document.createElement("style"); s.id = "__cu_mask"; document.head && document.head.appendChild(s); }
      if (s) s.textContent = on ? "[data-cu-mask]{background:#000!important;color:#000!important;}" : "";
    },
    maskPii: (on) => {
      for (const tr of document.querySelectorAll("tr")) {
        const cells = Array.from(tr.cells);
        if (cells.length === 2 && PII_LABEL.test(cleanLabel(textOf(cells[0])))) {
          if (on) cells[1].setAttribute("data-cu-mask", "1"); else cells[1].removeAttribute("data-cu-mask");
        }
      }
      for (const p of document.querySelectorAll("input:not([type=submit]):not([type=button]):not([type=reset]), textarea")) {
        if (on) p.setAttribute("data-cu-mask", "1"); else p.removeAttribute("data-cu-mask");
      }
    },
    // Black out leaf text that is a known sensitive value or looks like a money amount.
    maskData: (values) => {
      const money = /^-?\$?(\d{1,3}(,\d{3})+|\d+)\.\d{2}-?$/;
      for (const el of document.querySelectorAll("td,th,p,li,span,font,b,div,dd")) {
        if (el.children.length) continue;
        const t = textOf(el);
        if (t && (money.test(t) || values.some((v) => v && t.includes(v)))) el.setAttribute("data-cu-mask", "1");
      }
    },
    unmaskAll: () => {
      for (const el of document.querySelectorAll("[data-cu-mask]")) el.removeAttribute("data-cu-mask");
    },
    mark: (ref, on) => { const el = refs.get(ref); if (el) { if (on) el.setAttribute("data-cu-mask", "1"); else el.removeAttribute("data-cu-mask"); } },
  };
})();
