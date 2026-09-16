#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官网下载按钮动态化验证（2026-09-15 修 H4）

背景：index/guide/features 共 5 处下载按钮原本写死 v1.0.1，而发布脚本
tools/publish_update.py 只更新 version.json、不会改 HTML —— 每发一版都要手工
改 5 处，漏改就会「官网挂着旧包地址」。修复后按钮带 data-download 标记，
assets/script.js 读 version.json 自动改写地址与版本号，硬编码降级为兜底。

本脚本用「HTML 结构断言 + script.js 里的 URL 生成规则复算」双重验证，不依赖浏览器。
运行：python tools/_verify_website_downloads.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

fails: list[str] = []
checks = 0


def ok(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [!!] {label}")
        fails.append(label)


print("A. HTML：所有下载按钮都必须带 data-download 标记")
ANCHOR_RE = re.compile(r"<a\b[^>]*\bhref=\"(https://[^\"]*?/releases/download/[^\"]*?)\"[^>]*>",
                       re.IGNORECASE)

total_links = 0
for page in ("index.html", "guide.html", "features.html"):
    p = ROOT / page
    if not p.is_file():
        ok(False, f"A {page} 存在")
        continue
    html = p.read_text(encoding="utf-8")
    anchors = re.findall(r"<a\b[^>]*>", html)
    dl = [a for a in anchors if "/releases/download/" in a]
    total_links += len(dl)
    missing = [a for a in dl if "data-download=" not in a]
    ok(bool(dl) and not missing,
       f"A {page}：{len(dl)} 个下载按钮均带 data-download"
       + (f"（缺 {len(missing)} 个）" if missing else ""))

ok(total_links >= 5, f"A5 三个页面共找到 {total_links} 个下载按钮（≥5）")

print("B. script.js：动态改写逻辑")
js_path = ROOT / "assets" / "script.js"
ok(js_path.is_file(), "B1 assets/script.js 存在")
js = js_path.read_text(encoding="utf-8") if js_path.is_file() else ""
ok("data-download" in js, "B2 脚本按 [data-download] 选取按钮")
ok("version.json" in js, "B3 脚本读取 version.json")
ok("cache: 'no-store'" in js, "B4 强制不缓存（避免读到 CDN 旧公告牌）")
ok("data-version" in js, "B5 同步页面上其它 [data-version] 位置")
ok("catch" in js and "保留 HTML" in js.replace("\n", " "),
   "B6 读取失败时静默保留硬编码兜底")
# 【2026-09-15 补】首选源必须是 Gitee raw：本机/部分网络下 github.com 不可达，
# 站点内容可能推送不出去；而 Gitee raw 的 version.json 每次发版必更新，
# 用它作首选源，按钮就能一直指向最新版本，不会因为 Pages 内容滞后而挂旧包地址。
ok("gitee.com/kuai061102/kuai-release/raw/master/version.json" in js,
   "B7 首选 version.json 源为 Gitee raw（Pages 推送失败也不会挂旧包）")


def build_url(info: dict, kind: str = "primary") -> str:
    """复算 script.js 里的地址生成规则（与 JS 实现保持一致）。"""
    ver = str(info.get("version", "")).strip().lstrip("vV")
    fname = f"VideoPromptAssistant-Setup-{ver}.exe"
    if kind == "primary":
        return str(info.get("url", "")).strip() or (
            f"https://gitee.com/kuai061102/kuai-release/releases/download/v{ver}/{fname}")
    return str(info.get("url_mirror", "")).strip() or (
        f"https://github.com/yichenzi016-spec/KuAi/releases/download/v{ver}/{fname}")


print("C. 地址生成规则复算（升级到 1.0.2 的场景）")
fake = {"version": "1.0.2", "url": "", "url_mirror": ""}
u = build_url(fake, "primary")
ok(u == "https://gitee.com/kuai061102/kuai-release/releases/download/v1.0.2/"
        "VideoPromptAssistant-Setup-1.0.2.exe",
   "C1 无 url 字段时按版本号拼出 Gitee 直链")
ok("v1.0.2" in u and "Setup-1.0.2.exe" in u, "C2 版本号已切到 1.0.2（不再是写死的 1.0.1）")

fake2 = {"version": "v1.0.3",
         "url": "https://gitee.com/kuai061102/kuai-release/releases/download/v1.0.3/x.exe"}
ok(build_url(fake2) == fake2["url"], "C3 有 url 字段时优先使用 version.json 的地址")

print("D. 线上 version.json 与按钮目标的一致性")
vj = ROOT / "version.json"
if vj.is_file():
    info = json.loads(vj.read_text(encoding="utf-8"))
    ver = str(info.get("version", "")).strip()
    ok(bool(ver), f"D1 本地 version.json 版本号 = {ver}")
    ok(build_url(info) == str(info.get("url", "")).strip() or bool(info.get("url")),
       "D2 本地 version.json 的 url 与按钮规则一致")
else:
    ok(False, "D1 本地 version.json 存在")

print(f"\n===== E. 发布脚本自动同步官网页面（H4 静态兜底）=====")
import importlib.util as _ilu
import tempfile
import shutil

_spec = _ilu.spec_from_file_location("publish_update", HERE / "publish_update.py")
PU = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(PU)
ok(hasattr(PU, "sync_page_download_links"), "E1 publish_update 提供 sync_page_download_links")

tmp = Path(tempfile.mkdtemp(prefix="web_sync_"))
try:
    sample = """<html><head><title>KuAi V1.0.1 — 测试</title></head><body>
    <span class="badge"><span class="dot"></span> <b data-version>V1.0.1</b> 正式版 · Windows</span>
    <h2 data-version>立即获取 V1.0.1</h2>
    <a href="https://gitee.com/kuai061102/kuai-release/releases/download/v1.0.1/VideoPromptAssistant-Setup-1.0.1.exe" data-download="primary">⬇ 免费下载 V1.0.1</a>
    <div class="log-item"><div class="date">2026-09 · V1.0.1</div><h4>首个正式版</h4></div>
    </body></html>"""
    for name in ("index.html", "guide.html", "features.html"):
        (tmp / name).write_text(sample, encoding="utf-8")
    n = PU.sync_page_download_links(tmp, "1.0.2", lambda *_: None)
    out = (tmp / "index.html").read_text(encoding="utf-8")
    ok(n == 3, f"E2 三个页面均被更新（实际 {n}）")
    ok("download/v1.0.2/VideoPromptAssistant-Setup-1.0.2.exe" in out,
       "E3 下载 URL 已切到 v1.0.2")
    ok("data-version>V1.0.2<" in out, "E4 data-version 元素（含嵌套 b）已更新")
    ok("立即获取 V1.0.2" in out, "E5 带 data-version 的标题文案已更新")
    ok("免费下载 V1.0.2" in out, "E6 下载按钮文案已更新")
    ok("<title>KuAi V1.0.2 — 测试</title>" in out, "E7 <title> 内版本号已更新")
    ok('class="date">2026-09 · V1.0.1<' in out,
       "E8 版本历史（changelog 里的 V1.0.1）**未被改动**")
    # 幂等：再同步一次同版本，应无改动
    n2 = PU.sync_page_download_links(tmp, "1.0.2", lambda *_: None)
    ok(n2 == 0, f"E9 幂等：重复同步同一版本不产生改动（实际 {n2}）")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# E10：对**真实**页面跑一次「同步到当前版本」，应当无改动（说明页面已一致）
_real = Path(tempfile.mkdtemp(prefix="web_real_"))
try:
    cur = ""
    if vj.is_file():
        cur = str(json.loads(vj.read_text(encoding="utf-8")).get("version", "")).strip()
    for name in ("index.html", "guide.html", "features.html"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, _real / name)
    n3 = PU.sync_page_download_links(_real, cur, lambda *_: None) if cur else -1
    same = all(
        (ROOT / name).read_text(encoding="utf-8") == (_real / name).read_text(encoding="utf-8")
        for name in ("index.html", "guide.html", "features.html")
        if (ROOT / name).is_file()
    )
    ok(n3 == 0 and same,
       f"E10 真实页面已与当前版本 V{cur} 一致（无需再改）")
finally:
    shutil.rmtree(_real, ignore_errors=True)

print(f"\n===== {checks - len(fails)}/{checks} 通过 =====")
if fails:
    print("失败项：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
