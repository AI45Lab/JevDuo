/* JevDuo demo 工作台 · 应用内核（无框架、无构建、离线可开）
 *
 * 纪律：
 * - 不引入任何外部资源（CDN 只作为可选增强，主链路零外链）；
 * - 数据只用 DOM API 构造，不拼 innerHTML（避免把任务文本当代码执行）；
 * - 颜色只取 CSS 变量（见 app.css 的双主题 token）。
 */
window.TD = (function () {
  "use strict";

  var state = { overview: null, compare: null, episode: null };
  var views = {};
  var current = { view: "overview", params: {} };

  /* ------------------------------------------------------------ DOM 小工具 */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "html") node.innerHTML = value; // 仅用于本文件内的静态模板
      else if (key.indexOf("on") === 0 && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, String(value));
    });
    var list = children === null || children === undefined ? []
      : (Array.isArray(children) ? children : [children]);
    list.forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      if (typeof child === "string") { node.appendChild(document.createTextNode(child)); return; }
      if (child.nodeType) { node.appendChild(child); return; }
      node.appendChild(el("span", { text: String(child) }));
    });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  /* ------------------------------------------------------------ 数字格式 */
  function num(value, digits) {
    if (value === null || value === undefined || typeof value !== "number") return "—";
    return value.toFixed(digits === undefined ? 3 : digits);
  }
  function pct(value, digits) {
    if (value === null || value === undefined || typeof value !== "number") return "—";
    return (value * 100).toFixed(digits === undefined ? 2 : digits) + "%";
  }
  function int(value) {
    if (value === null || value === undefined || typeof value !== "number") return "—";
    return value.toLocaleString("en-US");
  }
  function hours(seconds) {
    if (typeof seconds !== "number" || !seconds) return "—";
    return (seconds / 3600).toFixed(1) + " h";
  }
  function tag(text, kind) { return el("span", { class: "tag " + (kind || ""), text: text }); }

  /* ------------------------------------------------------------ 路由 */
  function parseHash() {
    var raw = (location.hash || "#/").replace(/^#\/?/, "");
    var parts = raw.split("/").filter(Boolean);
    return { view: parts[0] || "home", params: parts.slice(1) };
  }
  function navigate(path) { location.hash = path; }

  function render() {
    current = parseHash();
    var app = document.getElementById("app");
    clear(app);
    var view = views[current.view];
    if (!view) {
      // 未知路由：回退到首页，但留一条痕迹（别静默吞掉）
      if (typeof console !== "undefined" && console.warn) {
        console.warn("[jevduo-demo] 未知视图：" + current.view + "，回退到首页");
      }
      view = views.home;
    }
    try {
      view(app, current.params);
    } catch (error) {
      app.appendChild(el("div", { class: "callout bad", text: "视图渲染失败：" + error.message }));
    }
    document.querySelectorAll("#nav a").forEach(function (link) {
      link.classList.toggle("active", link.dataset.view === current.view);
    });
  }

  /* ------------------------------------------------------------ 主题 */
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("td-theme", theme); } catch (error) { /* file:// 下可能不可用 */ }
  }
  function toggleTheme() {
    var now = document.documentElement.getAttribute("data-theme");
    setTheme(now === "dark" ? "light" : "dark");
  }

  /* ------------------------------------------------------------ 披露条 */
  function renderDisclaimer() {
    var node = document.getElementById("disclaimer");
    var summary = document.getElementById("disclaimer-summary");
    var body = document.getElementById("disclaimer-body");
    var overview = state.overview;
    if (!node || !summary || !body || !overview) return;
    var notes = (overview.disclaimer_notes) || [];
    if (!notes.length) { node.hidden = true; return; }
    clear(summary);
    clear(body);
    summary.appendChild(el("strong", { text: "读数字前先看口径（" + notes.length + " 条）" }));
    body.appendChild(el("ul", {}, notes.map(function (note) { return el("li", { text: note }); })));
    node.hidden = false;
  }

  /* ------------------------------------------------------------ 数据 */
  function readInline(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent || "null"); } catch (error) { return null; }
  }

  function episodePath(runId, taskId) {
    return "assets/data/episode/" + runId + "/" + taskId + ".json";
  }

  function loadEpisode(runId, taskId, done) {
    var url = episodePath(runId, taskId);
    if (typeof fetch !== "function") { done(null, "当前环境没有 fetch"); return; }
    fetch(url).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) {
      state.episode = data;
      done(data, null);
    }).catch(function (error) {
      done(null, error.message);
    });
  }

  /* ------------------------------------------------------------ 公共片段 */
  function card(title, children, hint) {
    var head = el("div", { class: "card-head" }, [el("h2", { text: title })]);
    if (hint) head.appendChild(el("span", { class: "hint", text: hint }));
    return el("div", { class: "card" }, [head].concat(children || []));
  }

  function kpi(label, value, note, kind) {
    return el("div", { class: "kpi " + (kind || "") }, [
      el("div", { class: "value", text: value }),
      el("div", { class: "label", text: label }),
      note ? el("div", { class: "note", text: note }) : null
    ]);
  }

  function table(headers, rows) {
    var thead = el("tr", {}, headers.map(function (head) {
      return el("th", { class: head.num ? "num" : null, text: head.label });
    }));
    var body = rows.map(function (row) {
      return el("tr", {}, row.map(function (cell) {
        if (cell && typeof cell === "object" && cell.nodeType) return el("td", { class: cell.class || null }, [cell]);
        if (cell && typeof cell === "object") {
          return el("td", { class: (cell.class || "") + (cell.num ? " num" : "") }, [cell.text]);
        }
        return el("td", { class: null, text: cell === null || cell === undefined ? "—" : String(cell) });
      }));
    });
    return el("table", { class: "data" }, [el("thead", {}, [thead]), el("tbody", {}, body)]);
  }

  return {
    state: state, views: views,
    el: el, clear: clear, num: num, pct: pct, int: int, hours: hours, tag: tag,
    card: card, kpi: kpi, table: table,
    render: render, navigate: navigate, parseHash: parseHash,
    setTheme: setTheme, toggleTheme: toggleTheme,
    renderDisclaimer: renderDisclaimer, readInline: readInline,
    loadEpisode: loadEpisode, episodePath: episodePath
  };
})();
