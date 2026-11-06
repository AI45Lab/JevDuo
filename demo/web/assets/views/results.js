/* 结果区：标题 → 大数字 → OSW2.1 风格对比表 → 战绩热力
 *
 * 版式原则：一屏一个论点；大字、少小字说明；口径差异写在对比表同一行里，
 * 不另开一块"注意事项"。
 */
(function () {
  "use strict";
  var TD = window.TD;

  function countUp(node, target, opts) {
    opts = opts || {};
    var digits = opts.digits === undefined ? 2 : opts.digits;
    var suffix = opts.suffix || "";
    var prefix = opts.prefix || "";
    if (typeof target !== "number" || !isFinite(target)) {
      node.textContent = prefix + (target === null || target === undefined ? "—" : target) + suffix;
      return;
    }
    var start = performance.now();
    var dur = 900;
    function frame(now) {
      var p = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      node.textContent = prefix + (target * eased).toFixed(digits) + suffix;
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function metric(label, foot, kind, animate) {
    var valueNode = TD.el("div", { class: "value" });
    var box = TD.el("div", { class: "metric " + (kind || "") }, [
      valueNode,
      TD.el("div", { class: "label", text: label }),
      foot ? TD.el("div", { class: "foot", text: foot }) : null
    ]);
    countUp(valueNode, animate.value, animate);
    return box;
  }

  function cellKind(row) {
    if (row.status !== "scored") return row.stop === "exception" ? "s-exc" : "s-wall";
    if (row.score >= 1) return "s-full";
    if (row.score > 0) return "s-part";
    return "s-zero";
  }

  /* ---------------------------------------------------------- 战绩热力 */
  function heat(overview) {
    var subset = overview.subset_tasks || [];
    var byDomain = {};
    (overview.rows || []).forEach(function (row) {
      var domain = row.domain || "other";
      (byDomain[domain] = byDomain[domain] || []).push(row);
    });
    // 两批都没跑出结果的题：单独一行虚线格（不折算 0，只是没结果）
    var missing = overview.ours.missing || [];
    if (missing.length) {
      byDomain["未跑出结果"] = missing.map(function (task) {
        return { task: task, score: null, status: "missing", stop: "", steps: 0, domain: "" };
      });
    }
    var domains = Object.keys(byDomain).sort(function (a, b) {
      return byDomain[b].length - byDomain[a].length;
    });
    var rows = domains.map(function (domain) {
      var cells = byDomain[domain].sort(function (a, b) { return a.task < b.task ? -1 : 1; }).map(function (row, index) {
        var kind = row.status === "missing" ? "s-miss" : cellKind(row);
        if (subset.indexOf(row.task) >= 0) kind += " in-subset";
        return TD.el("div", {
          class: "heat-cell " + kind,
          style: "animation-delay:" + (index * 12) + "ms",
          title: row.task + " · " + (row.status === "missing" ? "两批都没跑出结果"
                 : (row.score === null ? "未出分（" + (row.stop || "-") + "）" : row.score.toFixed(4))) +
                 " · 步数 " + (row.steps || 0),
          text: row.task
        });
      });
      var scored = byDomain[domain].filter(function (row) { return row.score !== null; });
      var average = scored.length
        ? scored.reduce(function (sum, row) { return sum + row.score; }, 0) / scored.length
        : null;
      return TD.el("div", { class: "heat-row" }, [
        TD.el("div", { class: "domain", text: domain }),
        TD.el("div", { class: "heat-strip" }, cells),
        TD.el("div", { class: "domain-avg", text: average === null ? "—" : average.toFixed(3) })
      ]);
    });
    var legend = TD.el("div", { class: "heat-legend" }, [
      TD.el("span", {}, [TD.el("i", { style: "background:var(--good)" }), document.createTextNode("满分")]),
      TD.el("span", {}, [TD.el("i", { style: "background:color-mix(in srgb, var(--good) 42%, var(--surface-1))" }), document.createTextNode("部分分")]),
      TD.el("span", {}, [TD.el("i", { style: "background:var(--surface-3)" }), document.createTextNode("零分")]),
      TD.el("span", {}, [TD.el("i", { style: "background:color-mix(in srgb, var(--warn) 45%, var(--surface-1))" }), document.createTextNode("撞墙钟")]),
      TD.el("span", {}, [TD.el("i", { style: "background:color-mix(in srgb, var(--bad) 55%, var(--surface-1))" }), document.createTextNode("异常未出分")]),
      TD.el("span", {}, [TD.el("i", { style: "background:transparent;border:1px solid var(--accent)" }), document.createTextNode("进入官方对照子集")])
    ]);
    return TD.el("div", {}, [TD.el("div", { class: "heat" }, rows), legend]);
  }

  /* ------------------------------------------------ OSW2.1 风格对比表 */
  function leaderboard(overview) {
    var ours = overview.ours;
    var harness = overview.official.harness || [];
    var e2e = overview.official.e2e_background || [];
    var sai = null;
    var darwinx = null;
    harness.forEach(function (row) {
      if (!sai && /Sai/i.test(row.model)) sai = row;
      if (!darwinx && /Darwinx/i.test(row.model)) darwinx = row;
    });
    var opus5 = e2e.filter(function (row) { return /Opus 5/.test(row.model || ""); })[0];

    function row(name, model, release, steps, binary, partial, cost, ours_) {
      return [
        { text: name, class: ours_ ? "good" : "" },
        { text: model, class: "dim" },
        { text: release, class: "dim" },
        { text: String(steps), num: true },
        { text: binary === null ? "—" : binary.toFixed(2), num: true },
        { text: partial === null ? "—" : partial.toFixed(2), num: true },
        { text: cost === null ? "未发布" : "$" + cost.toFixed(cost < 1 ? 3 : 2), num: true }
      ];
    }

    var rows = [
      row("JevDuo", "MiniMax-M3.1-Flash + JEV", "v2.1", 500,
        (ours.binary_rate_attempted || 0) * 100, (ours.partial_over_dataset || 0) * 100,
        ours.usd_per_task, true)
    ];
    if (sai) {
      rows.push(row("Sai", "Claude Opus 5", sai.release || "v2.1", sai.step_budget,
        sai.binary_accuracy, sai.partial_score, sai.cost_per_task_usd));
    }
    if (darwinx) {
      rows.push(row("Darwinx", "Claude Opus 4.8", darwinx.release || "v2.1", darwinx.step_budget,
        darwinx.binary_accuracy, darwinx.partial_score, darwinx.cost_per_task_usd));
    }
    if (opus5) {
      rows.push(row("Claude Opus 5", "官方 runner 直跑", opus5.release || "v2.1", opus5.step_budget,
        opus5.binary_accuracy, opus5.partial_score, opus5.cost_per_task_usd));
    }

    return TD.el("div", { class: "panel" }, [
      TD.el("div", { class: "panel-head" }, [
        TD.el("h2", { text: "结果对比" })
      ]),
      TD.table(
        [{ label: "Agent" }, { label: "模型底座" }, { label: "Release" }, { label: "步数", num: true },
         { label: "Binary %", num: true }, { label: "Partial %", num: true }, { label: "$/题", num: true }],
        rows
      )
    ]);
  }

  /* 结果区（首页的第三段）：OSWorld v2.1 测评结果 */
  TD.sections = TD.sections || {};
  TD.sections.results = function (container) {
    var overview = TD.state.overview;
    if (!overview) {
      container.appendChild(TD.el("div", { class: "callout bad", text: "没有内联的总览数据——请用构建脚本生成站点。" }));
      return;
    }
    var ours = overview.ours;
    var price = ours.price || {};
    var walls = (overview.rows || []).map(function (row) { return row.wall_seconds; })
      .filter(function (value) { return typeof value === "number" && value > 0; });
    var averageTaskHours = walls.length
      ? walls.reduce(function (sum, value) { return sum + value; }, 0) / walls.length / 3600
      : 0;

    container.appendChild(TD.el("section", { class: "hero" }, [
      TD.el("div", { class: "eyebrow", text: "OSWorld 2.1 · stage + memory · 原生官方评测器" }),
      TD.el("h1", { text: "OSWorld v2.1 测评结果" }),
      TD.el("p", { class: "lede", html:
        "慢模型执行规划与纠错，快模型快速选择动作。" })
    ]));

    container.appendChild(TD.el("div", { class: "metrics" }, [
      metric("Partial Score（108 题分母）", "未出分计 0 · 与官方榜同口径", "accent",
        { value: ours.partial_over_dataset * 100, digits: 2, suffix: "%" }),
      metric("Binary Accuracy", ours.binary + " / " + ours.attempted + " 题 · 逐题分恰好 1.00", ours.binary ? "good" : "bad",
        { value: ours.binary_rate_attempted * 100, digits: 2, suffix: "%" }),
      metric("每题成本", (price.model || "") + " · " + (price.snapshot || ""), "warn",
        { value: ours.usd_per_task || 0, digits: 3, prefix: "$" }),
      metric("每题平均执行时间", ours.attempted + " 题的均值", "",
        { value: averageTaskHours, digits: 2, suffix: " h" })
    ]));

    container.appendChild(leaderboard(overview));

    container.appendChild(TD.el("div", { class: "panel", style: "margin-top:22px" }, [
      TD.el("div", { class: "panel-head" }, [
        TD.el("h2", { text: "战绩（按域分行）" }),
        TD.el("span", { class: "hint", text: ours.attempted + " 题 · 悬停看详情" })
      ]),
      heat(overview)
    ]));

    container.appendChild(TD.el("div", { class: "footer-note", text:
      "数据源：stage+memory 批次（execution_mode=stage · 500 步上限 / 4h 墙钟 · " +
      "慢模型 MiniMax-M3.1-Flash reasoning_effort=max · 快模型 JEV · 原生官方评测器）；" +
      "数字由 tools/batch_summary.py 统一计算；口径差异见对比表备注列。" }));
  };
})();
