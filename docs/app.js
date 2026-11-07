// Sitcom Crave Roundup: pick stories, send them to Grok, mark them as posted.
(function () {
  var LS_POSTED = "scr_posted";
  var LS_HEADER = "scr_header";
  var GROK_URL = "https://grok.com/?q=";
  var GROK_LINK_LIMIT = 7000; // longer packets are copied only

  function load(key, fallback) {
    try {
      var v = localStorage.getItem(key);
      return v === null ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }
  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
  }
  function postedSet() {
    try { return new Set(JSON.parse(load(LS_POSTED, "[]"))); } catch (e) { return new Set(); }
  }
  function savePosted(set) {
    var arr = Array.from(set);
    store(LS_POSTED, JSON.stringify(arr.slice(-5000)));
  }

  var selected = new Map(); // id -> packet, in the order you picked them
  var $ = function (id) { return document.getElementById(id); };

  function status(msg) {
    var el = $("status");
    if (!el) return;
    el.textContent = msg;
    clearTimeout(status.t);
    status.t = setTimeout(function () { el.textContent = ""; }, 6000);
  }

  function header() {
    var el = $("header");
    var v = el ? el.value.trim() : "";
    return v;
  }

  // only the stories themselves: headline and links, separated by a blank line
  function buildPacket() {
    var parts = [];
    selected.forEach(function (packet) { parts.push(packet); });
    var body = parts.join("\n\n");
    var h = header();
    return h ? h + "\n\n" + body : body;
  }
  window.buildPacket = buildPacket;

  function updateTray() {
    $("count").textContent = String(selected.size);
    // the action bar slides up only while something is picked
    document.body.classList.toggle("has-sel", selected.size > 0);
  }

  function setChecked(id, on) {
    document.querySelectorAll('[data-id="' + CSS.escape(id) + '"]').forEach(function (item) {
      item.classList.toggle("sel", on);
      var c = item.querySelector(".pick");
      if (c) c.checked = on;
    });
  }

  // posted stories stay on the page with a green "Posted" badge; tapping the badge undoes it
  function applyPosted() {
    var posted = postedSet();
    document.querySelectorAll("[data-id]").forEach(function (el) {
      var isPosted = posted.has(el.dataset.id);
      el.classList.toggle("posted", isPosted);
      var badge = el.querySelector(".posted-badge");
      if (isPosted && !badge) {
        badge = document.createElement("button");
        badge.type = "button";
        badge.className = "posted-badge";
        badge.textContent = "Posted";
        badge.title = "Tap to unmark";
        badge.setAttribute("aria-label", "Posted. Tap to unmark");
        var meta = el.querySelector(".meta");
        if (meta) meta.insertBefore(badge, meta.firstChild);
        else el.appendChild(badge);
      } else if (!isPosted && badge) {
        badge.remove();
      }
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return fallbackCopy(text); });
    }
    return Promise.resolve(fallbackCopy(text));
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  document.addEventListener("change", function (e) {
    var box = e.target;
    if (!box.classList || !box.classList.contains("pick")) return;
    var item = box.closest("[data-id]");
    if (!item) return;
    var id = item.dataset.id;
    if (box.checked) selected.set(id, item.dataset.packet);
    else selected.delete(id);
    setChecked(id, box.checked);
    updateTray();
  });

  function init() {
    var h = $("header");
    if (h) {
      h.value = load(LS_HEADER, window.ROUNDUP_DEFAULT_HEADER || "");
      h.addEventListener("input", function () { store(LS_HEADER, h.value); });
    }

    $("copy").addEventListener("click", function () {
      if (!selected.size) return status("Pick a story first.");
      copyText(buildPacket()).then(function (ok) {
        status(ok ? "Copied " + selected.size + ". Paste it into Grok." : "Copy was blocked. Try again.");
      });
    });

    $("open").addEventListener("click", function () {
      if (!selected.size) return status("Pick a story first.");
      var text = buildPacket();
      var url = GROK_URL + encodeURIComponent(text);
      // start the copy and the new tab in the same tap: Safari blocks pop-ups after an async step
      var copying = copyText(text);
      if (url.length <= GROK_LINK_LIMIT) {
        window.open(url, "_blank", "noopener");
        status("Opened Grok. Also copied, to paste into a project chat.");
      } else {
        copying.then(function () { status("Too long for a link, so it was copied. Paste it into Grok."); });
      }
    });

    var shareBtn = $("share");
    if (shareBtn && navigator.share) {
      shareBtn.hidden = false;
      shareBtn.addEventListener("click", function () {
        if (!selected.size) return status("Pick a story first.");
        var text = buildPacket();
        copyText(text);
        navigator.share({ text: text }).then(function () {
          status("Shared.");
        }, function (err) {
          if (!err || err.name !== "AbortError") status("Sharing failed, but it's copied. Paste it into Grok.");
        });
      });
    }

    $("markposted").addEventListener("click", function () {
      if (!selected.size) return status("Pick the stories you posted first.");
      var posted = postedSet();
      selected.forEach(function (_, id) { posted.add(id); setChecked(id, false); });
      var n = selected.size;
      savePosted(posted);
      selected.clear();
      updateTray();
      applyPosted();
      status("Marked " + n + " as posted. Tap a Posted badge to undo.");
    });

    $("clear").addEventListener("click", function () {
      selected.forEach(function (_, id) { setChecked(id, false); });
      selected.clear();
      updateTray();
    });

    // tap anywhere on a card to pick it (links, "more" and the checkbox itself work as usual)
    document.addEventListener("click", function (e) {
      if (e.target.closest("a, summary, details, input, label, button, textarea, .cap")) return;
      var item = e.target.closest(".item[data-id]");
      if (!item) return;
      var box = item.querySelector(".pick");
      if (box) box.click();
    });

    // copy a ready caption (template or Gemini draft)
    document.addEventListener("click", function (e) {
      var btn = e.target.closest && e.target.closest(".capcopy");
      if (!btn) return;
      var box = btn.closest(".cap");
      copyText(box.dataset.cap).then(function (ok) {
        btn.textContent = ok ? "Copied" : "Copy blocked";
        btn.classList.toggle("done", ok);
        setTimeout(function () { btn.textContent = "Copy caption"; btn.classList.remove("done"); }, 2000);
      });
    });

    // tap a Posted badge to unmark that story
    document.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".posted-badge");
      if (!b) return;
      var item = b.closest("[data-id]");
      var posted = postedSet();
      posted.delete(item.dataset.id);
      savePosted(posted);
      applyPosted();
      status("Unmarked.");
    });

    // older shortcut: Shift+click a posted story's checkbox
    document.addEventListener("click", function (e) {
      if (!e.shiftKey || !e.target.classList || !e.target.classList.contains("pick")) return;
      var item = e.target.closest("[data-id]");
      if (!item || !item.classList.contains("posted")) return;
      var posted = postedSet();
      posted.delete(item.dataset.id);
      savePosted(posted);
      applyPosted();
      status("Unmarked as posted.");
    });

    initSections();
    initWriter();
    applyPosted();
    updateTray();
  }


  // ---------- caption writer: Gemini, called straight from this device ----------
  var LS_GEMKEY = "scr_gemini_key";
  var GEMINI_API = "https://generativelanguage.googleapis.com/v1beta";
  var busyModels = {};

  function gemKey() { return ((load(LS_GEMKEY, "") || "").trim().split(/\s+/)[0]) || ""; }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function tidyCaption(t) {
    t = (t || "").trim().replace(/^`+|`+$/g, "").trim();
    if (t.length > 1 && t[0] === '"' && t[t.length - 1] === '"') t = t.slice(1, -1).trim();
    t = t.replace(/https?:\/\/\S+/g, "");                       // captions carry no links
    t = t.replace(/ — /g, ", ").replace(/—/g, ", ").replace(/ – /g, ", ");
    t = t.replace(/[ \t]+/g, " ").replace(/[ \t]+\n/g, "\n");
    t = t.replace(/\s*\n\s*\n\s*(\(via [^)]*\))\s*$/, " $1");
    return t.trim().slice(0, 700);
  }

  function version(n) { var m = n.match(/(\d+(?:\.\d+)?)/); return m ? parseFloat(m[1]) : 0; }

  function pickModels(key) {
    try {
      var cached = JSON.parse(sessionStorage.getItem("scr_gem_models") || "null");
      if (cached && cached.length) return Promise.resolve(cached);
    } catch (e) { /* ignore */ }
    return fetch(GEMINI_API + "/models?pageSize=200", { headers: { "x-goog-api-key": key } })
      .then(function (r) {
        if (r.ok) return r.json();
        return r.text().then(function (body) {
          if (/API_KEY_INVALID|API key not valid|PERMISSION_DENIED/i.test(body) || r.status === 401 || r.status === 403) throw new Error("badkey");
          return { models: [] };
        });
      })
      .then(function (d) {
        var skip = /image|tts|live|audio|exp|embedding|8b|thinking|robotics|computer/;
        var names = (d.models || []).filter(function (m) {
          return (m.supportedGenerationMethods || []).indexOf("generateContent") >= 0;
        }).map(function (m) { return m.name.split("/").pop(); }).filter(function (n) { return /flash/.test(n) && !skip.test(n); });
        var byVer = function (a, b) { return version(b) - version(a); };
        var full = names.filter(function (n) { return !/lite|latest|preview/.test(n); }).sort(byVer);
        var prev = names.filter(function (n) { return !/lite/.test(n) && /preview/.test(n); }).sort(byVer);
        var lite = names.filter(function (n) { return /lite/.test(n) && !/latest|preview/.test(n); }).sort(byVer);
        var order = [];
        if (names.indexOf("gemini-flash-latest") >= 0) order.push("gemini-flash-latest");
        order = order.concat(full.slice(0, 2), prev.slice(0, 1));
        if (names.indexOf("gemini-flash-lite-latest") >= 0) order.push("gemini-flash-lite-latest");
        order = order.concat(lite.slice(0, 1));
        if (!order.length) order = ["gemini-flash-latest"];
        order = order.filter(function (n, i) { return order.indexOf(n) === i; });
        try { sessionStorage.setItem("scr_gem_models", JSON.stringify(order)); } catch (e) { /* ignore */ }
        return order;
      });
  }

  // ask Gemini; with a link it reads the page itself (URL context tool)
  function generate(key, models, prompt, useUrl) {
    var i = 0;
    function next() {
      while (i < models.length && busyModels[models[i]]) i++;
      if (i >= models.length) return Promise.reject(new Error("busy"));
      return attempt(models[i++], 0, useUrl);
    }
    function attempt(model, tries, withUrl) {
      var body = {
        systemInstruction: { parts: [{ text: window.ROUNDUP_CAPTION_RULES || "" }] },
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.3, maxOutputTokens: 2048 }
      };
      if (withUrl) body.tools = [{ url_context: {} }];
      return fetch(GEMINI_API + "/models/" + model + ":generateContent", {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": key },
        body: JSON.stringify(body)
      }).then(function (r) {
        if (r.ok) {
          return r.json().then(function (d) {
            var c = (d.candidates || [])[0] || {};
            var text = tidyCaption(((c.content || {}).parts || []).filter(function (p) { return !p.thought; })
              .map(function (p) { return p.text || ""; }).join(""));
            var meta = c.urlContextMetadata || c.url_context_metadata || {};
            var list = meta.urlMetadata || meta.url_metadata || [];
            var read = list.some(function (u) { return /SUCCESS/.test(u.urlRetrievalStatus || u.url_retrieval_status || ""); });
            if (!text) return next();
            return { text: text, model: model, read: read };
          });
        }
        return r.text().then(function (errBody) {
          if (/API_KEY_INVALID|API key not valid/i.test(errBody) || r.status === 401 || r.status === 403) throw new Error("badkey");
          if (r.status === 400 && withUrl) return attempt(model, tries, false);   // no link reading on this model
          if ((r.status === 429 || r.status >= 500) && tries === 0) return wait(2500).then(function () { return attempt(model, 1, withUrl); });
          busyModels[model] = true;
          return next();
        });
      });
    }
    return next();
  }

  function writeCaption(prompt, useUrl) {
    var key = gemKey();
    if (!key) return Promise.reject(new Error("nokey"));
    return pickModels(key).then(function (models) { return generate(key, models, prompt, useUrl); });
  }

  function showCaption(out, text, note) {
    out.innerHTML = "";
    var box = document.createElement("div");
    box.className = "cap";
    box.dataset.cap = text;
    var h = document.createElement("div"); h.className = "cap-h"; h.textContent = note;
    var p = document.createElement("p"); p.className = "cap-t"; p.textContent = text;
    var b = document.createElement("button"); b.type = "button"; b.className = "capcopy"; b.textContent = "Copy caption";
    box.appendChild(h); box.appendChild(p); box.appendChild(b);
    out.appendChild(box);
  }

  function showNote(out, msg, settingsLink) {
    out.innerHTML = "";
    var n = document.createElement("div"); n.className = "note"; n.textContent = msg + " ";
    if (settingsLink) {
      var a = document.createElement("a"); a.href = "#settings"; a.textContent = "Open Settings";
      a.addEventListener("click", function () { var st = $("settings"); if (st) st.open = true; });
      n.appendChild(a);
    }
    out.appendChild(n);
  }

  function explain(out, err) {
    var m = err && err.message;
    if (m === "nokey") return showNote(out, "Add your Gemini key in Settings first.", true);
    if (m === "badkey") return showNote(out, "Gemini didn't accept the saved key. Check it in Settings.", true);
    if (m === "busy") return showNote(out, "Gemini is busy or today's free limit is used up. Try again in a few minutes.");
    return showNote(out, "Couldn't reach Gemini. Check your connection and try again.");
  }

  function setBusy(btn, busy) {
    if (busy) { btn.dataset.label = btn.textContent; btn.textContent = "Writing"; }
    else { btn.textContent = btn.dataset.label || "Write caption"; }
    btn.disabled = busy;
    btn.classList.toggle("busy", busy);
  }

  function writeForItem(btn) {
    var item = btn.closest("[data-id]");
    var out = btn.parentNode.querySelector(".writer-out");
    var lines = (item.dataset.packet || "").split("\n");
    var headline = (item.querySelector(".t") || {}).textContent || lines[0];
    var link = lines.filter(function (l) { return /^https?:\/\//.test(l); })[0] || "";
    var outletEl = item.querySelector('.meta a[target="_blank"]');
    var outlet = outletEl ? outletEl.textContent : "";
    var sumEl = item.querySelector(".sum");
    var prompt = [
      "Write the caption for this story.",
      "Headline: " + headline,
      outlet ? "Outlet: " + outlet : "",
      sumEl ? "Summary: " + sumEl.textContent : "",
      link ? "Article: " + link : "",
      link ? "Read the article at that link and use only facts stated in it. If you can't open it, use only the headline and summary." : ""
    ].filter(Boolean).join("\n");
    setBusy(btn, true);
    writeCaption(prompt, !!link).then(function (res) {
      showCaption(out, res.text, res.read
        ? "Written just now by Gemini from the article. Check it before posting."
        : "Written just now by Gemini from the headline only (the article couldn't be read). Check it carefully.");
    }, function (err) { explain(out, err); }).then(function () { setBusy(btn, false); });
  }

  function writeFromMaker() {
    var input = ($("maker-in").value || "").trim();
    var out = $("maker-out"), btn = $("maker-go");
    if (!input) return showNote(out, "Paste a link or some text first.");
    var isLink = /^https?:\/\/\S+$/.test(input);
    var prompt = isLink
      ? "Write the caption for the story at this link: " + input + "\nRead it and use only facts stated in it."
      : "Write the caption for this story. Use only facts stated in it.\n\n" + input;
    setBusy(btn, true);
    writeCaption(prompt, isLink).then(function (res) {
      var note = !isLink ? "Written just now by Gemini from the text you pasted. Check it before posting."
        : res.read ? "Written just now by Gemini from the article. Check it before posting."
        : "Gemini couldn't open that link, so this may be thin. Paste the story text instead for a better caption.";
      showCaption(out, res.text, note);
    }, function (err) { explain(out, err); }).then(function () { setBusy(btn, false); });
  }

  function initWriter() {
    document.addEventListener("click", function (e) {
      var btn = e.target.closest && e.target.closest(".writer .writecap");
      if (btn) writeForItem(btn);
    });
    var go = $("maker-go");
    if (go) go.addEventListener("click", writeFromMaker);

    var keyIn = $("gemkey"), save = $("gemkey-save"), clear = $("gemkey-clear"), state = $("gemkey-state");
    function showState() {
      if (!state) return;
      state.textContent = gemKey() ? "A key is saved on this device." : "No key saved yet. Get one free at aistudio.google.com.";
    }
    if (save) save.addEventListener("click", function () {
      var v = (keyIn.value || "").trim().split(/\s+/)[0] || "";
      if (!v) { state.textContent = "Paste the key first."; return; }
      store(LS_GEMKEY, v);
      keyIn.value = "";
      try { sessionStorage.removeItem("scr_gem_models"); } catch (e) { /* ignore */ }
      busyModels = {};
      showState();
    });
    if (clear) clear.addEventListener("click", function () {
      try { localStorage.removeItem(LS_GEMKEY); } catch (e) { /* ignore */ }
      showState();
    });
    showState();
  }

  // section indicator: the left rail (wide screens) or the button in the top bar (phones)
  // shows which section is on screen; tap or click to jump
  function initSections() {
    var heads = Array.prototype.slice.call(document.querySelectorAll("main h2[id]"));
    var links = document.querySelectorAll("[data-sec]");
    var btn = $("secbtn"), menu = $("secmenu"), cur = $("cursec");
    if (!heads.length || !links.length) return;
    var labels = {};
    links.forEach(function (a) { labels[a.dataset.sec] = a.dataset.label; });
    heads = heads.filter(function (h) { return labels[h.id]; });
    var lastId = null;

    function update() {
      var offset = 90; // a heading counts as current once it reaches the top of the screen
      var active = heads[0];
      for (var i = 0; i < heads.length; i++) {
        if (heads[i].getBoundingClientRect().top - offset <= 0) active = heads[i];
        else break;
      }
      var doc = document.documentElement;
      if (window.innerHeight + window.scrollY >= doc.scrollHeight - 4) active = heads[heads.length - 1];
      if (!active || active.id === lastId) return;
      lastId = active.id;
      links.forEach(function (a) { a.classList.toggle("on", a.dataset.sec === active.id); });
      if (cur) cur.textContent = labels[active.id];
    }

    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () { ticking = false; update(); });
    }, { passive: true });
    window.addEventListener("resize", update);

    function setMenu(open) {
      if (!btn || !menu) return;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (btn && menu) {
      btn.addEventListener("click", function () { setMenu(menu.hidden); });
      menu.addEventListener("click", function (e) { if (e.target.closest("a")) setMenu(false); });
      document.addEventListener("click", function (e) {
        if (!menu.hidden && !e.target.closest(".jb")) setMenu(false);
      });
      document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
    }
    update();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
