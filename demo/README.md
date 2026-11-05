# demo/ — OSWorld 2.1 工作台

把**评测结果目录**变成一个**单页工作台**：左侧任务轨、中间双轨同步轨迹播放器、
右侧机制 / 评估 / 成本面板；对比对象是官方 leaderboard 的 **harness 型条目**
（`Sai w/ Claude Opus 5` 等），并用其 HF 公开参考 run 的逐题轨迹做同题对照。

## 边界（硬约束）

demo 层与 harness **完全解耦**：不 import `jevduo` / `jevduo_eval`、不调模型、不碰 VM。
唯一输入是 `docs/RESULT_DIR_CONTRACT.md` 描述的结果目录契约 + `demo/data/` 里的
官方数据快照。`tests/demo/test_site.py` 有一条断言专门守这条边界。

运行时只用标准库（Pillow 可用于构建期缩放帧）。站点**零外部资源加载**，离线可开。

## 目录

```
demo/
├── reader/          读取层：结果目录 → 站点数据契约（纯标准库，全流式）
│   ├── runs.py        发现 run、读 run 级元数据
│   ├── episodes.py    题级终态（分数/停止原因/评测来源/域）
│   ├── events.py      流式解析 events.jsonl → 步骤（plan/决策/核验/replan）
│   ├── llm.py         流式解析 llm_calls.jsonl → usage + 原文（丢 base64）
│   ├── traj.py        流式解析 traj.jsonl / a11y_digests.jsonl → 帧索引
│   ├── paths.py       题号归一 + 集群路径脱敏
│   ├── viewmodel.py   episode view model（站点唯一数据契约）
│   └── aggregate.py   逐题行与汇总（**委托 tools/batch_summary.py**，口径不分叉）
├── web/             站点源码（构建时拷进 demo/site/）
│   ├── index.html
│   └── assets/{app.css,app.js,boot.js,views/*.js,player/*.js}
├── fetch_sai_frames.py 抓官方参考 run 的帧（流式解 tar，跳过 mp4；缓存进 .cache/）
├── data/            vendored 官方数据
│   ├── official/leaderboard.json    官方榜快照（带 source_url / fetched_at）
│   ├── official/sai_tasks.json       Sai 逐题 mean/first/best（HF 134 次干净运行）
│   ├── official/sai_traj/            对比子集的 Sai 轨迹摘要
│   ├── official/compare_subset.json  对比子集（12 题 / 4 组）
│   └── prices.json                   挂牌价（带来源与快照日期）
├── build_site.py    构建脚本（结果目录 → 站点）
├── export_share.py  发布闸门：导出到内网共享目录 + 诚实披露红线自检
├── sync_official.py 刷新官方数据快照
└── frames.py        帧缩放（构建期，唯一保留的旧模块）
```

## 用法

```bash
# 构建（默认数据源 = stage+memory 批次）
python3 demo/build_site.py --out demo/site --benchmark osworld21

# 本地预览
python3 -m http.server 8899 --directory demo/site
# → http://localhost:8899/

# 刷新官方数据（联网）
python3 demo/sync_official.py

# 抓官方参考帧（联网，约 10 分钟；结果缓存进 demo/.cache/，不进 git）
python3 demo/fetch_sai_frames.py

# 发布到内网共享目录（红线不过就拒绝导出）
python3 demo/export_share.py --site demo/site --out /path/to/shared/dir
```

## 页面能看什么

- **总览 `#/`**：Hero 大数字（结论先行，数字滚动动画）→ 按域分行的**战绩热力矩阵** →
  **终态解剖**堆叠条 → **官方 harness 榜单条**（我们的行插在 harness 行里）→
  成本×分数散点（带轴与网格，虚线只画官方已发布值）；**首屏可折叠披露条**。
- **官方对照 `#/compare`**：对比子集 12 题的逐题 Δ、三处口径声明，
  以及**双边轨迹播放器**——同一进度点上并排看我们的帧与官方 Sai 的帧、动作与 reward
  （Sai 的帧来自其 HF 公开参考 run，流式解包只取 PNG）。
- **机制 `#/mechanism`**：快慢双模各干什么 + 失败模式解剖（完成却零分 / 墙钟耗尽 / 空转 / 异常）。
- **轨迹 `#/task/<run>/<task>`**：左任务轨、中间双轨播放器（上轨慢模型原文 + 下轨快模型排序 + 帧序列）、
  右机制面板（plan→act→verify→replan 闭环、需求逐条核验、成本流水）。

## 诚实披露红线（`export_share.py` 逐条断言，不过就拒绝导出）

1. 未出分的题显示「未出分」，**不折算 0**；
2. 虚线只画**官方已发布**参照值，不生成我们的预测分/目标分；
3. 美元成本标注快照日期与底座型号；
4. 快模型（本地 GPU）成本单列第二轴，不折美元；
5. release / 题集 / 步数预算差异出现在每处对比旁；
6. 慢模型原文只搬运 `llm_calls.jsonl` 的真实内容；
7. 排序分数恒标 **softmax，非校准概率**；
8. 帧是离散帧不是录屏，缺失如实标注；
9. 失败与口径在首屏披露（未出分、墙钟主约束、另一代码树实验）；
10. 集群路径与凭据不进站点。

## 数据源

唯一数据源是 stage+memory 批次（`execution_mode=stage`，500 步上限 / 4h 墙钟，
慢模型 MiniMax-M3.1-Flash `reasoning_effort=max`，快模型 JEV，**原生官方评测器**）。
**不使用** `osw21-500-jevfull` / kev 臂的结果。
