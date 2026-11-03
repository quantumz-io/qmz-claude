(function () {
  var sidebar = document.getElementById("sidebar");
  var menu = document.getElementById("menu-btn");
  if (menu && sidebar) {
    menu.addEventListener("click", function () { sidebar.classList.toggle("open"); });
    document.addEventListener("click", function (e) {
      if (sidebar.classList.contains("open") && !sidebar.contains(e.target) && e.target !== menu && !menu.contains(e.target)) {
        sidebar.classList.remove("open");
      }
    });
    sidebar.querySelectorAll("a").forEach(function (a) { a.addEventListener("click", function () { sidebar.classList.remove("open"); }); });
  }

  // Copy buttons fall back to selecting the text when the clipboard is refused.
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = document.getElementById(btn.getAttribute("data-copy"));
      if (!target) return;
      var text = target.innerText;
      var done = function () { btn.classList.add("done"); var old = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.classList.remove("done"); btn.textContent = old; }, 1400); };
      var select = function () { var r = document.createRange(); r.selectNodeContents(target); var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, select);
      } else { select(); }
    });
  });

  // Highlight the section in view in the sidebar.
  var subs = Array.prototype.slice.call(document.querySelectorAll(".nav-sub a[href^='#']"));
  var targets = subs.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); }).filter(Boolean);
  if (targets.length && "IntersectionObserver" in window) {
    var current = null;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) current = en.target.id; });
      if (!current) return;
      subs.forEach(function (a) { a.classList.toggle("active", a.getAttribute("href") === "#" + current); });
    }, { rootMargin: "-64px 0px -70% 0px", threshold: 0 });
    targets.forEach(function (t) { io.observe(t); });
  }

  // Search over the build-time index of sections across all pages.
  var input = document.getElementById("search-input");
  var box = document.getElementById("search-results");
  var indexEl = document.getElementById("search-index");
  if (input && box && indexEl) {
    var index = [];
    try { index = JSON.parse(indexEl.textContent); } catch (e) { index = []; }
    var active = -1;
    function render(q) {
      q = q.trim().toLowerCase();
      box.innerHTML = ""; active = -1;
      if (!q) { box.hidden = true; return; }
      var words = q.split(/\s+/);
      var hits = index.map(function (it) {
        var hay = (it.title + " " + it.text).toLowerCase();
        var score = 0;
        words.forEach(function (w) {
          if (it.title.toLowerCase().indexOf(w) >= 0) score += 5;
          var n = hay.split(w).length - 1; score += Math.min(n, 5);
        });
        return { it: it, score: score };
      }).filter(function (h) { return h.score > 0; }).sort(function (a, b) { return b.score - a.score; }).slice(0, 10);
      if (!hits.length) { box.innerHTML = '<div class="sr-empty">No matches for “' + q.replace(/</g, "&lt;") + '”</div>'; box.hidden = false; return; }
      hits.forEach(function (h) {
        var a = document.createElement("a");
        a.href = h.it.url;
        a.innerHTML = '<div class="sr-page">' + h.it.page + '</div><div class="sr-title">' + h.it.title + '</div><div class="sr-snip">' + h.it.text.slice(0, 160) + '</div>';
        box.appendChild(a);
      });
      box.hidden = false;
    }
    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("focus", function () { if (input.value) render(input.value); });
    input.addEventListener("keydown", function (e) {
      var items = box.querySelectorAll("a");
      if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); }
      else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); }
      else if (e.key === "Enter") { if (items[active]) { items[active].click(); } return; }
      else if (e.key === "Escape") { box.hidden = true; input.blur(); return; }
      else { return; }
      e.preventDefault();
      items.forEach(function (it, i) { it.classList.toggle("active", i === active); });
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target) && e.target !== input) box.hidden = true; });
  }
})();
