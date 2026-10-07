/* Chat UI. The only things this page ever sends to the server are the chosen model id
   and the conversation. It has no generation-setting controls at all; the server decides. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const app = $("app"), thread = $("thread"), scroller = $("scroll"), input = $("input"),
        sendBtn = $("send"), modelSel = $("model"), modelNote = $("modelnote"), convList = $("convlist"),
        tplPanel = $("tplpanel"), tplSelect = $("tplselect"), tplDesc = $("tpldesc"), tplFields = $("tplfields"),
        tplPreview = $("tplpreview"), tplMissing = $("tplmissing"), tplSend = $("tplsend"), tplInsert = $("tplinsert"),
        tplBtn = $("tplbtn"), tplChip = $("tplchip"), inlineVars = $("inlinevars");

  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage blocked: keep working in memory */ } },
  };

  let cfg = null;
  let convs = store.get("convs", []);
  let activeId = store.get("active", null);
  let controller = null;
  let modelSig = "";

  const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const active = () => convs.find((c) => c.id === activeId);

  function persist() {
    convs.sort((a, b) => b.updated - a.updated);
    convs = convs.slice(0, 50);
    store.set("convs", convs);
    store.set("active", activeId);
  }

  /* ------------------------------------------------------------------ */
  /* Markdown (escape first, then format; no raw HTML ever reaches the DOM) */
  /* ------------------------------------------------------------------ */
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inline(raw) {
    const codes = [];
    let s = raw.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = esc(s);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      (_, text, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
         .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  }

  const TABLE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
  const splitRow = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const isBlockStart = (l) => /^```/.test(l) || /^#{1,6}\s/.test(l) || /^\s*([-*+]|\d+[.)])\s+/.test(l)
    || /^>/.test(l) || /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l) || l.trim() === "";

  function codeBlock(text, lang) {
    return `<div class="code"><div class="code-head"><span>${esc(lang || "code")}</span>` +
      `<button class="copy" type="button">Copy</button></div><pre><code>${esc(text)}</code></pre></div>`;
  }

  function md(src) {
    const lines = src.replace(/\r\n/g, "\n").split("\n");
    let html = "", list = null, i = 0, m;
    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };

    while (i < lines.length) {
      const line = lines[i];

      if ((m = line.match(/^```\s*([\w+#.-]*)\s*$/))) {          // fenced code (also while still streaming)
        closeList();
        const buf = []; i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++;
        html += codeBlock(buf.join("\n"), m[1]);
      } else if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {   // table
        closeList();
        const head = splitRow(line); i += 2;
        let rows = "";
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
          rows += "<tr>" + splitRow(lines[i++]).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
        }
        html += `<div class="tablewrap"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
      } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        closeList(); const n = Math.min(m[1].length + 1, 6);
        html += `<h${n}>${inline(m[2])}</h${n}>`; i++;
      } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        closeList(); html += "<hr>"; i++;
      } else if (/^>/.test(line)) {
        closeList(); const buf = [];
        while (i < lines.length && /^>/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        html += `<blockquote>${inline(buf.join("\n")).replace(/\n/g, "<br>")}</blockquote>`;
      } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
        if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(m[1])}</li>`; i++;
      } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
        html += `<li>${inline(m[1])}</li>`; i++;
      } else if (line.trim() === "") {
        closeList(); i++;
      } else {
        closeList(); const buf = [line]; i++;
        while (i < lines.length && !isBlockStart(lines[i])) buf.push(lines[i++]);
        html += `<p>${inline(buf.join("\n"))}</p>`;
      }
    }
    closeList();
    return html;
  }

  /* ------------------------------------------------------------------ */
  /* Models                                                             */
  /* ------------------------------------------------------------------ */
  const usable = (id) => cfg.models.some((m) => m.id === id && m.available);
  function pickModel(preferred) {
    if (usable(preferred)) return preferred;
    if (usable(cfg.default_model)) return cfg.default_model;
    const any = cfg.models.find((m) => m.available);
    return any ? any.id : (preferred || cfg.default_model);
  }

  function renderModelSelect() {
    const conv = active();
    modelSel.textContent = "";
    for (const m of cfg.models) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.available ? m.label : `${m.label} (unavailable)`;
      o.disabled = !m.available;
      modelSel.appendChild(o);
    }
    if (conv) modelSel.value = conv.model;
    const chosen = cfg.models.find((m) => m.id === modelSel.value);
    const offline = chosen && !chosen.available;
    modelNote.className = offline ? "warn" : "";
    modelNote.textContent = offline ? "This model is offline. Please choose another." : (chosen ? chosen.description : "");
  }

  /* ------------------------------------------------------------------ */
  /* Rendering                                                          */
  /* ------------------------------------------------------------------ */
  function renderSidebar() {
    convList.textContent = "";
    for (const c of convs) {
      const row = document.createElement("div");
      row.className = "conv" + (c.id === activeId ? " active" : "");
      const title = document.createElement("span");
      title.className = "title"; title.textContent = c.title;
      const del = document.createElement("button");
      del.className = "del"; del.type = "button"; del.textContent = "×"; del.setAttribute("aria-label", "Delete chat");
      del.addEventListener("click", (e) => { e.stopPropagation(); removeConv(c.id); });
      row.append(title, del);
      row.addEventListener("click", () => selectConv(c.id));
      convList.appendChild(row);
    }
  }

  function copyText(text, btn) {
    const done = () => { const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = old; }, 1400); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
    } else fallbackCopy(text, done);
  }
  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch { /* nothing else to try */ }
    ta.remove();
  }

  function addActions(row, text, isLast, conv) {
    const bar = document.createElement("div"); bar.className = "actions";
    const copy = document.createElement("button"); copy.type = "button"; copy.textContent = "Copy";
    copy.addEventListener("click", () => copyText(text, copy));
    bar.appendChild(copy);
    if (isLast) {
      const regen = document.createElement("button"); regen.type = "button"; regen.textContent = "Regenerate";
      regen.addEventListener("click", () => {
        if (controller) return;
        const last = conv.messages[conv.messages.length - 1];
        if (last && last.role === "assistant") conv.messages.pop();
        persist(); renderThread(); generate(conv);
      });
      bar.appendChild(regen);
    }
    row.appendChild(bar);
  }

  function addMessageEl(role, text) {
    const row = document.createElement("div"); row.className = "msg " + role;
    const body = document.createElement("div"); body.className = "content";
    if (role === "user") body.textContent = text; else body.innerHTML = md(text);
    row.appendChild(body); thread.appendChild(row);
    return { row, body };
  }

  function scrollToBottom(force) {
    const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
    if (force || near) scroller.scrollTop = scroller.scrollHeight;
  }

  function renderThread(tail) {
    const conv = active();
    thread.textContent = "";
    if (!conv || conv.messages.length === 0) {
      const box = document.createElement("div"); box.className = "empty";
      const h = document.createElement("h1"); h.textContent = cfg.greeting; box.appendChild(h);
      const wrap = document.createElement("div"); wrap.className = "suggestions";
      for (const s of cfg.suggestions || []) {
        const b = document.createElement("button"); b.type = "button"; b.className = "suggestion"; b.textContent = s;
        b.addEventListener("click", () => { input.value = s; autosize(); input.focus(); updateSend(); });
        wrap.appendChild(b);
      }
      box.appendChild(wrap); thread.appendChild(box);
      return;
    }
    conv.messages.forEach((m, idx) => {
      const { row } = addMessageEl(m.role, m.content);
      if (m.role === "assistant") addActions(row, m.content, idx === conv.messages.length - 1 && !tail, conv);
    });
    if (tail) {
      const n = document.createElement("div"); n.className = "note" + (tail.error ? " err" : "");
      n.textContent = tail.text;
      if (tail.retry) {
        const b = document.createElement("button"); b.type = "button"; b.textContent = "Try again";
        b.addEventListener("click", () => { if (!controller) { renderThread(); generate(conv); } });
        n.appendChild(b);
      }
      thread.appendChild(n);
    }
    scrollToBottom(true);
  }

  function renderAll() { renderSidebar(); renderModelSelect(); renderTplChip(); renderThread(); }

  /* ------------------------------------------------------------------ */
  /* Conversations                                                      */
  /* ------------------------------------------------------------------ */
  function newConv() {
    const cur = active();
    if (cur && cur.messages.length === 0) return cur;
    const c = { id: uid(), title: "New chat", model: pickModel(store.get("model", cfg.default_model)), messages: [], updated: Date.now() };
    convs.unshift(c); activeId = c.id; persist();
    return c;
  }
  function stopStreaming() { if (controller) controller.abort(); }
  function selectConv(id) {
    stopStreaming();
    activeId = id;
    const c = active();
    if (c && !usable(c.model)) c.model = pickModel(c.model);
    persist(); renderAll(); app.classList.remove("open");
  }
  function removeConv(id) {
    if (id === activeId) stopStreaming();
    convs = convs.filter((c) => c.id !== id);
    if (id === activeId) { activeId = convs[0] ? convs[0].id : null; if (!activeId) newConv(); }
    persist(); renderAll();
  }

  /* ------------------------------------------------------------------ */
  /* Chatting                                                           */
  /* ------------------------------------------------------------------ */
  function setBusy(busy) {
    sendBtn.textContent = busy ? "■" : "↑";
    sendBtn.classList.toggle("stop", busy);
    sendBtn.setAttribute("aria-label", busy ? "Stop generating" : "Send message");
    updateSend();
  }
  function updateSend() {
    sendBtn.disabled = !controller && !input.value.trim();
    if (tpl) tplSend.disabled = missing().length > 0 || !!controller;
  }
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; }

  async function generate(conv) {
    const { row, body } = addMessageEl("assistant", "");
    body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    scrollToBottom(true);

    controller = new AbortController();
    setBusy(true);
    let answer = "", truncated = false, error = "", frame = 0;
    const paint = () => { frame = 0; body.innerHTML = md(answer); scrollToBottom(false); };
    // If nothing has arrived after a few seconds the model is probably starting up (serverless
    // backends scale to zero). Say so instead of leaving the user staring at three dots.
    const wake = setTimeout(() => {
      if (!answer) body.innerHTML = '<span class="note">Starting up the model. The first reply can take three to five minutes.</span> <span class="typing"><i></i><i></i><i></i></span>';
    }, 8000);

    try {
      const res = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: conv.model, messages: conv.messages, template: conv.template || undefined }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        let msg = "Something went wrong. Please try again.";
        try { msg = (await res.json()).error || msg; } catch { /* keep default */ }
        throw new Error(msg);
      }
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 2);
          if (!line.startsWith("data:")) continue;
          let ev; try { ev = JSON.parse(line.slice(5)); } catch { continue; }
          if (ev.type === "token") { answer += ev.text; if (!frame) frame = requestAnimationFrame(paint); }
          else if (ev.type === "truncated") truncated = true;
          else if (ev.type === "error") error = ev.message;
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") error = e.message || "Something went wrong. Please try again.";
    }

    clearTimeout(wake);
    if (frame) cancelAnimationFrame(frame);
    const aborted = controller && controller.signal.aborted;
    controller = null; setBusy(false);

    if (answer) {
      conv.messages.push({ role: "assistant", content: answer });
      conv.updated = Date.now(); persist();
    }
    if (activeId !== conv.id) { renderSidebar(); return; }   // user switched chats mid-reply

    let tail = null;
    if (error) tail = { text: error, error: true, retry: true };
    else if (truncated) tail = { text: "This reply reached its length limit. Ask me to continue if you need more." };
    else if (aborted && !answer) tail = { text: "Stopped.", retry: true };
    renderThread(tail);
    renderSidebar();
    input.focus();
  }

  async function send(fromTemplate) {
    if (controller) { stopStreaming(); return; }
    const text = (typeof fromTemplate === "string" ? fromTemplate : input.value).trim();
    if (!text) return;
    const conv = active() || newConv();
    if (!usable(conv.model)) { conv.model = pickModel(conv.model); renderModelSelect(); }
    conv.messages.push({ role: "user", content: text });
    if (conv.title === "New chat") conv.title = text.replace(/\s+/g, " ").slice(0, 48);
    conv.updated = Date.now(); persist();
    if (typeof fromTemplate !== "string") { input.value = ""; autosize(); }
    updateSend(); checkInlineVars();
    renderSidebar(); renderTplChip(); renderThread();
    generate(conv);
  }

  /* ------------------------------------------------------------------ */
  /* Prompt templates: {{variable}} fields filled in before sending      */
  /* ------------------------------------------------------------------ */
  const VAR_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;
  const LONG_FIELD = /(text|code|content|body|points|notes|context|document|details|input|draft|description|interests)/i;
  const varsOf = (t) => [...new Set([...t.matchAll(VAR_RE)].map((m) => m[1]))];
  const pretty = (n) => n.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  const templates = () => (cfg && cfg.templates) || [];
  let tpl = null;            // template shown in the panel ({id, name, template, ...}; id null = ad-hoc)
  let values = {};           // field values, kept by name so switching templates keeps shared ones

  function fillIn(text) { return text.replace(VAR_RE, (m, n) => ((values[n] || "").trim() ? values[n] : m)); }
  function missing() { return tpl ? varsOf(tpl.template).filter((n) => !(values[n] || "").trim()) : []; }

  function renderTplChip() {
    const c = active(), t = c && c.template && templates().find((x) => x.id === c.template);
    tplChip.hidden = !t;
    if (t) tplChip.textContent = `Template: ${t.name}  ×`;
  }

  function fillTplSelect() {
    tplSelect.textContent = "";
    const opts = templates().map((t) => [t.id, t.name]);
    if (tpl && !tpl.id) opts.push(["", "From your message"]);
    for (const [id, name] of opts) {
      const o = document.createElement("option"); o.value = id; o.textContent = name; tplSelect.appendChild(o);
    }
    tplSelect.value = tpl ? (tpl.id || "") : "";
  }

  function openTemplate(t) {
    tpl = t;
    if (!t) { tplPanel.hidden = true; tplBtn.classList.remove("on"); return; }
    tplPanel.hidden = false; tplBtn.classList.add("on");
    fillTplSelect();
    tplDesc.textContent = t.description || "";
    tplFields.textContent = "";
    const names = varsOf(t.template);
    if (!names.length) {
      const p = document.createElement("p"); p.className = "tpl-desc"; p.textContent = "This template has no fields to fill in.";
      tplFields.appendChild(p);
    }
    for (const n of names) {
      const wrap = document.createElement("label"); wrap.className = "tpl-field";
      const long = LONG_FIELD.test(n);
      if (long) wrap.classList.add("wide");
      const cap = document.createElement("span"); cap.textContent = pretty(n);
      const el = document.createElement(long ? "textarea" : "input");
      el.dataset.var = n; el.value = values[n] || "";
      if (!long) el.type = "text";
      el.maxLength = (cfg && cfg.max_message_chars) || 8000;
      el.addEventListener("input", () => { values[n] = el.value; updatePreview(); });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (!tplSend.disabled) tplSend.click(); }
      });
      wrap.append(cap, el); tplFields.appendChild(wrap);
    }
    updatePreview();
    const first = tplFields.querySelector("input, textarea");
    if (first) first.focus();
  }

  function updatePreview() {
    if (!tpl) return;
    tplPreview.textContent = "";
    let last = 0;
    for (const m of tpl.template.matchAll(VAR_RE)) {
      tplPreview.appendChild(document.createTextNode(tpl.template.slice(last, m.index)));
      const v = (values[m[1]] || "").trim();
      const span = document.createElement("span");
      span.className = v ? "v" : "v miss";
      span.textContent = v ? values[m[1]] : `{{${m[1]}}}`;
      tplPreview.appendChild(span);
      last = m.index + m[0].length;
    }
    tplPreview.appendChild(document.createTextNode(tpl.template.slice(last)));
    const miss = missing();
    tplMissing.textContent = miss.length ? `Still to fill in: ${miss.map(pretty).join(", ")}` : "All fields filled in";
    tplMissing.classList.toggle("miss", miss.length > 0);
    tplSend.disabled = miss.length > 0 || !!controller;
  }

  function useTemplateFor(conv) {
    // A saved template's own system prompt (kept on the server) applies to this whole chat.
    if (tpl && tpl.id) { conv.template = tpl.id; persist(); renderTplChip(); }
  }

  function tplFillAndSend() {
    if (!tpl || missing().length || controller) return;
    const text = fillIn(tpl.template);
    let conv = active() || newConv();
    if (!usable(conv.model)) conv.model = pickModel(conv.model);
    useTemplateFor(conv);
    openTemplate(null);
    send(text);
  }

  function tplInsertIntoMessage() {
    if (!tpl) return;
    useTemplateFor(active() || newConv());
    input.value = fillIn(tpl.template);
    openTemplate(null);
    autosize(); updateSend(); checkInlineVars(); input.focus();
  }

  // Typing {{fields}} straight into the message box offers the same fill-in form.
  function checkInlineVars() {
    const names = varsOf(input.value);
    inlineVars.hidden = !names.length;
    if (!names.length) return;
    inlineVars.textContent = `${names.length} fill-in field${names.length > 1 ? "s" : ""} in your message. `;
    const b = document.createElement("button"); b.type = "button"; b.textContent = "Fill them in";
    b.addEventListener("click", () => {
      openTemplate({ id: null, name: "From your message", description: "", template: input.value });
      input.value = ""; autosize(); updateSend(); checkInlineVars();
    });
    inlineVars.appendChild(b);
  }

  /* ------------------------------------------------------------------ */
  /* Wiring                                                             */
  /* ------------------------------------------------------------------ */
  input.addEventListener("input", () => { autosize(); updateSend(); checkInlineVars(); });
  tplBtn.addEventListener("click", () => {
    if (!tplPanel.hidden) { openTemplate(null); return; }
    const list = templates();
    if (!list.length) { input.placeholder = "No prompt templates are configured on the server"; return; }
    const c = active();
    openTemplate(list.find((t) => c && t.id === c.template) || list[0]);
  });
  tplSelect.addEventListener("change", () => openTemplate(templates().find((t) => t.id === tplSelect.value) || tpl));
  $("tplclose").addEventListener("click", () => { openTemplate(null); input.focus(); });
  tplSend.addEventListener("click", tplFillAndSend);
  tplInsert.addEventListener("click", tplInsertIntoMessage);
  tplChip.addEventListener("click", () => { const c = active(); if (c) { c.template = null; persist(); renderTplChip(); } });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !tplPanel.hidden) openTemplate(null); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener("click", () => send());
  $("newchat").addEventListener("click", () => { stopStreaming(); const nc = newConv(); nc.template = null; persist(); renderAll(); app.classList.remove("open"); input.focus(); });
  $("menu").addEventListener("click", () => app.classList.add("open"));
  $("scrim").addEventListener("click", () => app.classList.remove("open"));
  modelSel.addEventListener("change", () => {
    const c = active(); if (!c) return;
    c.model = modelSel.value; store.set("model", c.model); persist(); renderModelSelect();
  });
  thread.addEventListener("click", (e) => {          // copy buttons inside rendered code blocks
    const btn = e.target.closest(".copy");
    if (!btn) return;
    const code = btn.closest(".code").querySelector("code");
    copyText(code.textContent, btn);
  });

  async function fetchConfig() {
    const res = await fetch("/api/config", { cache: "no-store" });
    if (!res.ok) throw new Error("config");
    return res.json();
  }
  const signature = (c) => c.models.map((m) => m.id + ":" + m.available).join("|");

  async function init() {
    try { cfg = await fetchConfig(); }
    catch {
      thread.innerHTML = '<div class="empty"><h1>Can’t reach the service</h1><div class="note">Retrying…</div></div>';
      setTimeout(init, 3000); return;
    }
    document.title = cfg.title; $("brand").textContent = cfg.title;
    input.maxLength = cfg.max_message_chars || 8000;
    modelSig = signature(cfg);

    if (!active()) activeId = convs[0] ? convs[0].id : null;
    if (!active()) newConv();
    const c = active();
    if (!cfg.models.some((m) => m.id === c.model) || !usable(c.model)) c.model = pickModel(c.model);
    persist(); renderAll(); setBusy(false); input.focus();

    setInterval(async () => {                         // keep the picker honest about which models are up
      try {
        const next = await fetchConfig();
        if (signature(next) !== modelSig) { cfg = next; modelSig = signature(next); renderModelSelect(); }
      } catch { /* transient: try again next tick */ }
    }, 30000);
  }
  init();
})();