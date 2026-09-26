/*
 * Research assistant chat widget. Add one tag to any page:
 *
 *   <script src="/api/research-agent/widget.js" defer></script>
 *
 * Optional attributes on the tag:
 *   data-endpoint   API prefix (default: the folder this script was loaded from)
 *   data-title      panel title (default: "Research assistant")
 *   data-greeting   first message shown in an empty chat
 *
 * For single-page apps, send navigation through your router instead of a full
 * page load (set this before or after the script loads):
 *   window.ResearchAgent = window.ResearchAgent || {};
 *   window.ResearchAgent.navigate = (path) => router.push(path);
 *
 * The conversation is kept in sessionStorage, so it survives moving between pages.
 */
(function () {
  "use strict";
  if (window.ResearchAgent && window.ResearchAgent._loaded) return;
  var preset = window.ResearchAgent || {};

  var script = document.currentScript;
  var attr = function (name, fallback) {
    return (script && script.getAttribute(name)) || fallback;
  };
  var endpoint = attr("data-endpoint", script ? script.src.replace(/\/widget\.js(\?.*)?$/, "") : "/api/research-agent");
  var title = attr("data-title", "Research assistant");
  var greeting = attr(
    "data-greeting",
    "Hi! Ask me to research anything about transportation and logistics, or tell me where you want to go on the site."
  );
  var STORE_KEY = "research-agent:" + endpoint;

  // -- state ---------------------------------------------------------------------

  var state = { sessionId: null, open: false, messages: [] };
  try {
    var saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
    if (saved && Array.isArray(saved.messages)) state = saved;
  } catch (e) {}
  function persist() {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (e) {}
  }
  var busy = false;

  // -- markdown (escape first, then add a small, safe set of formatting) ------------

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function safeUrl(url) {
    if (/^https?:\/\//i.test(url)) return url;
    if (url.charAt(0) === "/" && url.charAt(1) !== "/") return url;
    return null;
  }
  function fmt(html) {
    return html
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }
  function inline(text) {
    var out = "";
    var last = 0;
    var re = /\[([^\]]+)\]\(([^)\s]+)\)/g;
    var m;
    while ((m = re.exec(text))) {
      out += fmt(esc(text.slice(last, m.index)));
      var url = safeUrl(m[2]);
      if (url) {
        var external = url.charAt(0) !== "/";
        out +=
          '<a href="' + esc(url) + '"' + (external ? ' target="_blank" rel="noopener noreferrer"' : "") + ">" +
          fmt(esc(m[1])) + "</a>";
      } else {
        out += fmt(esc(m[0]));
      }
      last = re.lastIndex;
    }
    return out + fmt(esc(text.slice(last)));
  }
  function markdown(src) {
    var lines = String(src).replace(/\r/g, "").split("\n");
    var html = "";
    var para = [];
    var list = null;
    var table = [];
    function flushPara() {
      if (para.length) html += "<p>" + para.map(inline).join("<br>") + "</p>";
      para = [];
    }
    function flushList() {
      if (list) html += "<" + list.tag + ">" + list.items.map(function (i) { return "<li>" + inline(i) + "</li>"; }).join("") + "</" + list.tag + ">";
      list = null;
    }
    function flushTable() {
      if (!table.length) return;
      var rows = table.filter(function (r) { return !/^\|?\s*:?-{2,}/.test(r); }).map(function (r) {
        return r.replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
      });
      html += "<table>" + rows.map(function (cells, i) {
        var tag = i === 0 ? "th" : "td";
        return "<tr>" + cells.map(function (c) { return "<" + tag + ">" + inline(c) + "</" + tag + ">"; }).join("") + "</tr>";
      }).join("") + "</table>";
      table = [];
    }
    function flushAll() { flushPara(); flushList(); flushTable(); }

    lines.forEach(function (line) {
      var m;
      if (/^\s*\|/.test(line)) { flushPara(); flushList(); table.push(line.trim()); return; }
      flushTable();
      if (!line.trim()) { flushPara(); flushList(); return; }
      if ((m = line.match(/^#{1,6}\s+(.*)$/))) { flushAll(); html += "<p class=\"h\">" + inline(m[1]) + "</p>"; return; }
      if ((m = line.match(/^\s*[-*•]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        var tag = /^\s*\d/.test(line) ? "ol" : "ul";
        flushPara();
        if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
        list.items.push(m[1]);
        return;
      }
      if (/^\s*(---|\*\*\*)\s*$/.test(line)) { flushAll(); return; }
      flushList();
      para.push(line);
    });
    flushAll();
    return html;
  }

  // -- DOM -----------------------------------------------------------------------

  var host = document.createElement("div");
  host.id = "research-agent-widget";
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    "<style>" +
    ":host{--bg:#fff;--fg:#1c2230;--muted:#667085;--line:#e3e6ec;--accent:#1f5eff;--accent-fg:#fff;--bubble:#f3f5f9;" +
    "all:initial;font:14px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:var(--fg)}" +
    "@media (prefers-color-scheme:dark){:host{--bg:#171b24;--fg:#e8ebf2;--muted:#9aa3b5;--line:#2c3342;--accent:#5b8cff;--bubble:#222837}}" +
    ".fab{position:fixed;right:20px;bottom:20px;z-index:2147483000;display:flex;align-items:center;gap:8px;border:0;border-radius:999px;" +
    "padding:12px 18px;background:var(--accent);color:var(--accent-fg);font:600 14px/1 inherit;font-family:inherit;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.2)}" +
    ".panel{position:fixed;right:20px;bottom:20px;z-index:2147483001;width:400px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 40px);" +
    "display:none;flex-direction:column;background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.25);overflow:hidden}" +
    ".panel.open{display:flex}" +
    "header{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line);font-weight:600}" +
    "header span{flex:1}" +
    "header button{border:0;background:none;color:var(--muted);cursor:pointer;font:inherit;font-size:13px;padding:4px 6px;border-radius:6px}" +
    "header button:hover{color:var(--fg);background:var(--bubble)}" +
    ".log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}" +
    ".msg{max-width:92%;padding:9px 12px;border-radius:12px;overflow-wrap:anywhere}" +
    ".user{align-self:flex-end;background:var(--accent);color:var(--accent-fg);white-space:pre-wrap}" +
    ".bot{align-self:flex-start;background:var(--bubble)}" +
    ".bot.streaming{white-space:pre-wrap}" +
    ".bot p{margin:0 0 8px}.bot p:last-child{margin-bottom:0}.bot .h{font-weight:700}" +
    ".bot ul,.bot ol{margin:0 0 8px;padding-left:20px}" +
    ".bot a{color:var(--accent)}" +
    ".bot code{font-family:ui-monospace,monospace;font-size:12px;background:var(--line);padding:1px 4px;border-radius:4px}" +
    ".bot table{border-collapse:collapse;margin:0 0 8px;font-size:13px}.bot td,.bot th{border:1px solid var(--line);padding:3px 6px;text-align:left}" +
    ".error{color:#c0392b}" +
    "details{margin-top:8px;font-size:12px;color:var(--muted)}details ol{margin:4px 0 0;padding-left:18px}" +
    ".go{display:block;margin-top:10px;border:0;border-radius:8px;padding:7px 12px;background:var(--accent);color:var(--accent-fg);font:600 13px/1.2 inherit;font-family:inherit;cursor:pointer}" +
    ".status{font-size:12px;color:var(--muted);padding:0 14px 8px;min-height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
    ".status.on::before{content:'';display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:var(--accent);animation:p 1s infinite alternate}" +
    "@keyframes p{from{opacity:.25}to{opacity:1}}" +
    "form{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line)}" +
    "textarea{flex:1;resize:none;border:1px solid var(--line);border-radius:10px;padding:8px 10px;font:inherit;color:var(--fg);background:var(--bg);max-height:120px}" +
    "textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}" +
    "form button{border:0;border-radius:10px;padding:0 14px;background:var(--accent);color:var(--accent-fg);font:600 14px inherit;font-family:inherit;cursor:pointer}" +
    "form button:disabled{opacity:.5;cursor:default}" +
    "@media (max-width:480px){.panel{right:0;bottom:0;width:100vw;max-width:100vw;height:100vh;max-height:100vh;border-radius:0}}" +
    "</style>" +
    '<button class="fab" type="button" aria-label="Open ' + esc(title) + '">✦ ' + esc(title) + "</button>" +
    '<section class="panel" role="dialog" aria-label="' + esc(title) + '">' +
    "<header><span>" + esc(title) + '</span><button type="button" class="new">New chat</button>' +
    '<button type="button" class="close" aria-label="Close">✕</button></header>' +
    '<div class="log" aria-live="polite"></div>' +
    '<div class="status" aria-live="polite"></div>' +
    '<form><textarea rows="1" placeholder="Ask about freight, carriers, regulations…" aria-label="Message"></textarea>' +
    '<button type="submit">Send</button></form>' +
    "</section>";

  var fab = root.querySelector(".fab");
  var panel = root.querySelector(".panel");
  var log = root.querySelector(".log");
  var statusEl = root.querySelector(".status");
  var form = root.querySelector("form");
  var input = root.querySelector("textarea");
  var sendBtn = root.querySelector("form button");

  function setStatus(text, working) {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (working ? " on" : "");
  }
  function scroll() {
    log.scrollTop = log.scrollHeight;
  }
  function renderBot(msg) {
    var el = document.createElement("div");
    el.className = "msg bot" + (msg.error ? " error" : "");
    el.innerHTML = msg.error ? esc(msg.text) : markdown(msg.text);
    if (msg.sources && msg.sources.length) {
      var details = document.createElement("details");
      details.innerHTML =
        "<summary>Sources (" + msg.sources.length + ")</summary><ol>" +
        msg.sources.map(function (s) {
          var url = safeUrl(s.url);
          return url
            ? '<li><a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title || s.url) + "</a></li>"
            : "";
        }).join("") + "</ol>";
      el.appendChild(details);
    }
    if (msg.navigate) {
      var go = document.createElement("button");
      go.type = "button";
      go.className = "go";
      go.textContent = "Go to " + msg.navigate.title + " →";
      go.addEventListener("click", function () { navigate(msg.navigate.path); });
      el.appendChild(go);
    }
    return el;
  }
  function render() {
    log.innerHTML = "";
    if (!state.messages.length) log.appendChild(renderBot({ text: greeting }));
    state.messages.forEach(function (msg) {
      if (msg.role === "user") {
        var el = document.createElement("div");
        el.className = "msg user";
        el.textContent = msg.text;
        log.appendChild(el);
      } else {
        log.appendChild(renderBot(msg));
      }
    });
    scroll();
  }
  function setOpen(open) {
    state.open = open;
    persist();
    panel.classList.toggle("open", open);
    fab.style.display = open ? "none" : "";
    if (open) {
      render();
      input.focus();
    }
  }
  function navigate(path) {
    state.open = true;
    persist();
    api.navigate(path);
  }

  // -- talking to the server --------------------------------------------------------

  function parseEvents(buffer, onEvent) {
    var parts = buffer.split("\n\n");
    var rest = parts.pop();
    parts.forEach(function (chunk) {
      var event = "message";
      var data = "";
      chunk.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
      });
      if (data) {
        try { onEvent(event, JSON.parse(data)); } catch (e) {}
      }
    });
    return rest;
  }

  function send(text) {
    text = String(text || "").trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    state.messages.push({ role: "user", text: text });
    persist();
    render();

    var bubble = document.createElement("div");
    bubble.className = "msg bot streaming";
    var streamed = "";
    setStatus("Working…", true);

    function finish(msg) {
      state.messages.push(msg);
      persist();
      busy = false;
      sendBtn.disabled = false;
      setStatus("");
      render();
      if (msg.navigate && msg.navigate.go_now) {
        setStatus("Taking you to " + msg.navigate.title + "…");
        setTimeout(function () { navigate(msg.navigate.path); }, 700);
      }
    }

    fetch(endpoint + "/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ session_id: state.sessionId, message: text }),
    })
      .then(function (res) {
        if (!res.ok || !res.body) {
          var reason =
            res.status === 401 ? "Please sign in to use the research assistant." :
            res.status === 409 ? "Still working on your last message." :
            "Something went wrong (" + res.status + "). Please try again.";
          throw new Error(reason);
        }
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var result = null;
        function onEvent(event, data) {
          if (event === "session") {
            state.sessionId = data.session_id;
            persist();
          } else if (event === "status") {
            setStatus(data.text, true);
          } else if (event === "text") {
            if (!bubble.parentNode) log.appendChild(bubble);
            streamed += data.delta;
            bubble.textContent = streamed;
            scroll();
          } else if (event === "done") {
            result = {
              role: "bot",
              text: data.answer || streamed.trim() || "Done.",
              sources: data.sources || [],
              navigate: data.navigate || null,
            };
            if (data.truncated) result.text += "\n\n*(This answer was cut short.)*";
          } else if (event === "error") {
            result = { role: "bot", text: data.message, error: true };
          }
        }
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              parseEvents(buffer + "\n\n", onEvent);
              finish(result || { role: "bot", text: "The connection closed early. Please try again.", error: true });
              return;
            }
            buffer = parseEvents(buffer + decoder.decode(chunk.value, { stream: true }), onEvent);
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        finish({ role: "bot", text: err.message || "Could not reach the research assistant.", error: true });
      });
  }

  // -- wiring ----------------------------------------------------------------------

  fab.addEventListener("click", function () { setOpen(true); });
  root.querySelector(".close").addEventListener("click", function () { setOpen(false); });
  root.querySelector(".new").addEventListener("click", function () {
    if (busy) return;
    if (state.sessionId) {
      fetch(endpoint + "/reset", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId }),
      }).catch(function () {});
    }
    state.sessionId = null;
    state.messages = [];
    persist();
    render();
    input.focus();
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value;
    input.value = "";
    input.style.height = "";
    send(text);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", function () {
    input.style.height = "";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });
  panel.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setOpen(false);
  });

  var api = {
    _loaded: true,
    open: function () { setOpen(true); },
    close: function () { setOpen(false); },
    ask: function (text) { setOpen(true); send(text); },
    navigate: preset.navigate || function (path) { window.location.assign(path); },
  };
  window.ResearchAgent = api;

  function mount() {
    document.body.appendChild(host);
    setOpen(!!state.open);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
