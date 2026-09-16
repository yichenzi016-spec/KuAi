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
import hashlib
import json
import os
import re
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


def compute_sha256(file_path: Path) -> str:
    """计算安装包 SHA256（大写十六进制）。写入 version.json 供客户端下载后校验。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def create_release(token: str, owner: str, repo: str, tag: str,
                  name: str, body: str) -> dict:
    """创建 release；若 tag 已存在则复用**并刷新说明**。返回 release 字典（含 id）。

    【2026-09-15 修 M1】重发同一版本号时（tag 已存在），旧代码直接复用、不更新 body，
    导致 Release 页面的更新说明永远停留在首次创建时的文案（Gitee/GitHub 均实测到：
    v1.0.1 的说明自 09-12 起我们重发两次都一个字没变）。现在对已存在的 Release 执行
    PATCH，保证页面说明与本次发布一致。
    """
    existing = get_existing_release(token, owner, repo, tag)
    if existing:
        rid = existing.get("id")
        if rid:
            st, patched = _api_call(
                "PATCH", REPO_API.format(owner=owner, repo=repo) + f"/releases/{rid}",
                token, data={"name": name, "body": body},
            )
            if st == 200 and isinstance(patched, dict):
                return patched
            raise RuntimeError(
                f"刷新 GitHub Release 说明失败 (HTTP {st}): {str(patched)[:200]}")
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
                # 【2026-09-15 修 H2】删除结果必须校验：旧代码丢弃返回值并无条件打印
                # "已删除旧资产"。GitHub 同名资产会以 "xxx-1.exe" 追加而非覆盖，删除
                # 静默失败会让 Release 里出现重复资产。失败即中止，不留下不确定状态。
                dst, ddata = _api_call("DELETE", asset["url"], token)
                if dst not in (200, 204):
                    raise RuntimeError(
                        f"删除 GitHub 旧资产失败 (HTTP {dst})：{name} — {str(ddata)[:200]}"
                    )
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


class GiteePublishError(RuntimeError):
    """Gitee 主源发布失败。

    【2026-09-15 修 H1】Gitee 是国内用户的**主用下载源**（客户端 UPDATE_CHECK_URLS
    第一个就是它）。它失败时绝不能静默降级成 GitHub 地址后仍报「✅ 发布完成」——
    国内多数用户打不开 GitHub，那等于新版本事实上没有可用下载。因此有令牌但发布
    失败一律抛此异常，由 do_publish 显式中止并报失败。

    「未配置令牌」不算失败，那是明确的降级模式（只发 GitHub），由 do_publish 单独处理。
    """


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


def gitee_ensure_release(token: str, tag: str, name: str, body: str) -> dict:
    """按 tag 取已存在的 release；没有则建一个（target_commitish=master）。

    【2026-09-15 修 M1】tag 已存在时同步刷新 Release 页说明（PATCH），
    避免重发同版本号后页面说明陈旧。失败一律抛 GiteePublishError。
    """
    st, data = _gitee_req("GET",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/tags/{tag}", token)
    if st == 200 and isinstance(data, dict):
        rid = data.get("id")
        if rid:
            # 【实测踩坑】Gitee 的 PATCH /releases/{id} 即便只改 name/body 也**强制要求
            # tag_name** —— 少了它直接 400 {"messages":["tag_name is missing"]}。
            # 旧实现没带 tag_name，PATCH 一直 400，而返回值又没检查，于是
            # 「说明已刷新」这件事从未真正发生过（09-12 至 09-15 重发多次都没更新）。
            stp, patched = _gitee_req("PATCH",
                f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{rid}", token,
                data={"tag_name": tag, "name": name, "body": body})
            if stp in (200, 201) and isinstance(patched, dict):
                return patched
            raise GiteePublishError(
                f"刷新 Release 说明失败 (HTTP {stp}): {str(patched)[:200]}")
        return data
    st2, data2 = _gitee_req("POST", f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases", token,
        data={"tag_name": tag, "target_commitish": "master", "name": name,
              "body": body, "draft": False, "prerelease": False})
    if st2 in (200, 201) and isinstance(data2, dict):
        return data2
    raise GiteePublishError(f"创建 Release 失败 (HTTP {st2}): {str(data2)[:200]}")


def gitee_upload_asset(token: str, release_id: int, file_path: Path, log) -> str:
    """上传安装包到 Gitee release（同名先删后传），返回浏览器下载地址。

    失败一律抛 GiteePublishError（含旧附件删除失败 / 上传失败），绝不静默返回空串。
    """
    name = file_path.name
    # Gitee 的 releases/{id} 返回的 assets 只有 name / browser_download_url，**不含 id**
    # （2026-09-12 实测），所以不能用它来精确定位/删除同名旧附件。
    # 正确做法：用 releases/{id}/attach_files 列表（这个端点**带 id**），
    # 删除所有同名旧附件后再上传，保证 release 里只有一份当前安装包。
    st, atts = _gitee_req("GET",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{release_id}/attach_files", token)
    if not isinstance(atts, list):
        raise GiteePublishError(
            f"无法列出 Gitee 附件 (HTTP {st})：{str(atts)[:200]}"
            "（无法保证同名附件不重复，已中止）"
        )
    for att in atts:
        if isinstance(att, dict) and att.get("name") == name and att.get("id"):
            # 【2026-09-15 修 H2】旧代码丢弃 DELETE 返回值并无条件打印"已删除旧附件"。
            # Gitee 同名附件是**追加**（代码注释自己也写明），删除静默失败会造成
            # Release 里出现两个同名 exe，下载链接可能返回旧包。现在删除失败即中止。
            dst, dmsg = _gitee_req("DELETE",
                f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{release_id}/attach_files/{att['id']}",
                token)
            if dst not in (200, 204):
                raise GiteePublishError(
                    f"删除旧附件失败 (HTTP {dst})：{name} (id={att['id']}) — {str(dmsg)[:200]}"
                )
            log(f"  · [Gitee] 已删除旧附件 {name} (id={att['id']})")
    # 复核：删除后不得再存在同名附件（防止「删除静默无效」）
    st_v, atts_v = _gitee_req("GET",
        f"/repos/{GITEE_OWNER}/{GITEE_REPO}/releases/{release_id}/attach_files", token)
    if isinstance(atts_v, list) and any(
            isinstance(a, dict) and a.get("name") == name for a in atts_v):
        raise GiteePublishError(
            f"旧附件 {name} 删除后仍存在于 Gitee Release（删除未生效），已中止以避免重复附件"
        )
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
        url = str(data2.get("browser_download_url", "")).strip()
        if url:
            return url
    raise GiteePublishError(f"上传安装包失败 (HTTP {st2}): {str(data2)[:200]}")


def gitee_push_version_json(token: str, payload: dict, log) -> None:
    """把 version.json 推到 kuai-release 的 master 分支（不存在则建，存在则更新）。

    2026-09-12 修：原先只在 HTTP 422 且 message 以 "already" 开头时才走 sha 更新分支，
    但 Gitee 实际返回的是 **400 + {"message":"文件名已存在"}**，导致重发时 version.json
    永远更新不了。改为「先 GET 取 sha，有 sha 就 PUT，无 sha 才 POST」，不再依赖状态码。

    2026-09-15 修 M2：payload 由调用方一次性构造（与本地 version.json 完全同一份 dict），
    序列化与本地走同一个 `_serialize_version_json`，从根本上杜绝「双端字段不一致」
    （旧代码本地保留旧 min_supported、Gitee 硬写新版本号，只是恰好相等才没暴露）。
    失败不再仅打日志：抛 GiteePublishError，避免「国内主源的公告牌没更新」却报成功。
    """
    import base64
    b64 = base64.b64encode(_serialize_version_json(payload).encode("utf-8")).decode()
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
        raise GiteePublishError(
            f"version.json 推送失败 (HTTP {st}): {str(data)[:200]}"
            "（国内用户的更新公告牌未更新，已中止）"
        )


def gitee_publish(token: str, exe_path: Path, tag: str, version: str,
                  notes: str, log) -> str:
    """确保 Gitee 公开仓库有对应 release 并上传安装包，返回下载地址。

    返回空串 = 「未启用」：没有配置 Gitee 令牌，降级为只发 GitHub。
    失败（有令牌但建 Release / 删附件 / 上传 / 推 version.json 出问题）
    一律抛 GiteePublishError，由 do_publish 显式中止。
    """
    if not token:
        log("  · ⚠️ [Gitee] 缺少 Gitee 令牌，跳过国内镜像（仓库外 ~/.workbuddy/gitee_token）")
        return ""
    rel = gitee_ensure_release(token, tag, f"KuAi视频助手 v{version}", notes)
    if not rel or not rel.get("id"):
        raise GiteePublishError("无法创建/获取 Release（仓库是否已设为「公开」？）")
    return gitee_upload_asset(token, rel["id"], exe_path, log)


# ----------------------------------------------------------------------------
# version.json + git
# ----------------------------------------------------------------------------
# 由本工具统一管理的字段；其余字段（用户/历史遗留的扩展键）原样保留。
_MANAGED_VERSION_KEYS = ("version", "url", "url_mirror", "notes", "mandatory",
                         "sha256", "size", "min_supported")


def _serialize_version_json(payload: dict) -> str:
    """version.json 的唯一序列化入口（本地写盘与 Gitee 推送共用，保证双端逐字节一致）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def update_version_json(website_dir: Path, version: str, url: str,
                        notes: str, mandatory: bool, url_mirror: str = "",
                        sha256: str = "", min_supported: str = "",
                        size: int = 0) -> dict:
    """重写本地 version.json 并**返回**实际写入的 payload（供 Gitee 侧复用同一份）。

    注意：保留原有文件里本工具不负责的额外字段，否则每次发布都会把它们静默删掉
    （2026-09-10 修）。url_mirror 是海外/代理兜底下载地址（GitHub Releases）。
    sha256：安装包摘要，客户端下载后可校验完整性（2026-09-15 新增 M5）。
    min_supported：未显式传入时沿用原文件里的值（不擅自改成"新版本号"）。

    【2026-09-15 修 M2】返回值为「本次实际写入的完整 payload」。调用方把它原样交给
    Gitee 写入端，双端就是同一份 dict 的同一份序列化结果，结构上不可能再分叉
    （旧代码 Gitee 侧硬写 min_supported=新版本号、本地侧保留旧值）。
    """
    path = website_dir / "version.json"
    old: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                old = loaded
        except Exception:
            old = {}
    extra = {k: v for k, v in old.items() if k not in _MANAGED_VERSION_KEYS}
    payload = {
        "version": version,
        "url": url,
        "url_mirror": url_mirror,
        "notes": notes,
        "mandatory": bool(mandatory),
    }
    if sha256:
        payload["sha256"] = sha256
    if size:
        payload["size"] = int(size)
    if not min_supported:
        # 沿用原文件的 min_supported（若有）——它是「最低可支持版本」的地板，
        # 不应随每次发版被抬到当前版本（否则一旦客户端实现该字段就会强制升级）。
        min_supported = str(old.get("min_supported", "") or "").strip()
    if min_supported:
        payload["min_supported"] = min_supported
    payload.update(extra)
    path.write_text(_serialize_version_json(payload), encoding="utf-8")
    return payload


# ----------------------------------------------------------------------------
# 官网页面的版本号同步（2026-09-15 新增，修 H4）
# ----------------------------------------------------------------------------
# 背景：官网 index/guide/features 共 5 处下载按钮写死 `.../download/v1.0.1/...Setup-1.0.1.exe`
# ，而本工具原先只更新 version.json、**完全不碰 HTML**。结果是每发一版都要人工改 5 处，
# 一旦漏改，官网就挂着一个不存在的旧包地址（用户点下载直接 404）。
#
# 现在：assets/script.js 在前端按 [data-download] / [data-version] 动态改写（首选读
# Gitee raw 的 version.json，页面内容滞后也不影响）；本函数则在**发布时**把这些静态
# 兜底值也一并改成新版本 —— 前端脚本被拦或执行失败时，页面仍然是正确的。
#
# 只改写「明确标记」的位置，绝不全局替换版本号：
#   · 下载 URL 里的 vX.Y.Z 与 Setup-X.Y.Z.exe（无歧义）
#   · <a data-download ...> 标签内的 VX.Y.Z
#   · 带 data-version 标记的元素内的 VX.Y.Z
#   · <title> 内的 VX.Y.Z
# 版本历史（如 <div class="log-item"> 里「2026-09 · V1.0.1 首个正式版」）**不动**。
_DL_URL_RE = re.compile(
    r"(releases/download/)v\d+\.\d+\.\d+(/VideoPromptAssistant-Setup-)\d+\.\d+\.\d+(\.exe)")
_ANCHOR_RE = re.compile(r"<a\b[^>]*\bdata-download\b[^>]*>.*?</a>", re.S | re.I)
_DV_TAG_RE = re.compile(
    r"(<[a-zA-Z][^>]*\bdata-version\b[^>]*>)(.*?)(</[a-zA-Z]+>)", re.S)
_TITLE_RE = re.compile(r"(<title>)(.*?)(</title>)", re.S | re.I)
_VER_IN_TEXT_RE = re.compile(r"V\d+\.\d+\.\d+")


def sync_page_download_links(website_dir: Path, version: str, log) -> int:
    """把官网页面里写死的下载地址/版本号同步到 version。返回被修改的文件数。"""
    changed = 0
    for name in ("index.html", "guide.html", "features.html"):
        page = website_dir / name
        if not page.is_file():
            continue
        try:
            html = page.read_text(encoding="utf-8")
        except OSError:
            continue
        new = _DL_URL_RE.sub(
            lambda m: f"{m.group(1)}v{version}{m.group(2)}{version}{m.group(3)}", html)
        new = _ANCHOR_RE.sub(
            lambda m: _VER_IN_TEXT_RE.sub(f"V{version}", m.group(0)), new)
        new = _DV_TAG_RE.sub(
            lambda m: m.group(1) + _VER_IN_TEXT_RE.sub(f"V{version}", m.group(2)) + m.group(3),
            new)
        new = _TITLE_RE.sub(
            lambda m: m.group(1) + _VER_IN_TEXT_RE.sub(f"V{version}", m.group(2)) + m.group(3),
            new)
        if new != html:
            page.write_text(new, encoding="utf-8")
            changed += 1
            log(f"  · {name} 下载地址/版本号已同步为 V{version}")
    return changed


# 本机 git 的 credential.helper=manager 会让 pull/push **静默失败**
# ——退出码 128、stdout/stderr **全空**，日志里只留下一句空洞的「push 失败：」。
# 2026-09-13 实测踩过：以为发布成功，实则远端一个字节没动。清空 helper + askpass 即恢复。
_GIT_SAFE = ["-c", "credential.helper=", "-c", "core.askpass="]


def _git(website_dir: Path, args, timeout: int = 300, env: dict | None = None):
    """跑 git 命令，返回 (rc, 合并输出)。按 UTF-8 解码字节，避免 GBK 控制台乱码。"""
    try:
        p = subprocess.run(["git"] + args, cwd=str(website_dir),
                           capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT >{timeout}s"
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, (out + err).strip()


def _git_config_env(token: str) -> dict:
    """把 GitHub PAT / 代理通过 **环境变量** 注入 git 配置，避免出现在命令行。

    【2026-09-15 修 L7】旧做法用
        -c url."https://<token>@github.com/".insteadOf="https://github.com/"
    把令牌拼进 argv —— Windows 上任何同用户进程都能通过命令行读取到该令牌。
    改用 Git 官方支持的 GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n
    环境变量形式传同样的配置（Git ≥ 2.31；本机实测 2.52），令牌不再出现在命令行。

    为什么仍需 insteadOf：2026-09-13 实测 `credential.helper=` 会禁用凭据导致 push
    静默失败（exit 128、输出全空）。把 PAT 嵌进 URL 才能绕开凭据管理器。
    代理同样注入，避免读到仓库 .git/config 里陈旧的 localhost:1080。
    """
    env = dict(os.environ)
    entries: list[tuple[str, str]] = []
    if token:
        entries.append((f"url.https://{token}@github.com/.insteadOf",
                        "https://github.com/"))
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        entries.append(("http.proxy", proxy))
        entries.append(("https.proxy", proxy))
    # 叠加到调用方可能已有的 GIT_CONFIG_* 之上（避免覆盖丢失）
    base = 0
    try:
        base = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        base = 0
    for i, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{base + i}"] = key
        env[f"GIT_CONFIG_VALUE_{base + i}"] = value
    env["GIT_CONFIG_COUNT"] = str(base + len(entries))
    return env


def git_commit_push(website_dir: Path, version: str, log,
                    token: str = "") -> bool:
    """提交并推送官网仓库。返回是否**完整成功**（add/commit/pull/push 全部 OK）。

    令牌与代理通过环境变量注入（见 _git_config_env），不出现在命令行。
    """
    log("  · git add / commit / pull --rebase / push …")
    env = _git_config_env(token)

    # 1) 先提交本地改动，再 rebase 远端。
    #    顺序不能反：若先 pull 后 commit，此刻 version.json 已是刚被重写过的**脏文件**，
    #    `pull --rebase` 会因「cannot pull with rebase: You have unstaged changes」直接拒绝。
    #
    #    【2026-09-15 修】原实现只 `add version.json`，于是任何**其它**网站改动
    #    （如 index/guide/features.html 的下载按钮、assets/script.js）会一直处于
    #    「已修改未暂存」状态 —— 既不会被提交，又会让 `pull --rebase` 直接失败
    #    （实测报 "cannot pull with rebase: You have unstaged changes"），
    #    整个官网推送静默卡死。官网是专用仓库，改为 `add -A` 把全部改动一起提交。
    rc, out = _git(website_dir, ["add", "-A"], env=env)
    if rc != 0:
        log(f"  · ⚠️ git add 失败：{out[-300:]}" if out
            else f"  · ⚠️ git add 失败（退出码 {rc}，无任何输出）")
        return False

    rc, out = _git(website_dir, ["commit", "-m", f"发布 v{version}：更新 version.json"],
                   env=env)
    if rc != 0 and "nothing to commit" not in out:
        log(f"  · ⚠️ git commit 失败：{out[-300:]}" if out
            else f"  · ⚠️ git commit 失败（退出码 {rc}，无任何输出）")
        return False

    # 2) 与远端对齐。GitHub Action 机器人会并发提交 "chore: sync download count"，
    #    不 rebase 会因 "fetch first" 被拒（2026-09-12 真实发生）。
    #    加 --autostash 兜底：万一步骤 1 之后仍有未暂存改动，也不会再让 rebase 中止。
    rc, out = _git(website_dir, _GIT_SAFE + ["pull", "--rebase", "--autostash", "--no-edit"],
                   env=env)
    if rc != 0:
        log(f"  · ⚠️ git pull --rebase 失败：{out[-300:]}" if out
            else f"  · ⚠️ git pull --rebase 失败（退出码 {rc}，无任何输出）")
        # rebase 半途失败会留下 .git/rebase-merge，后续所有 git 操作都会报错，
        # 必须显式 abort 复位，否则仓库脏住。
        _git(website_dir, ["rebase", "--abort"], env=env)

    # 3) 推送
    rc, out = _git(website_dir, _GIT_SAFE + ["push", "origin", "main"], env=env)
    if rc != 0:
        if not out:
            log(f"  · ⚠️ push 失败（退出码 {rc}，且**无任何输出**）。"
                "该特征通常是 credential.helper 拦截或网络/代理问题；"
                "请在官网目录手动执行  git push origin main  查看真实原因。")
        else:
            log(f"  · ⚠️ push 失败：{out[-300:]}")
        return False
    log("  · push 成功")
    return True


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

        # 安装包摘要：写入 version.json，客户端下载后可校验完整性（修 M5）。
        sha256 = compute_sha256(exe_path)
        exe_size = exe_path.stat().st_size
        log(f"  · 安装包 {exe_path.name}：{exe_size} 字节，SHA256={sha256}")

        log(f"[1/5] GitHub 创建 Release {tag} …")
        rel = create_release(token, owner, repo, tag,
                             f"KuAi视频助手 v{version}", notes)
        release_id = rel.get("id")
        if not release_id:
            raise RuntimeError("Release 缺少 id")
        log(f"[2/5] GitHub 上传安装包 …")
        gh_url = upload_asset(token, owner, repo, release_id, exe_path, log)
        gh_uploaded = bool(gh_url)
        if not gh_url:
            # 兜底：用约定 URL（GitHub Releases 下载地址规律）
            gh_url = (f"https://github.com/{owner}/{repo}/releases/"
                      f"download/{tag}/{urllib.parse.quote(exe_path.name)}")
            log("  · ⚠️ GitHub 未返回下载地址，改用约定 URL 兜底（上传可能未成功）")

        log(f"[3/5] Gitee 国内镜像：建 Release + 上传安装包 …")
        gitee_token = read_gitee_token()
        gitee_degraded = not bool(gitee_token)   # 未配置令牌 = 明确的降级模式
        if gitee_degraded:
            log("  · ⚠️ [Gitee] 未配置令牌 → 降级为「仅 GitHub」发布；"
                "国内未开代理的用户可能无法下载。")
            gitee_url = ""
        else:
            # 【2026-09-15 修 H1】有令牌却失败 = 国内主源不可用，必须显式中止，
            # 绝不能退回 GitHub 地址后仍报「发布完成」（国内用户打不开 GitHub）。
            gitee_url = gitee_publish(gitee_token, exe_path, tag, version, notes, log)

        # version.json 主用地址 = Gitee（国内免代理）；兜底 = GitHub（海外/代理）
        primary_url = gitee_url or gh_url
        log(f"[4/5] 更新 version.json → {version}（url=Gitee, url_mirror=GitHub）")
        # 【2026-09-15 修 M2】本地写盘与 Gitee 推送使用同一份 payload（update_version_json
        # 的返回值），双端字段不可能再分叉（旧代码 Gitee 侧硬写 min_supported=新版本号）。
        payload = update_version_json(website_dir, version, primary_url, notes,
                                      mandatory, gh_url, sha256=sha256,
                                      size=exe_size)
        if gitee_url:
            gitee_push_version_json(gitee_token, payload, log)
        # 【2026-09-15 修 H4】把官网页面里写死的下载地址/版本号同步到新版本。
        # 旧流程只更新 version.json、不碰 HTML，每发一版都得人工改 5 处按钮，
        # 漏改就会在官网挂出 404 的旧包地址。
        log("[4.5/5] 同步官网页面下载地址与版本号 …")
        sync_page_download_links(website_dir, version, log)
        log("[5/5] 推送官网仓库 …")
        website_pushed = git_commit_push(website_dir, version, log, token=token)

        all_ok = bool(gitee_url) and gh_uploaded and website_pushed
        if all_ok:
            log("✅ 发布完成！软件内「检查更新」稍后即可检测到 v" + version)
        else:
            log("⚠️ 发布流程已执行，但**并非全部成功**，请逐项确认：")
            if not gitee_url:
                log("   · Gitee 国内主源未上传（降级为仅 GitHub）")
            if not gh_uploaded:
                log("   · GitHub 兜底源上传未确认")
            if not website_pushed:
                log("   · 官网仓库未推送（GitHub Pages 上的 version.json 可能陈旧；"
                    "客户端主读 Gitee，通常不影响更新检测）")
        log("   国内下载（主用）：" + primary_url)
        log("   海外下载（兜底）：" + gh_url)
        log(f"   安装包 SHA256：{sha256}")
        return all_ok
    except GiteePublishError as ge:
        log(f"❌ 发布失败（国内主源 Gitee）：{ge}")
        log("   已中止：Gitee 是国内用户的默认下载源。跳过它会把 GitHub 地址当作主源，")
        log("   国内多数用户打不开 GitHub → 等于新版本没有可用下载。")
        return False
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
