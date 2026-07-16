#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动补号控制面板 — 本地 Web UI

  python panel_server.py
  → http://127.0.0.1:8787
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import auto_refill_sub2api as core  # noqa: E402

# ── log capture ────────────────────────────────────────────────────────────

LOG_BUFFER: deque[str] = deque(maxlen=2000)
_log_lock = threading.Lock()


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        # force clean unicode for Windows consoles / panel
        if not isinstance(msg, str):
            msg = str(msg)
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with _log_lock:
            LOG_BUFFER.append(line)


def setup_panel_logging() -> None:
    root = logging.getLogger()
    # avoid duplicate handlers on reload
    for h in list(root.handlers):
        if isinstance(h, BufferHandler):
            root.removeHandler(h)
    bh = BufferHandler()
    bh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root.addHandler(bh)
    root.setLevel(logging.INFO)
    # also attach to module logger
    core.log.setLevel(logging.INFO)
    if not any(isinstance(h, BufferHandler) for h in core.log.handlers):
        core.log.addHandler(bh)


setup_panel_logging()
log = logging.getLogger("panel")

# ── worker state ───────────────────────────────────────────────────────────

SECRET_KEYS = {
    "admin_password",
    "admin_token",
    "access_token",
    "password",
    "api_key",
    "proxy_password",
}


class WorkerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False  # loop mode
        self.busy = False  # any job in flight
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.mode = "idle"  # idle | loop | once | cleanup | register
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cycle_count = 0
        self.config_path = core.DEFAULT_CONFIG


WORKER = WorkerState()


def config_path() -> Path:
    return WORKER.config_path


def load_cfg() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        cfg = core.default_config()
        core.save_json(path, cfg)
        return cfg
    return core.load_config(path)


def save_cfg(cfg: dict[str, Any]) -> None:
    core.save_json(config_path(), cfg)


def mask_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SECRET_KEYS and isinstance(v, str) and v:
                if len(v) <= 4:
                    out[k] = "****"
                else:
                    out[k] = v[:2] + "****" + v[-2:]
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [mask_secrets(x) for x in obj]
    return obj


def merge_secrets(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """If UI sends masked **** values, keep old secrets."""
    out = dict(new)
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(old.get(k), dict):
            out[k] = merge_secrets(old[k], v)
        elif k in SECRET_KEYS and isinstance(v, str) and "****" in v:
            out[k] = old.get(k, "")
    return out


def public_status() -> dict[str, Any]:
    state = core.load_json(core.STATE_FILE)
    with WORKER.lock:
        return {
            "running": WORKER.running,
            "busy": WORKER.busy,
            "mode": WORKER.mode,
            "cycle_count": WORKER.cycle_count,
            "started_at": WORKER.started_at,
            "finished_at": WORKER.finished_at,
            "last_error": WORKER.last_error,
            "last_result": WORKER.last_result,
            "config_path": str(config_path()),
            "state_file": str(core.STATE_FILE),
            "history_len": len(state.get("history") or []),
            "last_state": state.get("last"),
        }


def _run_job(kind: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_cfg()
    if overrides:
        cfg = core.merge_dict(cfg, overrides)

    # apply policy flags by kind
    if kind == "cleanup":
        cfg.setdefault("policy", {})
        cfg["policy"]["enable_cleanup"] = True
        cfg["policy"]["enable_register"] = False
        cfg["policy"]["enable_refill"] = False
    elif kind == "register":
        cfg.setdefault("policy", {})
        cfg["policy"]["enable_cleanup"] = False
        cfg["policy"]["enable_register"] = True
        cfg["policy"]["enable_refill"] = False
    elif kind == "refill":
        cfg.setdefault("policy", {})
        cfg["policy"]["enable_cleanup"] = False
        cfg["policy"]["enable_register"] = True
        cfg["policy"]["enable_refill"] = True
    # kind == "full" uses config as-is

    source = core.SourceClient(cfg["source"])
    target = core.TargetClient(cfg["target"])
    source.login()
    target.login()
    result = core.run_cycle(cfg, source, target)

    # persist state
    state = core.load_json(core.STATE_FILE)
    history = state.get("history") or []
    history.append(result)
    state["history"] = history[-100:]
    state["last"] = result
    core.save_json(core.STATE_FILE, state)
    return result


def _job_wrapper(kind: str, overrides: dict[str, Any] | None = None) -> None:
    with WORKER.lock:
        if WORKER.busy and kind != "loop-tick":
            log.warning("已有任务在运行，跳过 %s", kind)
            return
        WORKER.busy = True
        WORKER.mode = kind if kind != "loop-tick" else "loop"
        WORKER.last_error = None
        WORKER.started_at = time.time()
        WORKER.finished_at = None
    try:
        log.info("===== 开始任务: %s =====", kind)
        result = _run_job("full" if kind == "loop-tick" else kind, overrides)
        with WORKER.lock:
            WORKER.last_result = result
            WORKER.cycle_count += 1
        log.info("===== 任务结束: %s =====", result.get("message") or kind)
    except Exception as e:  # noqa: BLE001
        err = str(e) or f"{type(e).__name__}"
        # Prefer short, actionable messages over full HTTPError dumps
        if "401" in err or "Unauthorized" in err or "密码" in err:
            log.error("任务失败: %s", err)
            log.error(
                "提示: 本地密码必须与 http://127.0.0.1:3000/admin/login 一致；"
                "远程填管理员邮箱密码或 JWT access_token。改完后先点「测试两边登录」。"
            )
        else:
            log.error("任务失败: %s\n%s", err, traceback.format_exc()[-1200:])
        with WORKER.lock:
            WORKER.last_error = err
    finally:
        with WORKER.lock:
            WORKER.busy = False
            if not WORKER.running:
                WORKER.mode = "idle"
            WORKER.finished_at = time.time()


def loop_worker() -> None:
    log.info("循环线程启动")
    while not WORKER.stop_event.is_set():
        cfg = load_cfg()
        interval = max(30, int(cfg.get("policy", {}).get("interval_sec") or 300))
        _job_wrapper("loop-tick")
        # interruptible sleep
        for _ in range(interval):
            if WORKER.stop_event.is_set():
                break
            time.sleep(1)
    with WORKER.lock:
        WORKER.running = False
        WORKER.mode = "idle"
        WORKER.thread = None
    log.info("循环线程已停止")


# ── FastAPI ────────────────────────────────────────────────────────────────

app = FastAPI(title="Auto Refill Panel", version="1.0")


class ConfigBody(BaseModel):
    config: dict[str, Any]


class RunBody(BaseModel):
    kind: str = Field(default="full", description="full|cleanup|register|refill")
    dry_run: bool = False


class LoginTestBody(BaseModel):
    which: str = Field(default="both", description="source|target|both")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PANEL_HTML


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(public_status(), media_type="application/json; charset=utf-8")


@app.get("/api/logs")
def api_logs(tail: int = 200, since: int = 0) -> JSONResponse:
    with _log_lock:
        lines = list(LOG_BUFFER)
    total = len(lines)
    if since > 0 and since < total:
        lines = lines[since:]
    else:
        lines = lines[-max(1, min(tail, 2000)) :]
    return JSONResponse(
        {"lines": lines, "total": total},
        media_type="application/json; charset=utf-8",
    )


@app.get("/api/config")
def api_get_config(mask: bool = True) -> dict[str, Any]:
    cfg = load_cfg()
    return {
        "path": str(config_path()),
        "config": mask_secrets(cfg) if mask else cfg,
        "masked": mask,
    }


@app.put("/api/config")
def api_put_config(body: ConfigBody) -> dict[str, Any]:
    old = load_cfg()
    merged = merge_secrets(old, body.config)
    # ensure structure
    base = core.default_config()
    cfg = core.merge_dict(base, merged)
    save_cfg(cfg)
    log.info("配置已保存 → %s", config_path())
    return {"ok": True, "path": str(config_path()), "config": mask_secrets(cfg)}


@app.post("/api/run")
def api_run(body: RunBody) -> dict[str, Any]:
    with WORKER.lock:
        if WORKER.busy:
            raise HTTPException(409, "已有任务在运行")
        if WORKER.running:
            raise HTTPException(409, "循环模式运行中，请先停止再单轮执行")
    kind = (body.kind or "full").lower()
    if kind not in ("full", "cleanup", "register", "refill"):
        raise HTTPException(400, f"unknown kind: {kind}")
    overrides: dict[str, Any] = {}
    if body.dry_run:
        overrides = {"policy": {"dry_run": True}}
    t = threading.Thread(
        target=_job_wrapper,
        args=(kind, overrides or None),
        daemon=True,
        name=f"job-{kind}",
    )
    t.start()
    return {"ok": True, "started": kind, "dry_run": body.dry_run}


@app.post("/api/loop/start")
def api_loop_start() -> dict[str, Any]:
    with WORKER.lock:
        if WORKER.running:
            return {"ok": True, "message": "已在运行"}
        if WORKER.busy:
            raise HTTPException(409, "有单轮任务在跑，请稍候")
        WORKER.stop_event.clear()
        WORKER.running = True
        WORKER.mode = "loop"
        WORKER.thread = threading.Thread(target=loop_worker, daemon=True, name="refill-loop")
        WORKER.thread.start()
    log.info("已启动循环模式")
    return {"ok": True, "message": "循环已启动"}


@app.post("/api/loop/stop")
def api_loop_stop() -> dict[str, Any]:
    with WORKER.lock:
        if not WORKER.running:
            return {"ok": True, "message": "未在运行"}
        WORKER.stop_event.set()
    log.info("正在停止循环…")
    return {"ok": True, "message": "已发送停止信号"}


@app.post("/api/test-login")
def api_test_login(body: LoginTestBody) -> dict[str, Any]:
    cfg = load_cfg()
    out: dict[str, Any] = {}
    which = body.which or "both"
    if which in ("source", "both"):
        try:
            s = core.SourceClient(cfg["source"])
            s.login()
            # light probe
            r = s.list_accounts(status="active", page=1, page_size=1)
            total = r.get("total") or r.get("account_count") or len(r.get("accounts") or [])
            out["source"] = {"ok": True, "total_hint": total, "base_url": cfg["source"]["base_url"]}
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                msg = (
                    "本地密码错误(401)。请填写能打开 http://127.0.0.1:3000/admin/login 的管理密码后保存。"
                    "注意：.env 初始密码在后台改过后会失效。"
                )
            out["source"] = {"ok": False, "error": msg}
    if which in ("target", "both"):
        try:
            t = core.TargetClient(cfg["target"])
            t.login()
            gid = t.resolve_group_id()
            cnt = t.count_group_accounts(gid)
            out["target"] = {
                "ok": True,
                "group_id": gid,
                "count": cnt,
                "base_url": cfg["target"]["base_url"],
            }
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                msg = "远程账号/密码错误(401)。请检查邮箱密码，或改用 access_token。"
            out["target"] = {"ok": False, "error": msg}
    return out


@app.get("/api/history")
def api_history(limit: int = 20) -> JSONResponse:
    state = core.load_json(core.STATE_FILE)
    hist = state.get("history") or []
    return JSONResponse(
        {"items": hist[-max(1, min(limit, 100)) :], "total": len(hist)},
        media_type="application/json; charset=utf-8",
    )


@app.post("/api/init-config")
def api_init_config() -> dict[str, Any]:
    path = config_path()
    if path.is_file():
        return {"ok": True, "message": "已存在", "path": str(path)}
    cfg = core.default_config()
    core.save_json(path, cfg)
    return {"ok": True, "message": "已生成", "path": str(path)}


# ── HTML UI ────────────────────────────────────────────────────────────────

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>号池自动补号控制台</title>
<style>
  :root {
    --bg: #0f1419;
    --panel: #1a2332;
    --panel2: #243044;
    --line: #2d3a4f;
    --text: #e7eef8;
    --muted: #8b9bb4;
    --accent: #3b82f6;
    --accent2: #22c55e;
    --warn: #f59e0b;
    --danger: #ef4444;
    --radius: 12px;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --sans: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font-family: var(--sans); color: var(--text);
    background: radial-gradient(1200px 600px at 10% -10%, #1e3a5f55, transparent),
                radial-gradient(900px 500px at 100% 0%, #14532d33, transparent),
                var(--bg);
  }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 20px 18px 40px; }
  header {
    display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
    gap: 12px; margin-bottom: 18px;
  }
  h1 { margin: 0; font-size: 1.55rem; letter-spacing: .02em; }
  .sub { color: var(--muted); font-size: .92rem; margin-top: 6px; }
  .badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 12px; border-radius: 999px;
    background: var(--panel); border: 1px solid var(--line); font-size: .85rem;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
  .dot.on { background: var(--accent2); box-shadow: 0 0 10px #22c55e88; }
  .dot.busy { background: var(--warn); box-shadow: 0 0 10px #f59e0b88; }
  .dot.err { background: var(--danger); }
  .grid {
    display: grid;
    grid-template-columns: 1.1fr .9fr;
    gap: 14px;
  }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: linear-gradient(180deg, #1c2738, var(--panel));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
    box-shadow: 0 12px 40px #0005;
  }
  .card h2 {
    margin: 0 0 12px; font-size: 1rem; color: #cfe0ff;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }
  .row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
  button, .btn {
    appearance: none; border: 1px solid var(--line); background: var(--panel2);
    color: var(--text); border-radius: 10px; padding: 9px 14px; cursor: pointer;
    font: inherit; font-size: .88rem; transition: .15s ease;
  }
  button:hover { border-color: #4b6288; background: #2c3c55; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: #2563eb; border-color: #3b82f6; }
  button.primary:hover { background: #1d4ed8; }
  button.good { background: #15803d; border-color: #22c55e; }
  button.danger { background: #991b1b; border-color: #ef4444; }
  button.warn { background: #92400e; border-color: #f59e0b; }
  button.ghost { background: transparent; }
  label { display: block; font-size: .78rem; color: var(--muted); margin: 8px 0 4px; }
  input, select, textarea {
    width: 100%; background: #0d1320; color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 9px 10px;
    font: inherit; font-size: .9rem;
  }
  textarea { font-family: var(--mono); font-size: .8rem; min-height: 280px; resize: vertical; }
  .fields {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px;
  }
  @media (max-width: 640px) { .fields { grid-template-columns: 1fr; } }
  .stat-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px;
  }
  @media (max-width: 720px) { .stat-grid { grid-template-columns: 1fr 1fr; } }
  .stat {
    background: #0d1522; border: 1px solid var(--line); border-radius: 10px; padding: 10px;
  }
  .stat .k { color: var(--muted); font-size: .72rem; }
  .stat .v { font-size: 1.25rem; font-weight: 700; margin-top: 4px; }
  .log {
    background: #080c14; border: 1px solid var(--line); border-radius: 10px;
    height: 360px; overflow: auto; padding: 10px 12px;
    font-family: var(--mono); font-size: .78rem; line-height: 1.45; white-space: pre-wrap;
  }
  .log .err { color: #fca5a5; }
  .log .ok { color: #86efac; }
  .log .warn { color: #fcd34d; }
  .msg {
    margin-top: 8px; padding: 8px 10px; border-radius: 8px; font-size: .85rem;
    background: #0d1522; border: 1px solid var(--line); color: var(--muted);
    min-height: 38px;
  }
  .msg.ok { border-color: #166534; color: #bbf7d0; }
  .msg.err { border-color: #7f1d1d; color: #fecaca; }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tabs button { padding: 6px 12px; font-size: .82rem; }
  .tabs button.active { background: #1d4ed8; border-color: #3b82f6; }
  .hist {
    max-height: 220px; overflow: auto; font-family: var(--mono); font-size: .75rem;
    background: #0d1522; border: 1px solid var(--line); border-radius: 8px; padding: 8px;
  }
  .hist div { padding: 4px 0; border-bottom: 1px solid #1a2436; color: var(--muted); }
  footer { margin-top: 16px; color: var(--muted); font-size: .78rem; text-align: center; }
  .check { display: flex; align-items: center; gap: 8px; margin-top: 10px; color: var(--muted); font-size: .85rem; }
  .check input { width: auto; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>号池自动补号控制台</h1>
      <div class="sub">远程测活删号 · 本地注册 · 导出转换 · 导入 sub2api</div>
    </div>
    <div class="badge">
      <span class="dot" id="status-dot"></span>
      <span id="status-text">加载中…</span>
    </div>
  </header>

  <div class="stat-grid">
    <div class="stat"><div class="k">运行模式</div><div class="v" id="s-mode">—</div></div>
    <div class="stat"><div class="k">已完成轮次</div><div class="v" id="s-cycles">0</div></div>
    <div class="stat"><div class="k">远程分组 ID</div><div class="v" id="s-group">—</div></div>
    <div class="stat"><div class="k">最近一次结果</div><div class="v" id="s-last" style="font-size:.85rem;font-weight:600">—</div></div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>操作面板</h2>
      <div class="row">
        <button class="good" id="btn-loop-start" onclick="loopStart()">▶ 启动自动循环</button>
        <button class="danger" id="btn-loop-stop" onclick="loopStop()">■ 停止循环</button>
        <button class="primary" id="btn-run-full" onclick="run('full')">执行完整闭环（一轮）</button>
      </div>
      <div class="row">
        <button class="warn" onclick="run('cleanup')">仅清理远程坏号</button>
        <button onclick="run('register')">仅本地注册账号</button>
        <button onclick="run('refill')">仅补号（含注册）</button>
        <button class="ghost" onclick="run('full', true)">模拟运行（不删不导）</button>
      </div>
      <div class="row">
        <button onclick="testLogin()">测试两边登录</button>
        <button class="ghost" onclick="refreshAll()">刷新状态</button>
        <button class="ghost" onclick="clearLogView()">清空日志视图</button>
      </div>
      <div class="check">
        <input type="checkbox" id="opt-dry" />
        <label for="opt-dry" style="margin:0">操作默认使用模拟模式（不删除、不导入）</label>
      </div>
      <div class="msg" id="toast">就绪</div>

      <h2 style="margin-top:16px">最近运行历史</h2>
      <div class="hist" id="history">—</div>
    </div>

    <div class="card">
      <h2>
        <span>参数配置</span>
        <span style="font-weight:400;color:var(--muted);font-size:.78rem" id="cfg-path"></span>
      </h2>
      <div class="tabs">
        <button class="active" data-tab="form" onclick="switchTab('form')">简易表单</button>
        <button data-tab="json" onclick="switchTab('json')">高级 JSON</button>
      </div>
      <div id="tab-form">
        <div class="fields">
          <div>
            <label>本地后台地址（grok2api）</label>
            <input id="f-src-url" placeholder="例如 http://127.0.0.1:3000" />
          </div>
          <div>
            <label>本地管理密码</label>
            <input id="f-src-pass" type="password" placeholder="与本地后台登录密码一致；留空则不修改" />
          </div>
          <div>
            <label>远程后台地址（sub2api）</label>
            <input id="f-tgt-url" placeholder="例如 https://fd.diamondruby.xyz" />
          </div>
          <div>
            <label>远程管理员邮箱</label>
            <input id="f-tgt-email" placeholder="sub2api 管理员账号邮箱" />
          </div>
          <div>
            <label>远程管理员密码</label>
            <input id="f-tgt-pass" type="password" placeholder="留空则不修改已保存密码" />
          </div>
          <div>
            <label>远程登录令牌（可选，优先于密码）</label>
            <input id="f-tgt-token" type="password" placeholder="浏览器 localStorage 的 access_token；留空不修改" />
          </div>
          <div>
            <label>目标分组名称</label>
            <input id="f-group-name" placeholder="例如 grok" />
          </div>
          <div>
            <label>目标分组 ID（优先，填了就不按名称找）</label>
            <input id="f-group-id" placeholder="例如 11；不填则按名称自动匹配" />
          </div>
          <div>
            <label>号池下限（少于此数量开始补号）</label>
            <input id="f-min" type="number" placeholder="例如 50" />
          </div>
          <div>
            <label>号池目标数量（补到多少）</label>
            <input id="f-target" type="number" placeholder="例如 100" />
          </div>
          <div>
            <label>单轮最多导入数量</label>
            <input id="f-max" type="number" placeholder="例如 50" />
          </div>
          <div>
            <label>循环检测间隔（秒）</label>
            <input id="f-interval" type="number" placeholder="例如 300" />
          </div>
          <div>
            <label>本地每次注册数量</label>
            <input id="f-reg-count" type="number" placeholder="例如 50" />
          </div>
          <div>
            <label>远程测活并发数</label>
            <input id="f-clean-workers" type="number" placeholder="例如 6" />
          </div>
          <div>
            <label>每轮最多测活数量（轮询扫描）</label>
            <input id="f-clean-max" type="number" placeholder="例如 60，全量很慢" />
          </div>
          <div>
            <label>测活超时（秒）</label>
            <input id="f-clean-timeout" type="number" placeholder="例如 45" />
          </div>
        </div>
        <div class="row" style="margin-top:12px">
          <label class="check" style="margin:0"><input type="checkbox" id="f-cleanup" /> 自动清理远程坏号</label>
          <label class="check" style="margin:0"><input type="checkbox" id="f-register" /> 本地不足时自动注册</label>
          <label class="check" style="margin:0"><input type="checkbox" id="f-refill" /> 自动导出并导入补号</label>
          <label class="check" style="margin:0"><input type="checkbox" id="f-del-local" /> 导入后删除本地已导出账号</label>
        </div>
        <div class="row" style="margin-top:10px">
          <button class="primary" onclick="saveForm()">保存配置</button>
          <button class="ghost" onclick="loadConfig()">重新加载</button>
        </div>
        <div class="msg" style="margin-top:10px;font-size:.8rem">
          优化说明：测活改为「每轮抽样 + 游标轮询」，不再全量扫；注册按缺口补，单次默认 50。
          本地密码需能打开 <code>http://127.0.0.1:3000/admin/login</code>。
        </div>
      </div>
      <div id="tab-json" style="display:none">
        <textarea id="cfg-json" spellcheck="false"></textarea>
        <div class="row" style="margin-top:8px">
          <button class="primary" onclick="saveJson()">保存 JSON</button>
          <button class="ghost" onclick="loadConfig()">重新加载</button>
        </div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>
      <span>实时日志</span>
      <span style="font-weight:400;color:var(--muted);font-size:.78rem">自动刷新</span>
    </h2>
    <div class="log" id="log"></div>
  </div>

  <footer>仅本机可访问（127.0.0.1）· 配置文件：scripts/config.auto_refill.json · 请勿把含密码/Token 的文件提交公开仓库</footer>
</div>

<script>
let CFG = null;
let logTotal = 0;
let logPaused = false;

function $(id) { return document.getElementById(id); }

function toast(msg, ok) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "msg " + (ok === true ? "ok" : ok === false ? "err" : "");
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || data.message || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function switchTab(name) {
  $("tab-form").style.display = name === "form" ? "" : "none";
  $("tab-json").style.display = name === "json" ? "" : "none";
  document.querySelectorAll(".tabs button").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
}

function fillForm(cfg) {
  CFG = cfg;
  const s = cfg.source || {}, t = cfg.target || {}, p = cfg.policy || {};
  $("f-src-url").value = s.base_url || "";
  $("f-src-pass").value = "";
  $("f-tgt-url").value = t.base_url || "";
  $("f-tgt-email").value = t.email || "";
  $("f-tgt-pass").value = "";
  $("f-tgt-token").value = "";
  $("f-group-name").value = t.group_name || "";
  $("f-group-id").value = t.group_id == null ? "" : t.group_id;
  $("f-min").value = p.min_count ?? 50;
  $("f-target").value = p.target_count ?? 100;
  $("f-max").value = p.max_per_cycle ?? 50;
  $("f-interval").value = p.interval_sec ?? 300;
  $("f-reg-count").value = s.register_count ?? 50;
  $("f-clean-workers").value = t.cleanup_workers ?? 6;
  if ($("f-clean-max")) $("f-clean-max").value = t.cleanup_max ?? 60;
  if ($("f-clean-timeout")) $("f-clean-timeout").value = t.cleanup_timeout_sec ?? 45;
  $("f-cleanup").checked = p.enable_cleanup !== false && t.cleanup_bad !== false;
  $("f-register").checked = p.enable_register !== false && s.auto_register !== false;
  $("f-refill").checked = p.enable_refill !== false;
  $("f-del-local").checked = !!s.delete_after_export;
  $("cfg-json").value = JSON.stringify(cfg, null, 2);
}

function collectFormPatch() {
  const patch = {
    source: {
      base_url: $("f-src-url").value.trim(),
      register_count: Number($("f-reg-count").value || 50),
      auto_register: $("f-register").checked,
      delete_after_export: $("f-del-local").checked,
    },
    target: {
      base_url: $("f-tgt-url").value.trim(),
      email: $("f-tgt-email").value.trim(),
      group_name: $("f-group-name").value.trim(),
      cleanup_bad: $("f-cleanup").checked,
      cleanup_workers: Number($("f-clean-workers").value || 6),
      cleanup_max: Number(($("f-clean-max") && $("f-clean-max").value) || 60),
      cleanup_timeout_sec: Number(($("f-clean-timeout") && $("f-clean-timeout").value) || 45),
    },
    policy: {
      min_count: Number($("f-min").value || 300),
      target_count: Number($("f-target").value || 500),
      max_per_cycle: Number($("f-max").value || 50),
      interval_sec: Number($("f-interval").value || 180),
      enable_cleanup: $("f-cleanup").checked,
      enable_register: $("f-register").checked,
      enable_refill: $("f-refill").checked,
    },
  };
  const gid = $("f-group-id").value.trim();
  patch.target.group_id = gid === "" ? null : Number(gid);
  const sp = $("f-src-pass").value;
  if (sp) patch.source.admin_password = sp;
  const tp = $("f-tgt-pass").value;
  if (tp) patch.target.password = tp;
  const tt = $("f-tgt-token").value;
  if (tt) patch.target.access_token = tt;
  return patch;
}

async function loadConfig() {
  const data = await api("/api/config?mask=true");
  $("cfg-path").textContent = data.path || "";
  fillForm(data.config);
}

async function saveForm() {
  try {
    // merge patch onto current full masked config, then server keeps secrets
    const base = CFG || (await api("/api/config?mask=true")).config;
    const patch = collectFormPatch();
    const merged = {
      ...base,
      source: { ...(base.source || {}), ...patch.source },
      target: { ...(base.target || {}), ...patch.target },
      policy: { ...(base.policy || {}), ...patch.policy },
      logging: base.logging || {},
    };
    const r = await api("/api/config", { method: "PUT", body: JSON.stringify({ config: merged }) });
    fillForm(r.config);
    toast("配置已保存", true);
  } catch (e) {
    toast("保存失败: " + e.message, false);
  }
}

async function saveJson() {
  try {
    const cfg = JSON.parse($("cfg-json").value);
    const r = await api("/api/config", { method: "PUT", body: JSON.stringify({ config: cfg }) });
    fillForm(r.config);
    toast("JSON 配置已保存", true);
  } catch (e) {
    toast("保存失败: " + e.message, false);
  }
}

function setButtons(st) {
  const busy = !!st.busy;
  const running = !!st.running;
  $("btn-loop-start").disabled = running || busy;
  $("btn-loop-stop").disabled = !running;
  $("btn-run-full").disabled = busy || running;
}

async function refreshStatus() {
  try {
    const st = await api("/api/status");
    const dot = $("status-dot");
    dot.className = "dot" + (st.busy ? " busy" : st.running ? " on" : st.last_error ? " err" : "");
    $("status-text").textContent = st.busy
      ? `运行中 · ${st.mode}`
      : st.running
        ? "循环待命 / 间隔等待"
        : st.last_error
          ? "出错"
          : "空闲";
    $("s-mode").textContent = st.mode || "idle";
    $("s-cycles").textContent = st.cycle_count ?? 0;
    const last = st.last_result || st.last_state || {};
    $("s-group").textContent = last.group_id != null ? last.group_id : "—";
    $("s-last").textContent = (last.message || st.last_error || "—").slice(0, 48);
    setButtons(st);
  } catch (e) {
    $("status-text").textContent = "无法连接面板";
  }
}

async function refreshHistory() {
  try {
    const h = await api("/api/history?limit=15");
    const el = $("history");
    if (!h.items || !h.items.length) {
      el.textContent = "暂无历史";
      return;
    }
    el.innerHTML = h.items.slice().reverse().map(it => {
      const t = it.ts ? new Date(it.ts * 1000).toLocaleString() : "";
      return `<div>[${t}] ${escapeHtml(it.message || JSON.stringify(it).slice(0, 120))}</div>`;
    }).join("");
  } catch (_) {}
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

async function refreshLogs() {
  if (logPaused) return;
  try {
    const data = await api("/api/logs?tail=300");
    const el = $("log");
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
    el.innerHTML = (data.lines || []).map(line => {
      let cls = "";
      if (/\[ERROR\]|失败|错误|Traceback/i.test(line)) cls = "err";
      else if (/\[WARNING\]|警告|跳过/i.test(line)) cls = "warn";
      else if (/完成|ok|成功|已删除|导入结果/i.test(line)) cls = "ok";
      return `<div class="${cls}">${escapeHtml(line)}</div>`;
    }).join("");
    if (stick) el.scrollTop = el.scrollHeight;
    logTotal = data.total || 0;
  } catch (_) {}
}

function clearLogView() {
  $("log").innerHTML = "";
}

async function run(kind, forceDry) {
  const dry = forceDry || $("opt-dry").checked;
  try {
    toast(`启动任务 ${kind}${dry ? " (dry-run)" : ""}…`);
    await api("/api/run", {
      method: "POST",
      body: JSON.stringify({ kind, dry_run: !!dry }),
    });
    toast(`任务 ${kind} 已开始`, true);
    setTimeout(refreshAll, 800);
  } catch (e) {
    toast(e.message, false);
  }
}

async function loopStart() {
  try {
    await api("/api/loop/start", { method: "POST", body: "{}" });
    toast("循环已启动", true);
    refreshStatus();
  } catch (e) {
    toast(e.message, false);
  }
}

async function loopStop() {
  try {
    await api("/api/loop/stop", { method: "POST", body: "{}" });
    toast("已请求停止循环", true);
    refreshStatus();
  } catch (e) {
    toast(e.message, false);
  }
}

async function testLogin() {
  try {
    toast("测试登录中…");
    const r = await api("/api/test-login", {
      method: "POST",
      body: JSON.stringify({ which: "both" }),
    });
    const parts = [];
    if (r.source) {
      parts.push(r.source.ok
        ? `本地✓ (hint=${r.source.total_hint})`
        : `本地✗ ${r.source.error}`);
    }
    if (r.target) {
      parts.push(r.target.ok
        ? `远程✓ group=${r.target.group_id} count=${r.target.count}`
        : `远程✗ ${r.target.error}`);
    }
    const ok = (r.source?.ok !== false) && (r.target?.ok !== false);
    toast(parts.join(" · "), ok);
    if (r.target?.ok) $("s-group").textContent = r.target.group_id;
  } catch (e) {
    toast(e.message, false);
  }
}

async function refreshAll() {
  await Promise.all([refreshStatus(), refreshLogs(), refreshHistory()]);
}

loadConfig().then(refreshAll).catch(e => toast("初始化失败: " + e.message, false));
setInterval(refreshStatus, 2000);
setInterval(refreshLogs, 1500);
setInterval(refreshHistory, 8000);
</script>
</body>
</html>
"""


def main() -> None:
    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("PANEL_PORT", "8787"))
    # ensure config exists
    if not core.DEFAULT_CONFIG.is_file():
        core.save_json(core.DEFAULT_CONFIG, core.default_config())
        log.info("已生成默认配置 %s", core.DEFAULT_CONFIG)
    log.info("控制面板: http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
