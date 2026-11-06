/* 轨迹浏览：三栏工作台（页面骨架）
 *
 * 左：任务卡（首帧预览 + 域 + 得分 + 步数）
 * 中：JevDuo 与 Sai 两边独立回放，**花费与用时按步骤累计**
 * 右：Task Instruction、模型底座、评测（得分 / 步数 / 逐条要求）
 *
 * 播放器本身在 browser_player.js（TD.browserPlayer）——拆开是为了让这个文件
 * 只管页面结构，别把状态机混进来（单模块 ≤300 行）。
 */
(function () {
  "use strict";
  var TD = window.TD;

  function domainLabel(domain) {
    var map = {
      browser_web: "Browser", libreoffice_impress: "Impress", libreoffice_calc: "Calc",
      libreoffice_writer: "Writer", email_client: "Email", gimp_image: "GIMP",
      video_editor: "Video", media_player: "Media", vscode_ide: "VS Code",
      system_settings: "Settings", other: "Other"
    };
    return map[domain] || domain || "Other";
  }

  /* ------------------------------------------------------------------ 页面 */
  TD.views.browser = function (container) {
    var compare = TD.state.compare;
    if (!compare) {
      container.appendChild(TD.el("div", { class: "callout bad", text: "没有内联的对照数据——请用构建脚本生成站点。" }));
      return;
    }
    var subset = (compare.rows || []).filter(function (row) { return row.group; });

    var rail = TD.el("div", { class: "panel rail-panel" });
    var middle = TD.el("div", { class: "panel mid-panel" });
    var right = TD.el("div", { class: "panel right-panel" });
    var grid = TD.el("div", { class: "workbench3" }, [rail, middle, right]);
    container.appendChild(grid);

    rail.appendChild(TD.el("div", { class: "panel-head" }, [
      TD.el("h2", { text: "Tasks" }),
      TD.el("span", { class: "hint", text: subset.length + " Samples" })
    ]));
    var cards = TD.el("div", { class: "task-list" });
    rail.appendChild(cards);

    var episodes = {};
    var saiData = {};
    var active = null;

    function renderCard(row, episode) {
      var frame = episode && episode.frames && episode.frames[0] ? episode.frames[0].src : null;
      var raw = (episode && episode.instruction ? episode.instruction : "").trim();
      var summary = raw.length > 110 ? raw.slice(0, 110).replace(/\s+\S*$/, "") + "…" : raw;
      var card = TD.el("div", {
        class: "task-card" + (active === row.task ? " active" : ""),
        onclick: function () { select(row.task); }
      }, [
        frame
          ? TD.el("img", { src: frame, alt: "Task " + row.task + " 首帧" })
          : TD.el("div", { class: "thumb-missing", text: "无帧" }),
        TD.el("div", { class: "task-card-body" }, [
          TD.el("div", { class: "task-id", text: "Task " + row.task }),
          TD.el("div", { class: "task-summary", text: summary || "—" }),
          TD.el("div", { class: "task-meta", text:
            (row.ours === null ? "未出分" : (row.ours * 100).toFixed(2) + "%") +
            " · " + ((episode && episode.steps) || 0) + " Steps" })
        ])
      ]);
      return card;
    }

    function select(taskId) {
      active = taskId;
      TD.clear(cards);
      subset.forEach(function (row) {
        cards.appendChild(renderCard(row, episodes[row.task]));
      });
      renderMiddle(taskId);
      renderRight(taskId);
    }

    function renderMiddle(taskId) {
      TD.clear(middle);
      var row = subset.filter(function (item) { return item.task === taskId; })[0] || {};
      var episode = episodes[taskId];
      middle.appendChild(TD.el("div", { class: "panel-head" }, [
        TD.el("h2", { text: "Task " + taskId }),
        TD.el("span", { class: "hint", text: row.group || "" })
      ]));
      var duo = TD.el("div", { class: "duo" }, [
        TD.browserPlayer.player({
          title: "JevDuo",
          who: "MiniMax-M3.1-Flash + JEV",
          score: episode && episode.score !== null ? "得分 " + episode.score.toFixed(4) : "",
          items: episode ? TD.browserPlayer.ourItems(episode) : []
        }),
        TD.browserPlayer.player({
          title: "Sai",
          who: "Claude Opus 5",
          score: saiData[taskId] && saiData[taskId].score !== null
            ? "得分 " + saiData[taskId].score.toFixed(4) : "",
          items: saiData[taskId]
            ? TD.browserPlayer.saiItems(saiData[taskId], compare.sai_usd_per_task)
            : []
        })
      ]);
      middle.appendChild(duo);
    }

    function renderRight(taskId) {
      TD.clear(right);
      var row = subset.filter(function (item) { return item.task === taskId; })[0] || {};
      var episode = episodes[taskId];
      var sai = saiData[taskId];
      right.appendChild(TD.el("div", { class: "panel-head" }, [TD.el("h2", { text: "Evaluation" })]));

      var scoreBox = TD.el("div", { class: "score-box" }, [
        TD.el("div", { class: "score-big", text: row.ours === null ? "—" : (row.ours * 100).toFixed(2) + "%" }),
        TD.el("div", { class: "score-sub", text: "JevDuo · " + ((episode && episode.steps) || 0) + " 步" }),
        TD.el("div", { class: "score-sub", text: "Sai · " + (row.sai_best === null || row.sai_best === undefined ? "—" : (row.sai_best * 100).toFixed(2) + "%") })
      ]);
      right.appendChild(scoreBox);

      right.appendChild(TD.el("div", { class: "meta-block" }, [
        TD.el("div", { class: "meta-title", text: "Task " + taskId }),
        TD.el("div", { class: "meta-line", text: "域：" + domainLabel((episode && episode.domain) || "") }),
        TD.el("div", { class: "meta-line", text: "慢模型：MiniMax-M3.1-Flash（reasoning_effort=max）" }),
        TD.el("div", { class: "meta-line", text: "快模型：JEV / qwen-jev-choice" }),
        TD.el("div", { class: "meta-line", text: "评测：原生官方评测器 · release v2.1" })
      ]));

      right.appendChild(TD.el("div", { class: "meta-block" }, [
        TD.el("div", { class: "meta-title", text: "对比口径" }),
        TD.el("div", { class: "meta-line", text:
          "同 release（v2.1）· 同原生官方评测器 · 同题号 → 逐题可比。" }),
        TD.el("div", { class: "meta-line", text:
          "题集不同：我们 " + compare.ours_attempted + " 题尝试（官方 108 题全量）；" +
          compare.ours_unscored + " 题未出分，按「未出分」处理，不折算 0。" }),
        TD.el("div", { class: "meta-line", text:
          "底座不同：我们 MiniMax-M3.1-Flash，官方 Sai 用 Claude Opus 5——成本比 1/" +
          (compare.cost_ratio || "?") + " 主要来自底座。" })
      ]));

      right.appendChild(TD.el("div", { class: "meta-block" }, [
        TD.el("div", { class: "meta-title", text: "Task Instruction" }),
        TD.el("div", { class: "instruction", text: (episode && episode.instruction) || "—" })
      ]));

      var reqs = (episode && episode.requirements) || [];
      var rubric = TD.el("div", { class: "meta-block" }, [
        TD.el("div", { class: "meta-title", text: "Rubric（逐条要求核验）" })
      ]);
      if (reqs.length) {
        reqs.forEach(function (req) {
          rubric.appendChild(TD.el("div", { class: "rubric-item" }, [
            TD.el("div", { class: "rubric-head" }, [
              TD.el("b", { text: req.id }),
              TD.el("span", { class: req.status === "achieved" ? "good" : "bad", text: String(req.status || "?") })
            ]),
            TD.el("div", { class: "small dim", text: (req.evidence || "").slice(0, 120) })
          ]));
        });
      } else {
        rubric.appendChild(TD.el("div", { class: "small dim", text: "这一题没有 requirement_checks 记录。" }));
      }
      right.appendChild(rubric);
    }

    /* 载入：我们的逐题数据 + Sai 的轨迹 */
    var pending = subset.length;
    function ready() {
      pending -= 1;
      if (pending > 0) return;
      select(subset.length ? subset[0].task : null);
    }
    subset.forEach(function (row) {
      TD.loadEpisode(row.run_id || "", row.task, function (episode, error) {
        episodes[row.task] = episode || null;
        ready();
      });
      if (typeof fetch === "function") {
        fetch("assets/data/sai_traj/" + row.task + ".json").then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        }).then(function (data) {
          data.task = row.task;
          saiData[row.task] = data;
          ready();
        }).catch(function () { ready(); });
      } else {
        ready();
      }
    });
  };
})();
