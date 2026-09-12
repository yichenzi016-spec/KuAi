#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KuAi 源码一键备份工具  (backup_source.py)
================================================

【为什么做这个】
用户要求：发布新版本时，把「源代码」自动上传到自己的私有仓库做备份。
源代码不开源，所以备份目标是 私有(private) 仓库，绝不推到公开仓库。

默认目标平台：Gitee（码云）——国内访问快、稳定、中文、私有仓库免费。
GitHub 也可作为目标，通过 --platform github 切换。

【它做了什么】
    1) 进入源码目录 E:/AI/AI提示词工具最新软件架构（该目录已是 git 仓库）
    2) git add -A 收集全部源码改动
    3) 有改动时 git commit（提交信息带日期），无改动则跳过
    4) git push 到私有备份仓库（默认 main:main，与本地分支同名，不做分支映射）

【前置条件（一次性）】
Gitee 路线（默认）：
1. 网页上已创建好 Gitee 私有仓库：https://gitee.com → 右上角 + → 新建仓库
     路径 KuAi061102/ku-ai，私有，不要勾任何初始化选项（保持空仓）。
2. Gitee → 设置 → 私人令牌 → 生成新令牌（建议权限：user_info、projects）
   把令牌原文写到本地：~/.workbuddy/gitee_token   （一字一行，不加空格）
3. （可选）告诉 AI 仓库所有者和名字，本工具默认 KuAi061102/ku-ai。

GitHub 路线（--platform github）：
1. github.com → New repository → KuAi-source-backup → Private → 空仓
2. github.com/settings/tokens → Fine-grained token → 加上该仓库的 Contents R/W
   写入 ~/.workbuddy/publish_token（已有）即可。

【怎么跑】
    python backup_source.py            # Gitee 默认，推送
    python backup_source.py --no-push  # 只本地提交不推送
    python backup_source.py --platform github --repo yichenzi016-spec/KuAi-source-backup
    python backup_source.py --src D:/codes/myapp --platform gitee --branch main:main
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# 默认配置（Gitee 路线）
# ----------------------------------------------------------------------------
DEFAULT_SRC = Path(r"E:/AI/AI提示词工具最新软件架构")
DEFAULT_PLATFORM = "gitee"
DEFAULT_REPO = "kuai061102/ku-ai"                   # 私有备份仓库（Gitee 显示 KUAI061102，但 URL 必须小写）
DEFAULT_BRANCH = "main:main"                       # 本地 main → 远端 main（不映射，避免主备分支分叉）

# 各平台 token 读取路径（按优先级；缺一个自动跳过）
PLATFORM_TOKEN_FILES = {
    "gitee":  [Path.home() / ".workbuddy" / "gitee_token",        # 推荐
               Path.home() / ".workbuddy" / "publish_token"],
    "github": [Path.home() / ".workbuddy" / "publish_token",
               Path.home() / ".workbuddy" / "backup_token"],
}
PLATFORM_HOST = {
    "gitee":  "gitee.com",
    "github": "github.com",
}


def read_token(platform: str) -> tuple[str, str]:
    """返回 (token, 来源文件 name)。"""
    for p in PLATFORM_TOKEN_FILES.get(platform, []):
        try:
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t, p.name
        except Exception:
            continue
    return "", ""


def run(cmd: list, cwd: Path) -> tuple[int, str]:
    """跑 git 命令并返回 (exit_code, 合并输出)。"""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out.strip()
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="KuAi 源码一键备份到 Gitee/GitHub 私有仓库")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="源码目录")
    ap.add_argument("--platform", default=DEFAULT_PLATFORM, choices=["gitee", "github"],
                    help="目标平台（默认 gitee）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="仓库 owner/name")
    ap.add_argument("--branch", default=DEFAULT_BRANCH,
                    help="本地→远端 分支映射，默认 main:main")
    ap.add_argument("--token-file", default="", help="自定义 token 文件路径（可选）")
    ap.add_argument("--no-push", action="store_true", help="只提交不推送")
    args = ap.parse_args()

    src = Path(args.src)
    if not (src / ".git").is_dir():
        print(f"❌ {src} 不是 git 仓库（缺少 .git）")
        return 1

    token = ""
    src_name = ""
    if args.token_file:
        try:
            t = Path(args.token_file).read_text(encoding="utf-8").strip()
            if t:
                token, src_name = t, Path(args.token_file).name
        except Exception:
            pass
    if not token:
        token, src_name = read_token(args.platform)
    if not token:
        print(f"❌ 找不到 {args.platform} 平台的可用 token。")
        print(f"   Gitee：把私人令牌写到  {PLATFORM_TOKEN_FILES['gitee'][0]}")
        print(f"   GitHub：写到  {PLATFORM_TOKEN_FILES['github'][0]}")
        print(f"   或用 --token-file 指定别的文件")
        return 1
    print(f"✓ 使用 token: {src_name}  平台: {args.platform}")

    # 解析分支映射 local:remote
    if ":" in args.branch:
        local_branch, remote_branch = args.branch.split(":", 1)
    else:
        local_branch = remote_branch = args.branch

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"=== 源码备份 {stamp} ===")
    print(f"源目录: {src}")
    print(f"目标: https://{PLATFORM_HOST[args.platform]}/{args.repo}  ({local_branch} → {remote_branch})")

    # 0) 校验本地分支存在（默认 main:main，不做分支映射，避免主备分支分叉）
    code, out = run(["git", "rev-parse", "--verify", local_branch], src)
    if code != 0:
        print(f"❌ 本地分支 {local_branch} 不存在")
        return 1

    # 1) add -A
    code, out = run(["git", "add", "-A"], src)
    if code != 0:
        print(f"❌ git add 失败:\n{out}"); return 1
    print("✓ git add -A")

    # 2) 有变更才提交
    code, out = run(["git", "diff", "--cached", "--quiet"], src)
    if code == 0:
        print("ℹ 无新改动，跳过提交")
    else:
        msg = f"源码备份 {stamp}"
        code, out = run(["git", "commit", "-m", msg], src)
        if code != 0:
            print(f"❌ git commit 失败:\n{out}"); return 1
        print(f"✓ git commit: {msg}")

    if args.no_push:
        print("（--no-push，未推送）")
        return 0

    # 3) push（token 只存在于本次命令行 URL，不进 .git/config）
    #
    # ⚠️【2026-09-13 修·真实踩坑】必须带 `-c credential.helper= -c core.askpass=`：
    #   本机 git 配了 credential.helper=manager（git-credential-manager.exe），
    #   它会让 `git push <带token的URL>` **静默失败** —— 退出码 128、stdout/stderr
    #   **全空**，什么都不打印，看起来像"什么都没发生"。实测踩过：以为推送成功，
    #   实则远端一个字节没动。清空 helper 后同一命令立刻成功。
    #   注意：这跟"URL 里带 token"无关，带上 token 也照样被 helper 拦掉。
    host = PLATFORM_HOST[args.platform]
    if args.platform == "gitee":
        url = f"https://oauth2:{token}@{host}/{args.repo}.git"
    else:
        url = f"https://{token}@{host}/{args.repo}.git"
    code, out = run(["git", "-c", "credential.helper=", "-c", "core.askpass=",
                     "push", url, f"{local_branch}:{remote_branch}"], src)
    if code != 0:
        # 若远端非空（如 Gitee 仓库建时勾了 README），用 --force-with-lease 风险大；改为 pull --rebase 重试
        if "non-fast-forward" in out or "rejected" in out or "fetch first" in out:
            print("⚠ 远端非空，尝试 pull --rebase 合并后再 push ...")
            # 远端仓库默认空的话 pull 也会失败；这里给清晰提示
            print("   建议：到 Gitee 仓库页确认仓库完全为空（建仓时不要勾任何初始化）")
            print(f"   当前错误: {out.splitlines()[-1] if out else '(无)'}")
        elif not out.strip():
            # 退出码非 0 却没有任何输出 —— 本机 git 的典型"静默失败"特征
            print(f"❌ git push 失败（退出码 {code}，且**无任何输出**）。")
            print("   本机常见原因：credential.helper 拦截导致静默失败。")
            print("   已内置 `-c credential.helper= -c core.askpass=` 规避；若仍失败请检查：")
            print("     ① token 是否有效  ② 网络/代理  ③ 远端分支是否 protected")
        else:
            print(f"❌ git push 失败（退出码 {code}）:\n{out}")
        return 1
    print(f"✓ 已推送到 https://{host}/{args.repo}  ({local_branch} → {remote_branch})")
    print("=== 备份完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
