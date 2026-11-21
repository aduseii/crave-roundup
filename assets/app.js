// Sitcom Crave Roundup: pick stories, send them to Grok, mark them as posted,
// filter sections, hide stories that aren't sitcom news, and keep an eye on breaking news.
(function () {
  var LS_POSTED = "scr_posted";
  var LS_HEADER = "scr_header";
  var LS_HIDDEN = "scr_hidden";   // story ids hidden with "Not sitcom"
  var LS_MUTED = "scr_muted";     // words or show names: any headline containing one is hidden
  var GROK_URL = "https://grok.com/?q=";
  var GROK_LINK_LIMIT = 7000; // longer packets are copied only
  var ROOT = window.ROUNDUP_ROOT || "";

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
  function loadList(key) {
    try { var v = JSON.parse(load(key, "[]")); return Array.isArray(v) ? v : []; } catch (e) { return []; }
  }
  function postedSet() { return new Set(loadList(LS_POSTED)); }
  function savePosted(set) { store(LS_POSTED, JSON.stringify(Array.from(set).slice(-5000))); }

  var selected = new Map(); // id -> packet, in the order you picked them
  var $ = function (id) { return document.getElementById(id); };
  var all = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function status(msg) {
    var el = $("status");
    if (!el) return;
    el.textContent = msg;
    clearTimeout(status.t);
    status.t = setTimeout(function () { el.textContent = ""; }, 6000);
  }

  function header() {
    var el = $("header");
    return el ? el.value.trim() : "";
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
    document.body.classList.toggle("has-sel", selected.size > 0);
  }

  function setChecked(id, on) {
    all('[data-id="' + CSS.escape(id) + '"]').forEach(function (item) {
      item.classList.toggle("sel", on);
      var c = item.querySelector(".pick");
      if (c) c.checked = on;
    });
  }

  // posted stories stay on the page with a green "Posted" badge; tapping the badge undoes it
  function applyPosted() {
    var posted = postedSet();
    all(".item[data-id]").forEach(function (el) {
      var isPosted = posted.has(el.dataset.id);
      el.classList.toggle("posted", isPosted);
      var badge = el.querySelector(".posted-badge");
      if (isPosted && !badge) {
        badge = document.createElement("button");
        badge.type = "button";
        badge.className = "posted-badge";
        badge.textContent = "Posted";
        badge.setAttribute("aria-label", "Posted. Tap to unmark");
        var kick = el.querySelector(".kick") || el.querySelector(".t");
        kick.parentNode.insertBefore(badge, kick);
      } else if (!isPosted && badge) {
        badge.remove();
      }
    });
    updateSchedule();
    renderBreaking(lastBreaking);
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

  // ---------- pictures that fail to load turn into the name tile ----------
  document.addEventListener("error", function (e) {
    var img = e.target;
    if (!img || img.tagName !== "IMG" || !img.parentNode || !img.parentNode.classList.contains("thumb")) return;
    var box = img.parentNode;
    var span = document.createElement("span");
    span.textContent = box.dataset.ph || "";
    box.classList.add("ph");
    box.replaceChild(span, img);
  }, true);

  // ---------- top stories: progress, and the first one you haven't posted shown big ----------
  function updateSchedule() {
    var rows = all(".today .item[data-id]");
    if (!rows.length) return;
    var posted = postedSet(), done = 0, next = null;
    rows.forEach(function (r) {
      r.classList.remove("next");
      if (posted.has(r.dataset.id)) done++;
      else if (!next) next = r;
    });
    $("sched-done").textContent = String(done);
    $("sched-bar").style.width = Math.round(done / rows.length * 100) + "%";
    if (next) next.classList.add("next");
  }

  // ---------- breaking banner and red dots, kept fresh from breaking.json ----------
  var lastBreaking = window.ROUNDUP_BREAKING || null;
  function ago(iso) {
    var m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
    if (m < 60) return m + " min ago";
    var h = Math.floor(m / 60);
    return h + "h ago";
  }
  function renderBreaking(list) {
    if (!list) return;
    var posted = postedSet();
    var live = list.filter(function (s) {
      return Date.now() - Date.parse(s.seen || s.time) < 24 * 3600 * 1000 && !posted.has(s.id);
    });
    all(".bdot").forEach(function (d) {
      d.hidden = !live.length;
      d.textContent = live.length > 9 ? "9+" : String(live.length);
    });
    var box = $("bbanner");
    if (!box) return;
    box.innerHTML = "";
    if (!live.length) { box.hidden = true; return; }
    live.slice(0, 2).forEach(function (s) {
      var a = document.createElement("a");
      a.className = "bb";
      a.href = ROOT + "breaking.html#s-" + s.id;
      var tag = document.createElement("span"); tag.className = "bb-tag"; tag.textContent = s.tag || "Breaking";
      var t = document.createElement("span"); t.className = "bb-t"; t.textContent = s.title;
      var w = document.createElement("span"); w.className = "bb-when";
      w.textContent = (s.outlet ? s.outlet + ", " : "") + ago(s.seen || s.time);
      t.appendChild(w);
      a.appendChild(tag); a.appendChild(t);
      box.appendChild(a);
    });
    if (live.length > 2) {
      var more = document.createElement("a");
      more.className = "bb-more";
      more.href = ROOT + "breaking.html";
      more.textContent = "See all " + live.length + " breaking stories";
      box.appendChild(more);
    }
    box.hidden = false;
  }
  function refreshBreaking() {
    if (!window.fetch || location.protocol === "file:") return;
    fetch(ROOT + "breaking.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && d.stories) { lastBreaking = d.stories; renderBreaking(lastBreaking); } })
      .catch(function () { /* offline: keep what the page came with */ });
  }

  // ---------- "Not sitcom": hide a story, or mute a word or show for good on this device ----------
  function mutedList() { return loadList(LS_MUTED); }
  function hiddenSet() { return new Set(loadList(LS_HIDDEN)); }
  function titleOf(item) { return ((item.querySelector(".t") || {}).textContent || ""); }

  function applyHidden() {
    var hidden = hiddenSet(), muted = mutedList().map(function (m) { return m.toLowerCase(); });
    var count = 0;
    all(".item[data-id]").forEach(function (item) {
      var t = titleOf(item).toLowerCase();
      var gone = hidden.has(item.dataset.id) || muted.some(function (m) { return m && t.indexOf(m) >= 0; });
      // a story you picked or posted never vanishes
      if (item.classList.contains("sel")) gone = false;
      item.classList.toggle("gone", gone);
      if (gone) count++;
    });
    var st = $("hidden-state");
    if (st) st.textContent = count ? count + " stor" + (count === 1 ? "y is" : "ies are") + " hidden on this page."
      : "Stories you mark \"Not sitcom\" are hidden on this device.";
    var list = $("mutelist");
    if (list) {
      list.innerHTML = "";
      var words = mutedList();
      if (!words.length) {
        var none = document.createElement("span"); none.className = "muted"; none.textContent = "None yet.";
        list.appendChild(none);
      }
      words.forEach(function (w) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "act"; b.dataset.unmute = w;
        b.textContent = w + "  ×";
        b.setAttribute("aria-label", "Unmute " + w);
        list.appendChild(b);
      });
    }
    updateCounts();
  }

  function suggestions(item) {
    var t = titleOf(item), out = [];
    var re = /['‘“"]([^'’”"]{3,60})['’”"]/g, m;
    while ((m = re.exec(t))) out.push(m[1].trim());
    (item.dataset.terms || "").split("|").forEach(function (x) { if (x) out.push(x); });
    var seen = {};
    return out.filter(function (x) {
      var k = x.toLowerCase();
      if (seen[k] || t.toLowerCase().indexOf(k) < 0) return false;
      seen[k] = 1; return true;
    }).slice(0, 4);
  }

  function openMute(item) {
    var old = item.querySelector(".mute");
    if (old) { old.remove(); return; }
    var box = document.createElement("div");
    box.className = "mute panel";
    var p = document.createElement("p");
    p.textContent = "Hide this story? You can also mute a show or name so stories about it stay hidden on this device.";
    var row = document.createElement("div"); row.className = "acts";
    function btn(label, fn) {
      var b = document.createElement("button"); b.type = "button"; b.className = "act"; b.textContent = label;
      b.addEventListener("click", function (e) { e.stopPropagation(); fn(); });
      row.appendChild(b);
    }
    btn("Hide this story", function () {
      var h = hiddenSet(); h.add(item.dataset.id); store(LS_HIDDEN, JSON.stringify(Array.from(h).slice(-3000)));
      box.remove(); applyHidden(); status("Hidden. Settings can bring it back.");
    });
    suggestions(item).forEach(function (s) {
      btn("Mute “" + s + "”", function () {
        var list = mutedList();
        if (list.indexOf(s) < 0) list.push(s);
        store(LS_MUTED, JSON.stringify(list));
        box.remove(); applyHidden(); status("Muted “" + s + "” on this device.");
      });
    });
    btn("Cancel", function () { box.remove(); });
    box.appendChild(p); box.appendChild(row);
    var after = item.querySelector(".acts");
    after.parentNode.insertBefore(box, after.nextSibling);
  }

  // ---------- section chips and search ----------
  var filter = "all", query = "";
  function updateCounts() {
    all(".chip[data-filter]").forEach(function (chip) {
      var key = chip.dataset.filter;
      if (key === "all") return;
      var sec = $("sec-" + key);
      var span = chip.querySelector("span");
      if (!sec || !span) return;
      var n = all(".item:not(.gone)", sec).length;
      span.textContent = n ? String(n) : "";
    });
  }
  function applyFilter() {
    var q = query.trim().toLowerCase();
    document.body.classList.toggle("filtering", filter !== "all" || !!q);
    all("section.sec").forEach(function (sec) {
      var off = filter !== "all" && sec.dataset.sec !== filter;
      if (!off && q) {
        var any = false;
        all(".item", sec).forEach(function (it) {
          var hit = it.textContent.toLowerCase().indexOf(q) >= 0;
          it.classList.toggle("nomatch", !hit);
          if (hit) any = true;
        });
        off = !any;
      } else {
        all(".item.nomatch", sec).forEach(function (it) { it.classList.remove("nomatch"); });
      }
      sec.classList.toggle("off", off);
      if (filter !== "all" && !off) all("details.more", sec).forEach(function (d) { d.open = true; });
    });
    all(".chip[data-filter]").forEach(function (c) { c.classList.toggle("on", c.dataset.filter === filter); });
  }
  function initChips() {
    all(".chip[data-filter]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        filter = chip.dataset.filter === filter ? "all" : chip.dataset.filter;
        applyFilter();
        var bar = document.querySelector("header.top");
        var y = filter === "all" ? 0 : (document.querySelector("main").getBoundingClientRect().top + window.scrollY - (bar ? bar.offsetHeight : 0));
        window.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
        chip.scrollIntoView({ inline: "nearest", block: "nearest" });
      });
    });
    var sb = $("searchbtn"), row = $("searchrow"), input = $("search");
    if (sb && row && input) {
      sb.addEventListener("click", function () {
        var open = row.hidden;
        row.hidden = !open;
        sb.setAttribute("aria-expanded", open ? "true" : "false");
        sb.classList.toggle("on", open);
        if (open) input.focus();
        else { input.value = ""; query = ""; applyFilter(); }
      });
      input.addEventListener("input", function () { query = input.value; applyFilter(); });
    }
    // links like #sec-news from elsewhere still land on their section
    if (location.hash && location.hash.indexOf("#s-") === 0) {
      var target = document.querySelector(location.hash);
      if (target) { target.classList.add("sel"); setTimeout(function () { target.classList.remove("sel"); }, 2500); }
    }
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

    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t.closest) return;

      // caption and "Not sitcom" buttons on each story
      var tog = t.closest(".act[data-toggle]");
      if (tog) {
        var item = tog.closest(".item");
        if (tog.dataset.toggle === "mute") { openMute(item); return; }
        var panel = item.querySelector('[data-panel="' + tog.dataset.toggle + '"]');
        if (panel) { panel.hidden = !panel.hidden; tog.classList.toggle("on", !panel.hidden); }
        return;
      }
      var un = t.closest("[data-unmute]");
      if (un) {
        store(LS_MUTED, JSON.stringify(mutedList().filter(function (w) { return w !== un.dataset.unmute; })));
        applyHidden();
        return;
      }
      // copy a ready caption (template or Gemini draft)
      var cb = t.closest(".capcopy");
      if (cb) {
        copyText(cb.closest(".cap").dataset.cap).then(function (ok) {
          cb.textContent = ok ? "Copied" : "Copy blocked";
          cb.classList.toggle("done", ok);
          setTimeout(function () { cb.textContent = "Copy caption"; cb.classList.remove("done"); }, 2000);
        });
        return;
      }
      // tap a Posted badge to unmark that story
      var badge = t.closest(".posted-badge");
      if (badge) {
        var posted = postedSet();
        posted.delete(badge.closest("[data-id]").dataset.id);
        savePosted(posted);
        applyPosted();
        status("Unmarked.");
        return;
      }
      // tap anywhere else on a card to pick it
      if (t.closest("a, summary, details, input, label, button, textarea, .cap, .panel")) return;
      var card = t.closest(".item[data-id]");
      if (!card) return;
      var box = card.querySelector(".pick");
      if (box) box.click();
    });

    var sh = $("showhidden");
    if (sh) sh.addEventListener("change", function () { document.body.classList.toggle("show-hidden", sh.checked); });

    initChips();
    initWriter();
    applyHidden();
    applyPosted();
    updateTray();
    renderBreaking(lastBreaking);
    refreshBreaking();
    setInterval(refreshBreaking, 5 * 60000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refreshBreaking();
    });
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
    if (busy) { btn.dataset.label = btn.innerHTML; btn.textContent = "Writing"; }
    else if (btn.dataset.label) { btn.innerHTML = btn.dataset.label; }
    btn.disabled = busy;
    btn.classList.toggle("busy", busy);
  }

  function writeForItem(btn) {
    var item = btn.closest("[data-id]");
    var out = item.querySelector(".writer-out");
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
      var btn = e.target.closest && e.target.closest(".writecap-btn");
      if (btn) writeForItem(btn);
    });
    var go = $("maker-go");
    if (go) go.addEventListener("click", writeFromMaker);
    var mIn = $("maker-in");
    if (mIn) mIn.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); writeFromMaker(); }
    });
    var mClear = $("maker-clear");
    if (mClear) mClear.addEventListener("click", function () { mIn.value = ""; $("maker-out").innerHTML = ""; mIn.focus(); });

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

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
