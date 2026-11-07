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
    return v || window.ROUNDUP_DEFAULT_HEADER || "";
  }

  function buildPacket() {
    var parts = [];
    var i = 1;
    selected.forEach(function (packet) {
      parts.push(i + ". " + packet);
      i += 1;
    });
    return header() + "\n\n" + parts.join("\n\n");
  }
  window.buildPacket = buildPacket;

  function updateTray() {
    $("count").textContent = String(selected.size);
  }

  function setChecked(id, on) {
    document.querySelectorAll('[data-id="' + CSS.escape(id) + '"] .pick').forEach(function (c) {
      c.checked = on;
    });
  }

  function applyPosted() {
    var posted = postedSet();
    var dropped = [];
    document.querySelectorAll("[data-id]").forEach(function (el) {
      var isPosted = posted.has(el.dataset.id);
      el.classList.toggle("posted", isPosted);
      if (isPosted && el.closest("ol.today")) {
        var t = el.querySelector("label");
        if (t) dropped.push(t.textContent.trim().replace(/, ~\d+:\d\d[ap]m$/, ""));
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
      if (!selected.size) return status("Pick at least one story first.");
      copyText(buildPacket()).then(function (ok) {
        status(ok ? "Copied " + selected.size + " for Grok. Paste it into your Grok chat."
                  : "Copy was blocked by the browser. Try again.");
      });
    });

    $("open").addEventListener("click", function () {
      if (!selected.size) return status("Pick at least one story first.");
      var text = buildPacket();
      var url = GROK_URL + encodeURIComponent(text);
      // open first (browsers block pop-ups that come after an async step), copy as a backup
      if (url.length <= GROK_LINK_LIMIT) {
        window.open(url, "_blank", "noopener");
        copyText(text);
        status("Opened Grok. The packet is also copied, in case you want to paste it into a project chat.");
      } else {
        copyText(text).then(function () {
          status("Too long for a link, so it was copied instead. Paste it into Grok.");
        });
      }
    });

    $("markposted").addEventListener("click", function () {
      if (!selected.size) return status("Pick the stories you posted first.");
      var posted = postedSet();
      selected.forEach(function (_, id) { posted.add(id); setChecked(id, false); });
      var n = selected.size;
      savePosted(posted);
      selected.clear();
      updateTray();
      applyPosted();
      status("Marked " + n + " as posted. Tick 'Show posted' to see them again.");
    });

    $("clear").addEventListener("click", function () {
      selected.forEach(function (_, id) { setChecked(id, false); });
      selected.clear();
      updateTray();
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

    applyPosted();
    updateTray();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
