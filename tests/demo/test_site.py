"""demo 站点构建的单测：结构、离线可开、口径不被折算。

发布闸门（诚实披露红线逐条自检）在 `demo/export_share.py` 及其对应测试里。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from helpers import make_contract_episode, make_contract_run  # noqa: E402


def build_site(tmp_path: Path, runs_root: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "demo" / "build_site.py"),
         "--runs-root", str(runs_root), "--out", str(out_dir),
         "--data-dir", str(REPO_ROOT / "demo" / "data")],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
    )


def _fixture(tmp_path: Path) -> Path:
    runs_root = tmp_path / "rjob_logs"
    run_dir = make_contract_run(runs_root)
    make_contract_episode(run_dir, task_dir="004", score=0.6556)
    make_contract_episode(run_dir, task_dir="025", score=None, stop="exception", steps=0)
    return runs_root


def test_build_site_produces_single_page_app(tmp_path):
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    result = build_site(tmp_path, runs_root, out_dir)
    assert result.returncode == 0, result.stderr
    assert (out_dir / "index.html").is_file()
    assert (out_dir / "assets" / "app.css").is_file()
    assert (out_dir / "assets" / "app.js").is_file()
    assert (out_dir / "assets" / "boot.js").is_file()
    assert (out_dir / "assets" / "views" / "home.js").is_file()
    assert (out_dir / "assets" / "views" / "results.js").is_file()
    assert (out_dir / "assets" / "views" / "facts.js").is_file()
    assert (out_dir / "assets" / "data" / "overview.json").is_file()
    assert (out_dir / "assets" / "data" / "compare.json").is_file()
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert '"attempted"' in html
    assert 'id="data-compare"' in html


def test_index_references_only_existing_scripts(tmp_path):
    """index.html 引用的每个脚本/样式都必须在产物里存在（防止漏拷视图）。"""
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r'<script src="([^"]+)"', html)
    assert sources
    for source in sources:
        assert (out_dir / source).is_file(), f"index.html 引用了不存在的脚本：{source}"
    styles = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
    assert styles
    for href in styles:
        assert (out_dir / href).is_file(), f"index.html 引用了不存在的样式：{href}"


def test_built_site_has_no_external_resources(tmp_path):
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    for pattern in (r'src\s*=\s*["\']https?://', r'href\s*=\s*["\']https?://'):
        assert not re.search(pattern, html, re.IGNORECASE)


def test_built_site_keeps_unscored_as_null(tmp_path):
    """未出分的题在站点数据里必须是 null，不能折算 0（红线 1）。"""
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    overview = json.loads((out_dir / "assets" / "data" / "overview.json").read_text(encoding="utf-8"))
    unscored = [row for row in overview["rows"] if row.get("status") != "scored"]
    assert unscored, "夹具里应当有一道未出分的题"
    assert all(row["score"] is None for row in unscored)


def test_overview_carries_disclaimer_notes(tmp_path):
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    overview = json.loads((out_dir / "assets" / "data" / "overview.json").read_text(encoding="utf-8"))
    notes = " ".join(overview["disclaimer_notes"])
    assert "底座不同" in notes
    assert "未出分" in notes
    assert "墙钟" in notes


def test_demo_layer_does_not_import_harness():
    offenders = []
    for path in (REPO_ROOT / "demo").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*(from|import)\s+jevduo(_eval)?\b", text, re.MULTILINE):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


# ---------------------------------------------------------------- 发布闸门


def test_red_line_checks_pass_on_built_site(tmp_path):
    """发布前的诚实披露红线自检必须全绿。"""
    from demo.export_share import check_no_secrets, check_red_lines

    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    checks = check_red_lines(out_dir)
    failed = [(name, detail) for name, ok, detail in checks if not ok]
    assert failed == [], failed
    assert check_no_secrets(out_dir)[0][1] is True


def test_export_refuses_when_red_line_fails(tmp_path):
    """红线不过 → 拒绝导出（退出码非 0，且不产出目标目录）。"""
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    # 人为把一个未出分的题折算成 0 分
    path = out_dir / "assets" / "data" / "overview.json"
    overview = json.loads(path.read_text(encoding="utf-8"))
    for row in overview["rows"]:
        if row.get("status") != "scored":
            row["score"] = 0
    path.write_text(json.dumps(overview, ensure_ascii=False), encoding="utf-8")
    # index.html 里内联的是同一份数据，也要一起改
    html_path = out_dir / "index.html"
    html = html_path.read_text(encoding="utf-8")
    marker = '<script type="application/json" id="data-overview">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    blob = json.dumps(overview, ensure_ascii=False, separators=(",", ":"))
    html_path.write_text(html[:start] + blob + html[end:], encoding="utf-8")

    target = tmp_path / "shared"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "demo" / "export_share.py"),
         "--site", str(out_dir), "--out", str(target)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "拒绝导出" in result.stdout
    assert not target.exists()


def test_built_site_ships_view_stylesheet_and_official_reference_data(tmp_path):
    """站点要带上视图样式与官方参考轨迹（含帧映射），否则对照页是对不上的。"""
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    assert (out_dir / "assets" / "views.css").is_file()
    assert (out_dir / "assets" / "browser.css").is_file()
    assert (out_dir / "assets" / "mechanism.css").is_file()
    traj_dir = out_dir / "assets" / "data" / "sai_traj"
    assert traj_dir.is_dir()
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in traj_dir.glob("*.json")]
    assert payloads, "官方参考轨迹没进站点"
    assert all("steps" in payload for payload in payloads)


def test_every_nav_view_is_registered():
    """导航里的每个视图都要有对应脚本并注册 TD.views.<name>。

    踩过的坑：把 overview 改成 sections.results 后，路由的默认视图名没跟着改，
    首页直接报「view is not a function」。这条测试就是防这个。
    """
    html = (REPO_ROOT / "demo" / "index.html").read_text(encoding="utf-8") if (
        REPO_ROOT / "demo" / "index.html").is_file() else (
        REPO_ROOT / "demo" / "web" / "index.html").read_text(encoding="utf-8")
    views_dir = REPO_ROOT / "demo" / "web" / "assets" / "views"
    nav_views = re.findall(r'data-view="([a-z]+)"', html)
    scripts = set(re.findall(r"assets/views/([a-z]+)\.js", html))
    assert nav_views, "导航里应当有视图入口"
    for view in nav_views:
        assert view in scripts, f"导航里的 {view} 没有对应的 views/{view}.js"
        text = (views_dir / f"{view}.js").read_text(encoding="utf-8")
        assert f"TD.views.{view}" in text, f"views/{view}.js 没有注册 TD.views.{view}"
    # 路由的默认视图名必须是个已注册的视图
    app_js = (REPO_ROOT / "demo" / "web" / "assets" / "app.js").read_text(encoding="utf-8")
    match = re.search(r'parts\[0\] \|\| "([a-z]+)"', app_js)
    assert match, "路由里找不到默认视图名"
    default = match.group(1)
    assert default in nav_views, f"默认视图 {default} 不在导航里"
    assert f"TD.views.{default}" in (views_dir / f"{default}.js").read_text(encoding="utf-8")


def test_sticky_column_is_not_stretched_by_grid():
    """吸顶列必须 align-items:start —— 否则 grid 默认 stretch 会把它拉成整行高，
    sticky 失效：往下滚卡片就滚走了，左栏看起来是空的（踩过的坑）。
    """
    css = (REPO_ROOT / "demo" / "web" / "assets" / "app.css").read_text(encoding="utf-8")
    match = re.search(r"\.mech-layout\s*\{([^}]*)\}", css, re.DOTALL)
    assert match, "app.css 里找不到 .mech-layout"
    assert "align-items: start" in match.group(1), (
        ".mech-layout 必须 align-items: start，否则吸顶元素被拉伸成整行高、sticky 失效"
    )


def test_web_assets_stay_under_300_lines():
    """前端单模块 ≤300 行（AGENTS.md 纪律 3）。

    demo 层的 JS/CSS 也算「模块」——超了就该拆职责，而不是接着往里塞。
    """
    assets = REPO_ROOT / "demo" / "web" / "assets"
    offenders = []
    for path in sorted(assets.rglob("*")):
        if path.suffix not in {".js", ".css"} or not path.is_file():
            continue
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > 300:
            offenders.append(f"{path.relative_to(REPO_ROOT)} ({count} 行)")
    assert offenders == [], "前端模块超 300 行：" + "、".join(offenders)


def test_built_index_carries_inline_data(tmp_path):
    """构建产物的 index.html 必须带**非空**的内联数据。

    症状：首页报「没有内联的总览数据——请用构建脚本生成站点。」
    成因：有人把**源** index.html 直接拷到产物目录，把构建时注入的内联数据冲掉了。
    """
    runs_root = _fixture(tmp_path)
    out_dir = tmp_path / "site"
    assert build_site(tmp_path, runs_root, out_dir).returncode == 0
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    for slot in ("data-overview", "data-compare"):
        match = re.search(r'id="' + slot + r'">(.*?)</script>', html, re.DOTALL)
        assert match, f"index.html 里找不到 {slot} 槽位"
        body = match.group(1).strip()
        assert body, f"{slot} 槽位是空的（内联数据被冲掉了？）"
        assert isinstance(json.loads(body), dict), f"{slot} 不是合法 JSON"
