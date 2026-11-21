# Jev CLI 轨迹适配接入

2026-11-19，将已经完成文本模式 CLI 适配的 Jev Python SDK 内置到
`jev_tools/jev-desktop/`，替换原有运行库。阶段控制、任务拆解、Slow 模型及 Jev
服务端权重不在本次修改范围内。

## 实际调用链

`JevSubagent` → 仓库内 `JevLoop` → target/operation 有限选择 →
`_Desktop` / `_Bridge` → `ScopedRuntime` 预算与日志 → `JevEnvironment` →
当前 VM 的原生 CLI。字段内容继续使用共享 Slow 文本模型生成。

后续已将本工程默认有限选择后端切为 k3.1-pro Chat Completions。原始 Jev 请求与
回答结构保留，只更换 HTTP 模型适配层；阶段控制仍未改动。独立模型绑定、凭据传递及
切回 TypeSafe 的方法见 [编排配置](ORCHESTRATION.md#接入-agent-desktop-jev)。
Chat 接入的本地回归和真实 API 协议记录单独保存在
`runs/jev-cli-upgrade-20261119/chat-validation.json` 与 `chat-live-smoke.json`，
不替代前次 SDK 验证记录，也不代表新的 VM 或官方评分结果。

新增 18 个默认按键、绑定窗口的 MAXIMIZE_WINDOW，以及依据已观察状态生成的
Click-only CHECK/UNCHECK。SDK 保留原生身份、焦点和交付保护，不使用截图来补齐目标。

现有 `venv` 已以 editable 方式导入仓库内包；无需重建环境。新环境仍由
`scripts/setup_env.sh` 安装本包。可检查：

```bash
venv/bin/python -c 'import jev_desktop; print(jev_desktop.__file__)'
```

路径应位于本仓库 `jev_tools/jev-desktop/src/jev_desktop/`。

## 文本和文件合同

`desktop.clipboard_text` 在 JevDuo 中默认开启，可显式设为 false；独立 SDK
默认关闭。仅在原生明确拒绝、not_delivered/safe、完整拥有窗口、可读原值和唯一焦点等
条件全部满足时，TYPE_TEXT 才使用剪贴板整字段替换。非空原值通过全选复制并比对完整内容
证明范围；粘贴一次后必须 AX 精确读回。INSERT_TEXT 不使用这条后备路线。

反馈保留 `verification_scope: element_value`、`application_commit: not_verified`。
字段内容正确与保存、发送或提交必须分别核验；优先返回动作后的观察证据。
不确定写入、取消和超时继续保留防重放证据。

文件路径通过调用方显式绑定，例如子目标合同中的：

```json
{
  "agent_input": {
    "app": "Code",
    "window_id": "<当前验证过的窗口 ID>",
    "resources": ["/home/user/project/notes.txt"]
  }
}
```

路径必须来自用户任务或调用方已核验的信息；不能从屏幕推测。列表最多 16 项，必须为绝对路径，
不得重复或包含控制字符。WINDOW 候选提供 OPEN_RESOURCE:0 等编号，CLI 以独立参数
`launch <app> --arg=<literal path> --timeout 5000` 发出一次请求，不经过 shell。
未明确传入列表时可继承静态 `gui_jev.options.resources`；传入空列表可禁用本次绑定。

启动器交接和 UI 变化不等于文件身份已确认。已交付但未核验的请求保留原始失败回执，
后续合同中的全部资源编号都会被禁用，防止换窗口或重绑编号后再次发送。
Ctrl+O 等原有 GUI 文件打开路线仍可选。

## 配置和兼容边界

`config.yaml` 已启用安全失败后的改选、已观察弹出层恢复、两次文本重选、窗口导航、
无效动作去重、目标范围复核、完成复核与有限启动观察重试。这些是执行循环中的条件控制，
不增加阶段规划器。显式 window_id 仍固定本次范围，不自动提供同应用的其他窗口。

仓库内 Rust CLI 的参数定义没有 `list-windows --owned-by`。因此生产配置明确使用
`follow_owned_dialogs: false`；SDK 中该能力仍保留，只有升级并验证对应原生接口后才能启用。
本次不改 VM 镜像、客体 binary 路径、Rust 源码或模型服务配置；客体兼容性不能由 Python
单元测试证明。也没有修改上层 general 子代理或全局截图配置，纯文本限制仅描述 Jev 决策链。

## 验证与备份

```bash
venv/bin/python -m pytest -q
venv/bin/python -m unittest discover -s jev_tools/jev-desktop/tests -q
venv/bin/ruff check src tests
venv/bin/ruff check --config jev_tools/jev-desktop/pyproject.toml jev_tools/jev-desktop/src jev_tools/jev-desktop/tests
```

集成测试使用脚本化 HTTP 和真实 SDK 循环，覆盖文本回调、精确剪贴板读回、字面量路径、
未知交付、取消与预算保护、跨合同防重放、范围和完成复核。它们不连接模型、VM 或集群，
也不产生新的官方成绩。

本次实际验证：JevDuo 全套 301 passed、79 subtests passed；SDK 运行 294 项，
结果 OK，其中 2 项可选检查跳过。两组 Ruff 与空白检查通过。离线构建 wheel 并安装到
独立目录后，使用 `python -I -S` 验证了 35 个模块完整打包、导入来源以及新增候选；
禁用了用户 site-packages，也未依赖外部 SDK 源码。机器记录见
[`validation.json`](../runs/jev-cli-upgrade-20261119/validation.json)。

替换前文件、已有未提交补丁、Git index 和清单保存在仓库外：
`../JevDuo-migration-backups/jev-cli-upgrade-20261119-005107/`。
原有 README 与 .gitignore 改动已保留。迁移验证记录位于
`runs/jev-cli-upgrade-20261119/`。没有 commit/push。
