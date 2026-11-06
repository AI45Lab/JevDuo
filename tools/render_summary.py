"""最小结果渲染器（demo 占位）：读 run 目录 summary.json → 单文件 HTML 表格。
零依赖（纯标准库），输出自包含可离线打开。完整 demo studio 自 feature/demo-studio 平移（后续里程碑）。"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

_PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>JevDuo run {run_id}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.9rem}}
th{{background:#f5f5f5}}
.scored{{color:#0a7d32}}.failed_unscored{{color:#c0392b}}
h1{{font-size:1.2rem}} code{{background:#f4f4f4;padding:0 .3rem}}
</style>
<h1>JevDuo run <code>{run_id}</code></h1>
<p>benchmark: <b>{benchmark}</b> · total {total} · scored {scored} ·
failed_unscored {failed_unscored} · average {average}</p>
<table><tr><th>task</th><th>score</th><th>status</th><th>stop_reason</th><th>steps</th></tr>
{rows}
</table></html>"""


def render(run_root: Path) -> str:
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    rows = []
    for task in summary.get("tasks", []):
        rows.append(
            "<tr><td>{}</td><td>{}</td><td class='{}'>{}</td><td>{}</td><td>{}</td></tr>".format(
                html.escape(str(task.get("task_id"))),
                task.get("score"),
                html.escape(str(task.get("status"))),
                html.escape(str(task.get("status"))),
                html.escape(str(task.get("stop_reason"))),
                task.get("steps"),
            )
        )
    return _PAGE.format(
        run_id=html.escape(str(summary.get("run_id"))),
        benchmark=html.escape(str(summary.get("benchmark"))),
        total=summary.get("total"), scored=summary.get("scored"),
        failed_unscored=summary.get("failed_unscored"),
        average=summary.get("average_score"), rows="\n".join(rows),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 run summary 为单文件 HTML")
    parser.add_argument("run_root", help="run 目录（含 summary.json）")
    parser.add_argument("--output", default=None, help="输出 HTML 路径（默认 <run_root>/summary.html）")
    args = parser.parse_args()
    run_root = Path(args.run_root)
    output = Path(args.output) if args.output else run_root / "summary.html"
    output.write_text(render(run_root), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
