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
    ok(list(got.keys())[:4] == ["version", "url", "notes", "mandatory"],
       "B4 字段顺序：主字段在前、额外字段在后")

    # B5: 文件不存在 → 正常写出
    d2 = tempfile.mkdtemp(prefix="pub_upd_")
    tmpdirs.append(d2)
    pu.update_version_json(Path(d2), "1.0.1", "u", "n", False)
    got2 = json.loads((Path(d2) / "version.json").read_text(encoding="utf-8"))
    ok(set(got2.keys()) == {"version", "url", "notes", "mandatory"},
       "B5 文件不存在时可正常创建（4 个主字段）")

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

print(f"\n===== {checks - len(fails)}/{checks} 通过 =====")
if fails:
    print("失败项：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
