#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_update.py 回归验证（2026-09-10）

背景：官网发布工具 tools/publish_update.py 有两个真实缺陷——
  ① GUI 首次使用必失败：on_publish 把 cfg["token"]（恒为空）传给 do_publish，
     用户在界面里粘贴了 PAT 也没用，永远报「缺少 GitHub 令牌」。
  ② update_version_json 只写 4 个字段，会把原有 version.json 里的
     min_supported 等额外字段静默删掉。

本脚本用「源码 AST 断言 + 真实函数调用」双重验证修复，不依赖 GUI。
运行：python tools/_verify_publish_update.py
"""
from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")  # 防 GBK 控制台 UnicodeEncodeError 假失败
except Exception:
    pass

HERE = Path(__file__).resolve().parent
PUB = HERE / "publish_update.py"
if not PUB.is_file():
    print("FAIL: 找不到 publish_update.py")
    sys.exit(1)

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


SRC = PUB.read_text(encoding="utf-8")
TREE = ast.parse(SRC)

# ---------------------------------------------------------------- A 静态锚点
print("A. 静态锚点（AST + 文本）")

# A1: on_publish 里 do_publish 的 token 关键字实参必须是 tok
tok_arg = None
found_call = False
for node in ast.walk(TREE):
    if isinstance(node, ast.FunctionDef) and node.name == "on_publish":
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "do_publish"):
                found_call = True
                for kw in sub.keywords:
                    if kw.arg == "token":
                        tok_arg = kw.value
ok(found_call, "A1 on_publish 中存在 do_publish 调用")
ok(isinstance(tok_arg, ast.Name) and getattr(tok_arg, "id", "") == "tok",
   "A2 do_publish 的 token 实参是界面输入的 tok（不是 cfg['token']）")
ok('token=cfg["token"]' not in SRC and "token=cfg['token']" not in SRC,
   "A3 已不存在 token=cfg[...] 的错误写法")

# A4: update_version_json 里确实做了额外字段合并
uv_fn = None
for node in ast.walk(TREE):
    if isinstance(node, ast.FunctionDef) and node.name == "update_version_json":
        uv_fn = node
ok(uv_fn is not None, "A4 找到 update_version_json 函数")
has_star_star = False
if uv_fn is not None:
    for sub in ast.walk(uv_fn):
        if isinstance(sub, ast.Dict) and any(k is None for k in sub.keys):
            has_star_star = True  # {**extra}
ok(has_star_star, "A5 update_version_json 里有 {**extra} 合并（保留额外字段）")

# ---------------------------------------------------------------- B 功能验证
print("B. update_version_json 功能验证")
sys.path.insert(0, str(HERE))
import publish_update as pu  # noqa: E402

tmpdirs: list[str] = []
try:
    # B1/B2: 已存在且含 min_supported
    d1 = tempfile.mkdtemp(prefix="pub_upd_")
    tmpdirs.append(d1)
    p1 = Path(d1) / "version.json"
    p1.write_text(json.dumps({
        "version": "1.0.1",
        "url": "old-url",
        "notes": "old",
        "mandatory": True,
        "min_supported": "1.0.1",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pu.update_version_json(Path(d1), "1.2.0", "new-url", "新说明", False)
    got = json.loads(p1.read_text(encoding="utf-8"))
    ok(got.get("min_supported") == "1.0.1", "B1 额外字段 min_supported 被保留")
    ok(got.get("version") == "1.2.0" and got.get("url") == "new-url"
       and got.get("notes") == "新说明", "B2 主字段被正确覆盖")
    ok(got.get("mandatory") is False, "B3 mandatory 由 True 正确改为 False")
    # 2026-09-13 修：主字段在 09-12 增加了 url_mirror（Gitee 主用 + GitHub 兜底），
    # 原断言只认 4 个字段，属脚本过时（非回归）。
    ok(list(got.keys())[:5] == ["version", "url", "url_mirror", "notes", "mandatory"],
       "B4 字段顺序：主字段在前（version/url/url_mirror/notes/mandatory）、额外字段在后")
    ok(got.get("url_mirror") == "", "B4b 未传 url_mirror 时写出空串（字段始终存在，读写对齐）")

    # B5: 文件不存在 → 正常写出
    d2 = tempfile.mkdtemp(prefix="pub_upd_")
    tmpdirs.append(d2)
    pu.update_version_json(Path(d2), "1.0.1", "u", "n", False)
    got2 = json.loads((Path(d2) / "version.json").read_text(encoding="utf-8"))
    ok(set(got2.keys()) == {"version", "url", "url_mirror", "notes", "mandatory"},
       "B5 文件不存在时可正常创建（5 个主字段，含 url_mirror）")

    # B6: 已有文件是坏 JSON → 不崩，按 4 字段写出
    d3 = tempfile.mkdtemp(prefix="pub_upd_")
    tmpdirs.append(d3)
    (Path(d3) / "version.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
    pu.update_version_json(Path(d3), "1.0.1", "u", "n", False)
    got3 = json.loads((Path(d3) / "version.json").read_text(encoding="utf-8"))
    ok(got3.get("version") == "1.0.1", "B6 坏 JSON 时不崩溃且能写出新内容")

    # ------------------------------------------------------------ C 版本号校验
    print("C. _valid_version 校验")
    ok(pu._valid_version("1.0.1") is True, "C1 1.0.1 合法")
    ok(pu._valid_version("v1.2.3") is True, "C2 v1.2.3 合法（自动去 v）")
    ok(pu._valid_version("1.0") is False, "C3 1.0 不合法（必须三段）")
    ok(pu._valid_version("1.2.3.4") is False, "C4 1.2.3.4 不合法")
finally:
    for d in tmpdirs:
        shutil.rmtree(d, ignore_errors=True)
    print(f"  （已清理 {len(tmpdirs)} 个临时目录）")

# ---------------------------------------------------------------- D git 链路
# 背景（2026-09-13 真实踩坑）：本机 git 配了 credential.helper=manager，
# 它会让 `git push <带token的URL>` **静默失败**——退出码 128、stdout/stderr **全空**。
# 且原实现在 `add/commit` **之前**就 pull，此时 version.json 已是脏文件，
# `pull --rebase` 会因「unstaged changes」直接拒绝。
print("D. git_commit_push：凭据静默失败规避 + 提交/变基顺序")

ok(hasattr(pu, "_GIT_SAFE"), "D1 存在 _GIT_SAFE 常量")
_gs = getattr(pu, "_GIT_SAFE", [])
ok("credential.helper=" in _gs and "core.askpass=" in _gs,
   "D2 _GIT_SAFE 同时清空 credential.helper 与 core.askpass")

_gcp_src = None
for _node in ast.walk(TREE):
    if isinstance(_node, ast.FunctionDef) and _node.name == "git_commit_push":
        try:
            _gcp_src = ast.unparse(_node)
        except Exception:
            _gcp_src = None
ok(_gcp_src is not None, "D3 git_commit_push 可反解为源码")

if _gcp_src:
    ok(_gcp_src.count("_GIT_SAFE") >= 2, "D4 pull 与 push 都带上了 _GIT_SAFE")
    ok("'--rebase'" in _gcp_src or '"--rebase"' in _gcp_src,
       "D5 使用 pull --rebase（避免与 GitHub 机器人分叉后被拒）")
    _i_c, _i_p = _gcp_src.find("'commit'"), _gcp_src.find("'pull'")
    ok(_i_c != -1 and _i_p != -1 and _i_c < _i_p,
       "D6 先 commit 后 pull（脏工作树下 rebase 会被拒）")

# D7：真实 git 往返 —— 造一个「机器人已抢先提交」的分叉，验证仍能推上去且不丢机器人改动
_git = shutil.which("git")
ok(bool(_git), "D7 环境有 git")
if _git:
    _root = tempfile.mkdtemp(prefix="pub_git_")
    tmpdirs.append(_root)

    def _run(cwd, *a):
        return subprocess.run([_git, *a], cwd=cwd, capture_output=True)

    _bare = os.path.join(_root, "remote.git")
    _run(_root, "init", "--bare", "-q", "-b", "main", _bare)

    _work = os.path.join(_root, "work")
    _run(_root, "init", "-q", "-b", "main", _work)
    _run(_work, "remote", "add", "origin", _bare)
    _run(_work, "config", "user.name", "t")
    _run(_work, "config", "user.email", "t@t")
    (Path(_work) / "version.json").write_text('{"version":"1.0.1"}\n', encoding="utf-8")
    (Path(_work) / "downloads.json").write_text('{"n":1}\n', encoding="utf-8")
    _run(_work, "add", "-A")
    _run(_work, "commit", "-q", "-m", "init")
    _run(_work, "push", "-q", "origin", "HEAD:main")
    _run(_work, "fetch", "-q", "origin")
    _run(_work, "branch", "-q", "--set-upstream-to=origin/main", "main")

    # 模拟 GitHub Action 机器人：另一个克隆往同一分支再提交 → 制造分叉
    _bot = os.path.join(_root, "bot")
    _run(_root, "clone", "-q", _bare, _bot)
    _run(_bot, "config", "user.name", "b")
    _run(_bot, "config", "user.email", "b@b")
    (Path(_bot) / "downloads.json").write_text('{"n":2}\n', encoding="utf-8")
    _run(_bot, "add", "-A")
    _run(_bot, "commit", "-q", "-m", "chore: sync download count")
    _run(_bot, "push", "-q", "origin", "HEAD:main")

    # 被测：改写 version.json 后走完整 发布 → 提交 → rebase → 推送
    pu.update_version_json(Path(_work), "1.2.0", "u-gitee", "新说明", False, "u-gh")
    _logs: list[str] = []
    pu.git_commit_push(Path(_work), "1.2.0", _logs.append)

    def _blr(*a):
        r = subprocess.run([_git, "--git-dir", _bare, *a], capture_output=True)
        return (r.stdout or b"").decode("utf-8", "replace").strip()

    _hist = _blr("log", "--format=%s", "main")
    ok("发布 v1.2.0：更新 version.json" in _hist,
       "D8 发布提交已进入远端的 main（push 真的成功了）")
    ok("chore: sync download count" in _hist,
       "D9 机器人提交仍在历史里（rebase 而非强推覆盖）")
    _vj = _blr("show", "main:version.json")
    ok('"version": "1.2.0"' in _vj, "D10 远端 version.json 已是新版本")
    ok('"url_mirror": "u-gh"' in _vj, "D11 远端 version.json 含 url_mirror 兜底地址")
    _dj = _blr("show", "main:downloads.json")
    ok('"n":2' in _dj, "D12 机器人改的 downloads.json 未被本地旧内容回退")

    shutil.rmtree(_root, ignore_errors=True)
    print("  （已清理 D 段临时 git 仓库）")

print(f"\n===== {checks - len(fails)}/{checks} 通过 =====")
if fails:
    print("失败项：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
