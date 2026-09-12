#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KuAi 一键发布更新工具  (publish_update.py)
================================================

【为什么做这个】
以前发新版要手动去 GitHub 建 Release、把 exe 拖进去、再改 version.json 并 push，
步骤又多又容易漏。这个工具把"发布更新"变成一件事：

    1) 用文件框选安装包 exe
    2) 填新版本号（如 1.2.0）+ 更新说明
    3) 点「发布更新」

工具会自动：
    A. 调用 GitHub API 创建 Release（标签 vX.Y.Z）并上传 exe
    B. 重写 version.json（version / url / notes / mandatory）
    C. git add + commit + push 到官网仓库

软件内的「检查更新」随后就能检测到新版并一键升级。

【使用前准备：拿一个 GitHub 个人令牌(PAT)】
1. 打开 https://github.com/settings/personal-access-tokens?type=beta
2. 点「Generate new token」（细粒度 fine-grained）
3. Repository access 选 Only select repositories → 选 yichenzi016-spec/KuAi
4. Permissions 加 Contents: Read and write
5. 生成后复制那一长串，第一次打开本工具时粘贴进去
   （token 只存到 ~/.workbuddy/publish_token，仓库外的本地文件，绝不进 git）

【怎么跑】
    python publish_update.py
有图形界面就点；没有（服务器/无桌面）就走命令行：
    python publish_update.py --exe 路径 --version 1.2.0 --notes "更新说明" --token ghp_xxx

注意：本文件只是开发者发布工具，不会影响线上网站内容。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import threading
import urllib.request
import urllib.parse
from pathlib import Path

# ----------------------------------------------------------------------------
# 配置（只需改这里或首次在界面里填）
# ----------------------------------------------------------------------------
DEFAULT_OWNER = "yichenzi016-spec"
DEFAULT_REPO = "KuAi"
# 本工具所在目录的上级 = 官网仓库根（version.json 在这里）
WEBSITE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "publish_config.json"

REPO_API = "https://api.github.com/repos/{owner}/{repo}"
UPLOAD_API = "https://uploads.github.com/repos/{owner}/{repo}/releases/{rid}/assets"

# ★ 仓库外的安全 token 存储（绝不要放进仓库内，尤其官网是 Public 仓库）
EXTERNAL_TOKEN_PATH = Path.home() / ".workbuddy" / "publish_token"


def read_external_token() -> str:
    """从仓库外的安全位置读取 PAT（不进 git、不进公开仓库）。"""
    try:
        return EXTERNAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def save_external_token(token: str) -> None:
    """把 PAT 写到仓库外的安全位置。"""
    try:
        EXTERNAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXTERNAL_TOKEN_PATH.write_text(token.strip(), encoding="utf-8")
    except Exception as e:
        print(f"[警告] 无法保存外部 token: {e}")


# ----------------------------------------------------------------------------
# 配置读写（注意：token 绝不写进仓库内的 config）
# ----------------------------------------------------------------------------
def load_config() -> dict:
    cfg = {
        "token": "",
        "owner": DEFAULT_OWNER,
        "repo": DEFAULT_REPO,
        "website_dir": str(WEBSITE_DIR),
        "last_exe": "",
        "last_version": "",
    }
    if CONFIG_PATH.is_file():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ----------------------------------------------------------------------------
# GitHub API 封装
# ----------------------------------------------------------------------------
def _api_call(method: str, url: str, token: str,
              data: dict | None = None,
              raw_bytes: bytes | None = None,
              content_type: str = "application/json") -> tuple[int, object]:
    """返回 (status_code, parsed_json_or_text)。"""
    ctx = ssl.create_default_context()
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "KuAi-Publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if raw_bytes is not None:
        body = raw_bytes
        headers["Content-Type"] = content_type
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # 网络层错误
        return -1, str(e)


def get_existing_release(token: str, owner: str, repo: str, tag: str) -> dict | None:
    """按 tag 查已存在的 release（用于幂等：已存在就复用）。"""
    status, data = _api_call(
        "GET", REPO_API.format(owner=owner, repo=repo) + f"/releases/tags/{tag}", token
    )
    if status == 200 and isinstance(data, dict):
        return data
    return None


def create_release(token: str, owner: str, repo: str, tag: str,
                  name: str, body: str) -> dict:
    """创建 release；若 tag 已存在则复用。返回 release 字典（含 id）。"""
    existing = get_existing_release(token, owner, repo, tag)
    if existing:
        return existing
    status, data = _api_call(
        "POST", REPO_API.format(owner=owner, repo=repo) + "/releases",
        token, data={
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": False,
        },
    )
    if status not in (200, 201) or not isinstance(data, dict):
        raise RuntimeError(f"创建 Release 失败 (HTTP {status}): {data}")
    return data


def upload_asset(token: str, owner: str, repo: str, release_id: int,
                file_path: Path, log) -> str:
    """上传安装包到 release；同名资产已存在则先删后传。返回浏览器下载地址。"""
    name = file_path.name
    # 删除已存在的同名资产（保证重发可覆盖）
    _, rel = _api_call(
        "GET", REPO_API.format(owner=owner, repo=repo) + f"/releases/{release_id}",
        token,
    )
    if isinstance(rel, dict):
        for asset in rel.get("assets", []):
            if asset.get("name") == name:
                _api_call("DELETE", asset["url"], token)
                log(f"  · 已删除旧资产 {name}")
                break
    size = file_path.stat().st_size
    log(f"  · 上传 {name} ({size/1024/1024:.1f} MB) …")
    with open(file_path, "rb") as fh:
        raw = fh.read()
    url = (UPLOAD_API.format(owner=owner, repo=repo, rid=release_id)
           + f"?name={urllib.parse.quote(name)}")
    status, data = _api_call(
        "POST", url, token, raw_bytes=raw,
        content_type="application/octet-stream",
    )
    if status not in (200, 201) or not isinstance(data, dict):
        raise RuntimeError(f"上传资产失败 (HTTP {status}): {data}")
    return data.get("browser_download_url", "")


# ----------------------------------------------------------------------------
# Gitee 国内镜像（免代理检查更新 + 下载的主用源）
# ----------------------------------------------------------------------------
# 账号安全等级达标并「公开」后，国内用户不开代理即可：
#   · 读取  https://gitee.com/kuai061102/kuai-release/raw/master/version.json
#   · 下载  https://gitee.com/kuai061102/kuai-release/releases/download/vX.Y.Z/xxx.exe
# 本段把「发布更新」自动同步到这个公开仓库，做到「每次都放在那」。
GITEE_OWNER = "kuai061102"
GITEE_REPO = "kuai-release"
GITEE_API = "https://gitee.com/api/v5"
GITEE_TOKEN_PATH = Path.home() / ".workbuddy" / "gitee_token"


def read_gitee_token() -> str:
    try:
        return GITEE_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _gitee_req(method: str, path: str, token: str,
               data: dict | None = None,
               raw_bytes: bytes | None = None,
               content_type: str = "application/json",
               timeout: int = 60):
    url = GITEE_API + path + (f"?access_token={token}" if "?" not in path else f"&access_token={token}")
    body = None
    headers = {"Accept": "application/json"}
    if raw_bytes is not None:
        body = raw_bytes
        headers["Content-Type"] = content_type
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, str(e)


def gitee_ensure_release(token: str, tag: str, name: str, body: str) -> dict | None:
    """按 tag 取已存在的 release；没有则建一个（target_commitish=master）。"""
    st, data = _gitee_req("GET",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/tags/{tag}", token)
    if st == 200 and isinstance(data, dict):
        return data
    st2, data2 = _gitee_req("POST", f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases", token,
        data={"tag_name": tag, "target_commitish": "master", "name": name,
              "body": body, "draft": False, "prerelease": False})
    if st2 in (200, 201) and isinstance(data2, dict):
        return data2
    return None


def gitee_upload_asset(token: str, release_id: int, file_path: Path, log) -> str:
    """上传安装包到 Gitee release（同名先删后传），返回浏览器下载地址。"""
    name = file_path.name
    # Gitee 的 releases/{id} 返回的 assets 只有 name / browser_download_url，**不含 id**
    # （2026-09-12 实测），所以不能用它来精确定位/删除同名旧附件。
    # 正确做法：用 releases/{id}/attach_files 列表（这个端点**带 id**），
    # 删除所有同名旧附件后再上传，保证 release 里只有一份当前安装包。
    st, atts = _gitee_req("GET",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{release_id}/attach_files", token)
    if isinstance(atts, list):
        for att in atts:
            if (isinstance(att, dict) and att.get("name") == name and att.get("id")):
                _gitee_req("DELETE",
                    f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{release_id}/attach_files/{att['id']}",
                    token)
                log(f"  · [Gitee] 已删除旧附件 {name} (id={att['id']})")
    raw = file_path.read_bytes()
    log(f"  · [Gitee] 上传 {name} ({len(raw)/1024/1024:.1f} MB) …")
    boundary = "----WebKitFormBoundaryKuAiGiteeUp"
    bnb = b"--" + boundary.encode() + b"\r\n"
    bnb += b'Content-Disposition: form-data; name="file"; filename="' + name.encode() + b'"\r\n'
    bnb += b"Content-Type: application/octet-stream\r\n\r\n"
    bnb += raw + b"\r\n"
    bnb += b"--" + boundary.encode() + b"--\r\n"
    st2, data2 = _gitee_req("POST",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{release_id}/attach_files", token,
        raw_bytes=bnb, content_type=f"multipart/form-data; boundary={boundary}", timeout=560)
    if st2 in (200, 201) and isinstance(data2, dict):
        return data2.get("browser_download_url", "")
    log(f"  · ⚠️ [Gitee] 上传失败 (HTTP {st2}): {str(data2)[:200]}")
    return ""


def gitee_push_version_json(token: str, payload: dict, log) -> None:
    """把 version.json 推到 kuai-release 的 master 分支（不存在则建，存在则更新）。

    2026-09-12 修：原先只在 HTTP 422 且 message 以 "already" 开头时才走 sha 更新分支，
    但 Gitee 实际返回的是 **400 + {"message":"文件名已存在"}**，导致重发时 version.json
    永远更新不了。改为「先 GET 取 sha，有 sha 就 PUT，无 sha 才 POST」，不再依赖状态码。
    """
    import base64
    b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")).decode()
    msg = f"发布 v{payload.get('version','')}：更新 version.json"
    sha = None
    stg, gd = _gitee_req("GET",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/contents/version.json?ref=master", token)
    if stg == 200 and isinstance(gd, dict):
        sha = gd.get("sha")
    if sha:
        st, data = _gitee_req("PUT",
            f"/repos/{GITEE_OWNER}/{GITEE_REPO}/contents/version.json", token,
            data={"content": b64, "message": msg, "branch": "master", "sha": sha})
    else:
        st, data = _gitee_req("POST",
            f"/repos/{GITEE_OWNER}/{GITEE_REPO}/contents/version.json", token,
            data={"content": b64, "message": msg, "branch": "master"})
    if st in (200, 201):
        log("  · [Gitee] version.json 已更新")
    else:
        log(f"  · ⚠️ [Gitee] version.json 推送失败 (HTTP {st}): {str(data)[:200]}")


def gitee_publish(token: str, exe_path: Path, tag: str, version: str,
                  notes: str, log) -> str:
    """确保 Gitee 公开仓库有对应 release 并上传安装包，返回下载地址（失败返回空串）。"""
    if not token:
        log("  · ⚠️ [Gitee] 缺少 Gitee 令牌，跳过国内镜像（仓库外 ~/.workbuddy/gitee_token）")
        return ""
    rel = gitee_ensure_release(token, tag, f"KuAi视频助手 v{version}", notes)
    if not rel or not rel.get("id"):
        log("  · ⚠️ [Gitee] 无法创建/获取 Release（仓库是否已设为「公开」？）")
        return ""
    return gitee_upload_asset(token, rel["id"], exe_path, log)


# ----------------------------------------------------------------------------
# version.json + git
# ----------------------------------------------------------------------------
def update_version_json(website_dir: Path, version: str, url: str,
                        notes: str, mandatory: bool, url_mirror: str = "") -> None:
    """重写 version.json。

    注意：保留原有文件里本函数不负责的额外字段（如 min_supported），
    否则每次发布都会把它们静默删掉（2026-09-10 修）。
    url_mirror 是海外/代理兜底下载地址（GitHub Releases）。
    """
    path = website_dir / "version.json"
    extra: dict = {}
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                extra = {k: v for k, v in old.items()
                         if k not in ("version", "url", "notes", "mandatory", "url_mirror")}
        except Exception:
            extra = {}
    payload = {
        "version": version,
        "url": url,
        "url_mirror": url_mirror,
        "notes": notes,
        "mandatory": bool(mandatory),
        **extra,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def git_commit_push(website_dir: Path, version: str, log) -> None:
    log("  · git add / commit / push …")
    subprocess.run(["git", "pull", "--no-edit"], cwd=str(website_dir),
                   check=False, capture_output=True)
    subprocess.run(["git", "add", "version.json"], cwd=str(website_dir),
                   check=False, capture_output=True)
    subprocess.run(["git", "commit", "-m",
                    f"发布 v{version}：更新 version.json"], cwd=str(website_dir),
                   check=False, capture_output=True)
    r = subprocess.run(["git", "push", "origin", "main"], cwd=str(website_dir),
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  · ⚠️ push 失败：{r.stderr.strip()[-300:]}")
    else:
        log("  · push 成功")


# ----------------------------------------------------------------------------
# 主发布流程
# ----------------------------------------------------------------------------
def do_publish(token: str, owner: str, repo: str, website_dir: Path,
               exe_path: Path, version: str, notes: str, mandatory: bool,
               log) -> bool:
    try:
        if not token:
            raise ValueError("缺少 GitHub 令牌(PAT)，请在界面里粘贴一次。")
        if not exe_path.is_file():
            raise FileNotFoundError(f"找不到安装包：{exe_path}")
        if not _valid_version(version):
            raise ValueError("版本号格式应为 X.Y.Z，例如 1.2.0")
        tag = f"v{version}"
        log(f"[1/5] GitHub 创建 Release {tag} …")
        rel = create_release(token, owner, repo, tag,
                             f"KuAi视频助手 v{version}", notes)
        release_id = rel.get("id")
        if not release_id:
            raise RuntimeError("Release 缺少 id")
        log(f"[2/5] GitHub 上传安装包 …")
        gh_url = upload_asset(token, owner, repo, release_id, exe_path, log)
        if not gh_url:
            # 兜底：用约定 URL（GitHub Releases 下载地址规律）
            gh_url = (f"https://github.com/{owner}/{repo}/releases/"
                      f"download/{tag}/{urllib.parse.quote(exe_path.name)}")
        log(f"[3/5] Gitee 国内镜像：建 Release + 上传安装包 …")
        gitee_token = read_gitee_token()
        gitee_url = gitee_publish(gitee_token, exe_path, tag, version, notes, log)
        # version.json 主用地址 = Gitee（国内免代理）；兜底 = GitHub（海外/代理）
        primary_url = gitee_url or gh_url
        log(f"[4/5] 更新 version.json → {version}（url=Gitee, url_mirror=GitHub）")
        update_version_json(website_dir, version, primary_url, notes, mandatory, gh_url)
        if gitee_url:
            gitee_push_version_json(gitee_token, {
                "version": version,
                "url": gitee_url,
                "url_mirror": gh_url,
                "notes": notes,
                "mandatory": bool(mandatory),
                "min_supported": version,
            }, log)
        else:
            log("  · ⚠️ 未拿到 Gitee 下载地址，国内镜像未生效"
                "（请确认 Gitee 仓库 kuai-release 已设为「公开」且令牌有效）")
        log("[5/5] 推送官网仓库 …")
        git_commit_push(website_dir, version, log)
        log("✅ 发布完成！软件内「检查更新」稍后即可检测到 v" + version)
        log("   国内下载（主用）：" + primary_url)
        log("   海外下载（兜底）：" + gh_url)
        return True
    except Exception as e:
        log(f"❌ 发布失败：{e}")
        return False


def _valid_version(v: str) -> bool:
    parts = v.strip().lstrip("vV").split(".")
    if len(parts) != 3:
        return False
    return all(p.isdigit() for p in parts)


# ----------------------------------------------------------------------------
# GUI（有桌面就显示，否则回退命令行）
# ----------------------------------------------------------------------------
def run_gui(cfg: dict) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title("KuAi 一键发布更新")
    root.geometry("620x560")

    def browse_exe():
        p = filedialog.askopenfilename(
            title="选择安装包",
            initialdir=os.path.dirname(cfg.get("last_exe") or ""),
            filetypes=[("安装包", "*.exe"), ("所有文件", "*.*")])
        if p:
            exe_var.set(p)
            cfg["last_exe"] = p

    def browse_dir():
        d = filedialog.askdirectory(title="选择官网仓库目录",
                                    initialdir=cfg.get("website_dir"))
        if d:
            dir_var.set(d)
            cfg["website_dir"] = d

    frm = tk.Frame(root, padx=14, pady=12)
    frm.pack(fill="both", expand=True)

    tk.Label(frm, text="GitHub 令牌(PAT)：", anchor="w").pack(fill="x")
    token_var = tk.StringVar(value=read_external_token() or cfg.get("token", ""))
    tk.Entry(frm, textvariable=token_var, show="*", width=70).pack(fill="x")

    tk.Label(frm, text="官网仓库目录：", anchor="w").pack(fill="x", pady=(8, 0))
    dir_var = tk.StringVar(value=cfg.get("website_dir", str(WEBSITE_DIR)))
    row = tk.Frame(frm)
    row.pack(fill="x")
    tk.Entry(row, textvariable=dir_var, width=58).pack(side="left", fill="x", expand=True)
    tk.Button(row, text="浏览", command=browse_dir).pack(side="left", padx=4)

    tk.Label(frm, text="安装包 exe：", anchor="w").pack(fill="x", pady=(8, 0))
    exe_var = tk.StringVar(value=cfg.get("last_exe", ""))
    row = tk.Frame(frm)
    row.pack(fill="x")
    tk.Entry(row, textvariable=exe_var, width=58).pack(side="left", fill="x", expand=True)
    tk.Button(row, text="浏览", command=browse_exe).pack(side="left", padx=4)

    tk.Label(frm, text="新版本号（X.Y.Z）：", anchor="w").pack(fill="x", pady=(8, 0))
    ver_var = tk.StringVar(value=cfg.get("last_version", ""))
    tk.Entry(frm, textvariable=ver_var, width=30).pack(fill="x")

    tk.Label(frm, text="更新说明（可多行）：", anchor="w").pack(fill="x", pady=(8, 0))
    notes_txt = scrolledtext.ScrolledText(frm, height=5)
    notes_txt.pack(fill="x")

    manda_var = tk.BooleanVar(value=False)
    tk.Checkbutton(frm, text="强制更新（用户必须升，否则提示）",
                   variable=manda_var).pack(anchor="w", pady=(4, 0))

    log_box = scrolledtext.ScrolledText(frm, height=9, bg="#111", fg="#0f0")
    log_box.pack(fill="both", expand=True, pady=(8, 0))

    def log(msg):
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        root.update_idletasks()

    def on_publish():
        tok = token_var.get().strip()
        # 把 token 存到仓库外安全位置，绝不写进仓库内的 config
        if tok:
            save_external_token(tok)
        cfg["website_dir"] = dir_var.get().strip()
        cfg["last_exe"] = exe_var.get().strip()
        cfg["last_version"] = ver_var.get().strip()
        save_config(cfg)
        btn.config(state="disabled")
        try:
            do_publish(
                # 必须用界面上刚输入的 tok（首次使用时 cfg["token"] 恒为空，
                # 用 cfg["token"] 会导致「粘贴了 PAT 仍报缺少令牌」，2026-09-10 修）
                token=tok, owner=cfg.get("owner", DEFAULT_OWNER),
                repo=cfg.get("repo", DEFAULT_REPO),
                website_dir=Path(cfg["website_dir"]),
                exe_path=Path(exe_var.get().strip()),
                version=ver_var.get().strip(),
                notes=notes_txt.get("1.0", "end").strip(),
                mandatory=manda_var.get(), log=log)
        finally:
            btn.config(state="normal")

    btn = tk.Button(frm, text="🚀 发布更新", bg="#2a7", fg="white",
                    height=2, command=on_publish)
    btn.pack(fill="x", pady=(8, 0))
    tk.Label(frm, text="首次使用需粘贴 PAT（仅存本地仓库外安全位置，绝不会进 git）。",
             fg="#888").pack()

    root.mainloop()


def run_cli() -> None:
    ap = argparse.ArgumentParser(description="KuAi 一键发布更新（命令行）")
    ap.add_argument("--exe", required=True, help="安装包 exe 路径")
    ap.add_argument("--version", required=True, help="新版本号 X.Y.Z")
    ap.add_argument("--notes", default="", help="更新说明")
    ap.add_argument("--token", default="", help="GitHub PAT（或用环境变量 GH_TOKEN）")
    ap.add_argument("--mandatory", action="store_true", help="强制更新")
    ap.add_argument("--owner", default=DEFAULT_OWNER)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--website-dir", default=str(WEBSITE_DIR))
    args = ap.parse_args()

    cfg = load_config()
    token = (args.token or os.environ.get("GH_TOKEN")
             or read_external_token() or cfg.get("token", ""))
    ok = do_publish(
        token=token, owner=args.owner, repo=args.repo,
        website_dir=Path(args.website_dir), exe_path=Path(args.exe),
        version=args.version, notes=args.notes, mandatory=args.mandatory,
        log=print)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli()
    else:
        try:
            run_gui(load_config())
        except Exception:
            print("图形界面不可用，回退命令行。用法：")
            print('python publish_update.py --exe 路径 --version 1.2.0 '
                  '--notes "说明" --token ghp_xxx')
            sys.exit(2)
