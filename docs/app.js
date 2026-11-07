// Sitcom Crave Roundup: pick stories, send them to Grok, mark them as posted.
(function () {
  var LS_POSTED = "scr_posted";
  var LS_HEADER = "scr_header";
  var LS_SHOWPOSTED = "scr_showposted";
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

  function applyPosted() {
    var posted = postedSet();
    var dropped = [];
    document.querySelectorAll("[data-id]").forEach(function (el) {
      var isPosted = posted.has(el.dataset.id);
      el.classList.toggle("posted", isPosted);
      if (isPosted && el.closest(".today")) {
        var t = el.querySelector(".t");
        if (t) dropped.push(t.textContent.trim());
      }
    });
    var line = $("droppedline");
    if (line) line.textContent = dropped.length ? "Dropped as already posted: " + dropped.join("; ") : "";
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

    var sp = $("showposted");
    if (sp) {
      sp.checked = load(LS_SHOWPOSTED, "0") === "1";
      document.body.classList.toggle("hideposted", !sp.checked);
      sp.addEventListener("change", function () {
        store(LS_SHOWPOSTED, sp.checked ? "1" : "0");
        document.body.classList.toggle("hideposted", !sp.checked);
      });
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
      status("Marked " + n + " as posted.");
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

    // unmark one posted story: it shows when "Show posted" is ticked; Shift+click its checkbox
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
    applyPosted();
    updateTray();
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
