/* 首页 = 简介 → 机制（滚动变化）→ OSWorld v2.1 结果
 *
 * 排版参考 sai.work 的骨架：深色 hero → 粘性滚动区（滚动时内容切换）→
 * 分区（eyebrow + 引导句 + 编号卡）→ 数据区。
 */
(function () {
  "use strict";
  var TD = window.TD;

  /* ------------------------------------------------------------ 1. 简介 */
  function intro() {
    return TD.el("section", { class: "intro" }, [
      TD.el("p", { class: "eyebrow", text: "JEVDUO · 快慢双模 CUA HARNESS" }),
      TD.el("h1", { class: "intro-title", text: "一个大脑，一个小脑，一起操作电脑" }),
      TD.el("p", { class: "intro-lede", html:
        "慢模型负责拆解、规划与纠错；快模型负责在候选空间里挑选动作。" +
        "两者用同一份状态接力，跑在真实的 OSWorld v2.1 任务上。" })
    ]);
  }

  /* ------------------------------------------- 2. 机制（流程图 + 滚动切换） */
  function mechanism(episode) {
    var steps = buildMechanismSteps(episode);
    var facts = TD.facts.build(episode);
    var active = 0;

    var diagram = TD.flow.concept(facts.concept, function (index) { setStep(index, true); });

    var sticky = TD.el("div", { class: "mech-sticky" });
    var column = TD.el("div", { class: "mech-steps" });

    var cards = steps.map(function (step, index) {
      var card = TD.el("div", { class: "mech-card" + (index === 0 ? " active" : "") }, [
        TD.el("div", { class: "mech-card-head" }, [
          TD.el("span", { class: "mech-step-no", text: String(index + 1).padStart(2, "0") }),
          TD.el("b", { text: step.title }),
          TD.el("span", { class: "mech-role", text: step.role })
        ]),
        step.diagram ? step.diagram : null,
        TD.el("pre", { class: "text", text: step.body }),
        step.meta ? TD.el("div", { class: "mech-card-meta", text: step.meta }) : null,
        step.extra ? step.extra : null
      ]);
      sticky.appendChild(card);
      return card;
    });

    var chainItems = steps.map(function (step, index) {
      var item = TD.el("div", {
        class: "mech-step" + (index === 0 ? " active" : ""),
        onclick: function () { setStep(index, false); }
      }, [
        TD.el("div", { class: "mech-node" }, [
          TD.el("span", { class: "mech-dot", text: String(index + 1) }),
          TD.el("div", {}, [
            TD.el("h3", { text: step.title }),
            TD.el("p", { text: step.desc })
          ])
        ])
      ]);
      column.appendChild(item);
      return item;
    });

    function setStep(index, scrollIntoView) {
      active = index;
      cards.forEach(function (card, i) { card.classList.toggle("active", i === index); });
      chainItems.forEach(function (item, i) { item.classList.toggle("active", i === index); });
      diagram.setActive(index);
      if (scrollIntoView && chainItems[index]) {
        chainItems[index].scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }

    /* 滚到哪一步，粘性卡片就切到哪一步 */
    function syncActive() {
      var blocks = column.querySelectorAll(".mech-step");
      var best = 0;
      var bestDistance = Infinity;
      blocks.forEach(function (block, index) {
        var rect = block.getBoundingClientRect();
        var distance = Math.abs(rect.top - 140);
        if (distance < bestDistance) { bestDistance = distance; best = index; }
      });
      if (best !== active) setStep(best, false);
    }
    window.addEventListener("scroll", syncActive, { passive: true });
    window.addEventListener("resize", syncActive);
    setTimeout(syncActive, 60);

    /* ← / → 切换步骤。监听挂在 document 上，所以必须可清理：
       否则每次重渲染都会叠加一个监听器，还会劫持别的页面（轨迹浏览）的方向键。 */
    if (TD._mechCleanup) { TD._mechCleanup(); TD._mechCleanup = null; }
    function onKey(event) {
      if (event.key === "ArrowLeft") { setStep(Math.max(0, active - 1), true); }
      else if (event.key === "ArrowRight") { setStep(Math.min(steps.length - 1, active + 1), true); }
      else return;
      event.preventDefault();
    }
    document.addEventListener("keydown", onKey);
    TD._mechCleanup = function () { document.removeEventListener("keydown", onKey); };

    var layout = TD.el("div", { class: "mech-layout" });
    layout.appendChild(sticky);
    layout.appendChild(column);

    var section = TD.el("section", { class: "mech" }, [
      TD.el("p", { class: "eyebrow", text: "机制 · 双模怎么协同" }),
      TD.el("h2", { class: "section-title", text: "一次决策，经过两个模型" }),
      TD.el("p", { class: "section-lede", html:
        "下面是OSWorld v2.1真实的一条链路：慢模型写规划，快模型排候选，" +
        "动作执行后由核验判定要不要重规划。<b>点流程图节点</b>或用 <b>← / →</b> 切换环节。" }),
      TD.el("div", { class: "flow-wrap" }, [
        diagram.node,
        TD.el("div", { class: "flow-legend" }, [
          TD.el("span", {}, [TD.el("i", { style: "background:var(--series-4)" }), document.createTextNode("慢模型")]),
          TD.el("span", {}, [TD.el("i", { style: "background:var(--series-2)" }), document.createTextNode("快模型")]),
          TD.el("span", {}, [TD.el("i", { style: "background:var(--series-1)" }), document.createTextNode("动作/候选")]),
          TD.el("span", {}, [TD.el("i", { style: "background:var(--good)" }), document.createTextNode("核验通过")]),
          TD.el("span", {}, [TD.el("i", { style: "background:var(--warn)" }), document.createTextNode("重规划回路")])
        ])
      ]),
      layout
    ]);
    return section;
  }

  function buildMechanismSteps(episode) {
    var facts = TD.facts.build(episode);
    var steps = [];

    steps.push({
      title: "慢模型拆解与规划",
      role: "slow_model",
      desc: "慢模型把任务拆成子任务，给出每个子任务的目标与预期结果；不达标就重规划，拆出新的子任务。",
      diagram: TD.flow.milestones(facts.subtasks, {
        total: facts.subtasks.length, plans: facts.plans, limit: 6
      }),
      meta: facts.planner
        ? ("规划 " + facts.plans + " 次 · 拆出 " + facts.subtasks.length + " 个子任务 · token " +
           TD.int(facts.planner.slow.prompt_tokens) + " in / " +
           TD.int(facts.planner.slow.completion_tokens) + " out · " +
           ((facts.planner.elapsed_ms || 0) / 1000).toFixed(1) + "s · $" +
           (facts.planner.cost_usd || 0).toFixed(5))
        : ("规划 " + facts.plans + " 次 · 拆出 " + facts.subtasks.length + " 个子任务"),
      body: (facts.plan && facts.plan.stage_goal
              ? ("stage_goal：" + facts.plan.stage_goal + "\n\n") : "") +
            (facts.planner ? ((facts.planner.slow.text || "").slice(0, 900) || "（无原文）")
                           : "（这一题没有记录到规划原文）")
    });

    steps.push({
      title: "快模型在候选空间里挑选动作",
      role: "fast_model",
      desc: "候选空间来自 a11y 树解析与慢模型提案；快模型只做排序，逐轮收敛到一个可执行动作。",
      diagram: TD.flow.choiceBlocks(facts.bars, facts.winner),
      meta: facts.ranked
        ? ("候选空间 " + facts.candidates + " 个 · 画出 " + facts.bars.length + " 个动作级候选 · margin " +
           facts.ranked.fast.operation_margin + " / " + facts.ranked.fast.target_margin +
           " · " + facts.ranked.fast.latency_ms + " ms" +
           (facts.ranked.fast.used_image ? " · 用了截图" : " · 只用结构"))
        : ("候选空间 " + facts.candidates + " 个"),
      body: (facts.winner
              ? ("快模型选中：" + (facts.winner.operation || "?") + " → " +
                 (facts.winner.description || facts.winner.id || "") + "\n")
              : "") +
            (facts.opBars && facts.opBars.length
              ? ("操作类型分布：" + facts.opBars.map(function (bar) {
                  return bar.operation + " " + (bar.score || 0).toFixed(3);
                }).join(" · ") + "\n")
              : "") +
            (facts.ranked
              ? ("候选 " + facts.candidates + " 个 → 赢家 " + (facts.ranked.fast.winner || "?") +
                 "\noperation margin " + facts.ranked.fast.operation_margin +
                 " · target margin " + facts.ranked.fast.target_margin +
                 "\n排序耗时 " + facts.ranked.fast.latency_ms + " ms" +
                 (facts.ranked.fast.used_image ? "（用了截图）" : "（只用结构）"))
              : "（没有排序事件）")
    });

    steps.push({
      title: "执行动作",
      role: "action",
      desc: "赢家被编译成可执行动作送进 VM；下面这帧就是动作执行后的画面。",
      extra: facts.executed && facts.executed.frame
        ? TD.flow.frameStrip(facts.frames, facts.executed.frame)
        : null,
      meta: facts.executed
        ? ("$" + (facts.executed.cost_usd || 0).toFixed(5) + " · " +
           ((facts.executed.elapsed_ms || 0) / 1000).toFixed(1) + "s · step " + facts.executed.step +
           " · 累计 $" + (facts.executed.cost_usd || 0).toFixed(5))
        : "",
      body: facts.executed
        ? (JSON.stringify(facts.executed.action || {}, null, 2).slice(0, 700) +
           "\n本步花费 $" + (facts.executed.cost_usd || 0).toFixed(5) +
           " · 用时 " + ((facts.executed.elapsed_ms || 0) / 1000).toFixed(1) + "s")
        : "（没有动作事件）"
    });

    steps.push({
      title: "重规划 / 纠错",
      role: "replan",
      desc: "子任务完成得不符合预期时，慢模型带着失败原因换目标重来（stage_replan 是原生事件），并重写计划。",
      diagram: facts.replan && facts.replan.milestones.length
        ? TD.flow.milestones(facts.replan.milestones, {
            total: facts.replan.milestones.length, plans: 1, limit: 4
          })
        : null,
      meta: facts.replan
        ? ("reason = " + (facts.replan.reason || "—") +
           " · 重规划后拆出 " + facts.replan.milestones.length + " 个子任务")
        : "",
      body: facts.replan
        ? ("reason = " + (facts.replan.reason || "—") +
           "\n\n重规划时的 stage_goal：\n" + (facts.replan.stage_goal || "—") +
           "\n\n重规划后的新计划 stage_goal：\n" + (facts.replan.goal || "—") +
           "\n\n新的子任务：\n" +
           (facts.replan.milestones.length
             ? facts.replan.milestones.map(function (item, index) {
                 return (index + 1) + ". " + item;
               }).join("\n")
             : "（没有记录到新的子任务）"))
        : "（这一题没有触发重规划）"
    });

    return steps;
  }

  /* -------------------------------------------------------------- 页面 */
  TD.views.home = function (container) {
    var overview = TD.state.overview;
    var compare = TD.state.compare;
    container.appendChild(intro());
    if (!overview) {
      container.appendChild(TD.el("div", { class: "callout bad", text: "没有内联的总览数据——请用构建脚本生成站点。" }));
      return;
    }
    var placeholder = TD.el("div", { class: "mech" }, [
      TD.el("p", { class: "eyebrow", text: "机制 · 双模怎么协同" }),
      TD.el("h2", { class: "section-title", text: "一次决策，经过两个模型" }),
      TD.el("p", { class: "section-lede", text: "载入真实链路…" })
    ]);
    container.appendChild(placeholder);

    var row = (compare.rows || []).filter(function (item) { return item.group && item.ours > 0; })[0];
    function loadMechanism() {
      function finish() {
        // 结果段无论如何都要渲染——机制段载入失败也不能让首页少一半
        if (TD.sections && TD.sections.results) TD.sections.results(container);
      }
      if (!row) {
        TD.clear(placeholder);
        placeholder.appendChild(TD.el("p", { class: "section-lede", text: "没有可展示的机制样例。" }));
        finish();
        return;
      }
      TD.loadEpisode(row.run_id || "", row.task, function (episode, error) {
        if (!episode) {
          TD.clear(placeholder);
          placeholder.appendChild(TD.el("p", { class: "section-lede", text: "载入机制样例失败：" + (error || "未知错误") }));
          finish();
          return;
        }
        placeholder.replaceWith(mechanism(episode));
        finish();
      });
    }
    loadMechanism();
  };
})();
