#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全自动号池闭环：sub2api Grok 分组 ⇄ 本地 grok2api 注册/导出

完整流程（每轮）：
  A. 远程清理：遍历目标分组账号，POST /admin/accounts/{id}/test
     - 非 200 / success=false → 自动 DELETE 删除
  B. 统计远程可用数；若 < min_count：
     B1. 本地可用验活账号不足时 → 触发协议注册补源
         POST /admin/api/accounts/register-email，轮询 batch 直到完成
     B2. 从本地导出 active + last_probe.ok 账号
     B3. 转 sub2api 格式并 POST /api/v1/admin/accounts/data 导入
     B4. 可选绑定 group_id；可选本地删除已导出账号

配置：同目录 config.auto_refill.json。敏感信息勿提交仓库。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── defaults ───────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.auto_refill.json"
STATE_FILE = SCRIPT_DIR / ".auto_refill_state.json"
LOCK_FILE = SCRIPT_DIR / ".auto_refill.lock"

DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_SCOPE = "openid profile email offline_access grok-cli:access api:access"

log = logging.getLogger("auto_refill")


def interruptible_sleep(seconds: float, should_stop=None) -> bool:
    """Sleep up to seconds; return True if stopped early.
    should_stop: optional callable -> bool
    """
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if callable(should_stop) and should_stop():
            return True
        time.sleep(min(1.0, max(0.05, end - time.time())))
    return False



# ── process lock / state helpers ───────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_lock(owner: str = "auto_refill") -> bool:
    """Single-instance lock. Returns True if acquired."""
    now = time.time()
    if LOCK_FILE.is_file():
        try:
            prev = load_json(LOCK_FILE)
            prev_pid = int(prev.get("pid") or 0)
            if prev_pid and prev_pid != os.getpid() and _pid_alive(prev_pid):
                log.error(
                    "已有实例在运行 pid=%s owner=%s started=%s → %s",
                    prev_pid,
                    prev.get("owner"),
                    prev.get("started_at"),
                    LOCK_FILE,
                )
                return False
        except Exception:
            pass
    save_json(
        LOCK_FILE,
        {
            "pid": os.getpid(),
            "owner": owner,
            "started_at": now,
            "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
        },
    )
    return True


def release_lock() -> None:
    try:
        if not LOCK_FILE.is_file():
            return
        prev = load_json(LOCK_FILE)
        if int(prev.get("pid") or 0) in (0, os.getpid()):
            LOCK_FILE.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        # py<3.8 missing_ok fallback
        try:
            if LOCK_FILE.is_file():
                LOCK_FILE.unlink()
        except Exception:
            pass
    except Exception:
        pass


def load_state() -> dict[str, Any]:
    return load_json(STATE_FILE)


def save_state(state: dict[str, Any]) -> None:
    save_json(STATE_FILE, state)


def get_cleanup_cursor() -> int:
    st = load_state()
    try:
        return int(st.get("cleanup_cursor") or 0)
    except Exception:
        return 0


def set_cleanup_cursor(cursor: int) -> None:
    st = load_state()
    st["cleanup_cursor"] = int(cursor)
    st["cleanup_cursor_updated_at"] = time.time()
    save_state(st)


def get_fail_streaks() -> dict[str, int]:
    st = load_state()
    raw = st.get("fail_streaks") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except Exception:
            continue
    return out


def set_fail_streaks(streaks: dict[str, int]) -> None:
    st = load_state()
    # cap size
    items = sorted(streaks.items(), key=lambda kv: kv[1], reverse=True)[:2000]
    st["fail_streaks"] = {k: int(v) for k, v in items if int(v) > 0}
    save_state(st)


# ── util ───────────────────────────────────────────────────────────────────

def _first_non_empty(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _to_unix_seconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:  # ms
            n /= 1000.0
        return int(n)
    s = str(value).strip()
    if not s:
        return None
    try:
        n = float(s)
        if n > 1e12:
            n /= 1000.0
        return int(n)
    except ValueError:
        pass
    try:
        # ISO-8601
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _to_rfc3339(unix_sec: int | float | None) -> str | None:
    if unix_sec is None:
        return None
    return datetime.fromtimestamp(float(unix_sec), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _parse_jwt_payload(token: str | None) -> dict[str, Any] | None:
    if not token or not isinstance(token, str) or token.count(".") < 2:
        return None
    try:
        payload = _b64url_decode(token.split(".")[1])
        data = json.loads(payload.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _strip_empty(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if v is not None and v != ""}


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ── config ─────────────────────────────────────────────────────────────────

def default_config() -> dict[str, Any]:
    return {
        # 本地 grok2api（账号源 + 注册）
        "source": {
            "base_url": "http://127.0.0.1:3000",
            "admin_password": "",  # 或用 admin_token
            "admin_token": "",
            # 导出筛选：active = 轮询中；空字符串 = 不过滤状态
            "status": "active",
            # 只要 last_probe.ok == true 的账号（验活通过）
            "require_probe_ok": True,
            # 本地分组名过滤（空 = 不限）
            "group": "",
            "export_batch_size": 50,
            # 导出并成功绑定远程分组后，是否从本地删除（防丢号：绑失败不删）
            "delete_after_export": True,
            # 仅在绑定成功（或明确跳过绑定）后才删本地
            "delete_only_after_bind": True,
            # 本地号池最少保留多少 active 才够导出；不足则注册
            "min_local_active": 50,
            # 额外缓冲：exportable 目标 = max(need, min_local) + buffer
            "local_buffer": 20,
            # 自动注册
            "auto_register": True,
            "register_count": 12,  # 提速：12/批，够数 early-stop
            "register_concurrency": 3,  # 提速并发
            "register_stagger_ms": 500,
            "register_probe_delay_sec": 15,
            # 空 body 会用后台已保存的 registration_config（邮件/打码/代理）
            "register_body": {},
            "register_poll_interval_sec": 8,
            "register_timeout_sec": 720,
            # 注册完成后等待秒数再导出（给 probe 时间）
            "register_settle_sec": 12,
            # 注册前检查本地是否过载；过载则跳过本轮注册
            "register_health_gate": True,
            "register_max_active_sessions": 10,
            # rate_limited 退避
            "register_rate_limit_retries": 3,
            "register_rate_limit_backoff_sec": 60,
            # 达到 need 后提前结束等待（不必等整批失败会话）
            "register_early_stop_ok": 0,  # 0=自动按 deficit
            "register_max_cpu_skip": False,
        },
        # 远程 sub2api（号池目标）
        "target": {
            "base_url": "https://fd.diamondruby.xyz",
            # 推荐 JWT：登录后 localStorage access_token，或接口返回
            "access_token": "",
            "email": "",
            "password": "",
            # 分组：优先 group_id；也可填 group_name 自动解析
            "group_id": None,
            "group_name": "grok",
            "platform": "grok",
            # 导入后是否 bulk-update 绑定 group_ids
            "bind_group_after_import": True,
            # 跳过默认分组绑定（导入到指定分组时建议 true）
            "skip_default_group_bind": True,
            # 每轮是否先测活并删除非 200
            "cleanup_bad": True,
            # 只检测 active 状态；false = 检测分组内全部
            "cleanup_active_only": True,
            # 单轮最多测活数量（0=不限；建议 40~80，全量测活很慢）
            "cleanup_max": 60,
            # 测活并发（线程）
            "cleanup_workers": 6,
            # 两次测活间隔秒（防压垮上游）
            "cleanup_delay_sec": 0.05,
            # 测活超时秒
            "cleanup_timeout_sec": 45,
            # 失败达到 streak 后才删
            "cleanup_fail_delete": True,
            # 同一账号连续失败几轮才删除（1=立刻删）
            "cleanup_fail_streak": 2,
            "cleanup_delete_unschedulable": True,
            "cleanup_delete_error_message": True,
            "cleanup_metadata_max": 500,
            # 游标改存 .auto_refill_state.json（此处仅兼容读旧配置）
            "cleanup_cursor": 0,
            # 优先测这些状态（空=只按 active/全部分页）
            "cleanup_prefer_status": ["error", "inactive", "expired", "quota_exhausted"],
            # 主池按页拉取，避免每轮 list 全量
            "cleanup_page_size": 100,
        },
        # 补号策略
        "policy": {
            "min_count": 300,  # 低于此值触发补号（紧急）
            "target_count": 500,  # 补到此值
            "max_per_cycle": 50,  # 单次最多导入
            # 分层：current 在 [min, target) 时用较小批量
            "normal_batch": 25,
            "interval_sec": 180,  # 轮询间隔
            "import_chunk_size": 25,  # 导入分片大小
            "concurrency": 1,
            "priority": 1,
            "rate_multiplier": 1.0,
            "dry_run": False,
            # 阶段开关
            "enable_cleanup": True,
            "enable_register": True,
            "enable_refill": True,
            # 注册不等待整批完成（只触发；下轮再导出）
            "register_async": False,
        },
        "logging": {
            "level": "INFO",
            "file": str(SCRIPT_DIR / "auto_refill.log"),
        },
    }


def merge_dict(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None, cli: argparse.Namespace | None = None) -> dict[str, Any]:
    cfg = default_config()
    conf_path = path or DEFAULT_CONFIG
    if conf_path.is_file():
        cfg = merge_dict(cfg, load_json(conf_path))
        log.debug("loaded config %s", conf_path)
    # env overrides
    env_map = {
        "SOURCE_BASE_URL": ("source", "base_url"),
        "SOURCE_ADMIN_PASSWORD": ("source", "admin_password"),
        "SOURCE_ADMIN_TOKEN": ("source", "admin_token"),
        "TARGET_BASE_URL": ("target", "base_url"),
        "TARGET_ACCESS_TOKEN": ("target", "access_token"),
        "TARGET_EMAIL": ("target", "email"),
        "TARGET_PASSWORD": ("target", "password"),
        "TARGET_GROUP_NAME": ("target", "group_name"),
        "TARGET_GROUP_ID": ("target", "group_id"),
        "MIN_COUNT": ("policy", "min_count"),
        "TARGET_COUNT": ("policy", "target_count"),
        "INTERVAL_SEC": ("policy", "interval_sec"),
    }
    for env_key, path_keys in env_map.items():
        val = os.environ.get(env_key)
        if val is None or val == "":
            continue
        section, key = path_keys
        if key in ("group_id", "min_count", "target_count", "interval_sec"):
            try:
                cfg[section][key] = int(val)
            except ValueError:
                cfg[section][key] = val
        else:
            cfg[section][key] = val
    if cli:
        if getattr(cli, "min_count", None) is not None:
            cfg["policy"]["min_count"] = cli.min_count
        if getattr(cli, "target_count", None) is not None:
            cfg["policy"]["target_count"] = cli.target_count
        if getattr(cli, "interval", None) is not None:
            cfg["policy"]["interval_sec"] = cli.interval
        if getattr(cli, "dry_run", False):
            cfg["policy"]["dry_run"] = True
        if getattr(cli, "once", False):
            cfg["_once"] = True
        if getattr(cli, "group_name", None):
            cfg["target"]["group_name"] = cli.group_name
        if getattr(cli, "group_id", None) is not None:
            cfg["target"]["group_id"] = cli.group_id
        if getattr(cli, "max_per_cycle", None) is not None:
            cfg["policy"]["max_per_cycle"] = cli.max_per_cycle
        if getattr(cli, "skip_cleanup", False):
            cfg["policy"]["enable_cleanup"] = False
        if getattr(cli, "skip_register", False):
            cfg["policy"]["enable_register"] = False
        if getattr(cli, "cleanup_only", False):
            cfg["policy"]["enable_cleanup"] = True
            cfg["policy"]["enable_register"] = False
            cfg["policy"]["enable_refill"] = False
        if getattr(cli, "register_only", False):
            cfg["policy"]["enable_cleanup"] = False
            cfg["policy"]["enable_register"] = True
            cfg["policy"]["enable_refill"] = False
    return cfg


# ── convert (port of localhost:6501 logic) ─────────────────────────────────

def convert_auth_record(
    record: dict[str, Any],
    *,
    concurrency: int = 1,
    priority: int = 1,
    rate_multiplier: float = 1.0,
) -> dict[str, Any]:
    access_token = _first_non_empty(
        record.get("key"),
        record.get("access_token"),
        record.get("accessToken"),
        record.get("token"),
        (record.get("credentials") or {}).get("access_token")
        if isinstance(record.get("credentials"), dict)
        else None,
    )
    refresh_token = _first_non_empty(
        record.get("refresh_token"),
        record.get("refreshToken"),
        record.get("rt"),
        (record.get("credentials") or {}).get("refresh_token")
        if isinstance(record.get("credentials"), dict)
        else None,
    )
    if not access_token and not refresh_token:
        raise ValueError("缺少 access_token / key 或 refresh_token")

    payload = _parse_jwt_payload(str(access_token) if access_token else None)
    expires_at_unix = _first_non_empty(
        _to_unix_seconds(record.get("expires_at")),
        _to_unix_seconds(record.get("expiresAt")),
        _to_unix_seconds(record.get("expired")),
        _to_unix_seconds(
            (record.get("credentials") or {}).get("expires_at")
            if isinstance(record.get("credentials"), dict)
            else None
        ),
        _to_unix_seconds(payload.get("exp")) if payload else None,
    )
    email = _first_non_empty(
        record.get("email"),
        (record.get("credentials") or {}).get("email")
        if isinstance(record.get("credentials"), dict)
        else None,
        payload.get("email") if payload else None,
        record.get("name"),
    )
    sub = _first_non_empty(
        record.get("user_id"),
        record.get("userId"),
        record.get("principal_id"),
        record.get("sub"),
        (record.get("credentials") or {}).get("sub")
        if isinstance(record.get("credentials"), dict)
        else None,
        payload.get("sub") if payload else None,
        payload.get("principal_id") if payload else None,
    )
    team_id = _first_non_empty(
        record.get("team_id"),
        record.get("teamId"),
        (record.get("credentials") or {}).get("team_id")
        if isinstance(record.get("credentials"), dict)
        else None,
        payload.get("team_id") if payload else None,
    )
    client_id = _first_non_empty(
        record.get("oidc_client_id"),
        record.get("client_id"),
        record.get("clientId"),
        (record.get("credentials") or {}).get("client_id")
        if isinstance(record.get("credentials"), dict)
        else None,
        payload.get("client_id") if payload else None,
        payload.get("aud") if payload else None,
        DEFAULT_CLIENT_ID,
    )
    name = _first_non_empty(record.get("name"), email, sub, "Grok OAuth Account")

    credentials = _strip_empty(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": _to_rfc3339(expires_at_unix) if expires_at_unix is not None else None,
            "token_type": _first_non_empty(record.get("token_type"), "Bearer"),
            "client_id": client_id,
            "scope": _first_non_empty(record.get("scope"), DEFAULT_SCOPE),
            "email": email,
            "sub": sub,
            "team_id": team_id,
            "base_url": _first_non_empty(record.get("base_url"), DEFAULT_BASE_URL),
            "id_token": _first_non_empty(record.get("id_token"), record.get("idToken")),
        }
    )
    account = _strip_empty(
        {
            "name": name,
            "notes": _first_non_empty(record.get("notes"), record.get("note")),
            "platform": "grok",
            "type": "oauth",
            "credentials": credentials,
            "extra": _strip_empty(
                {
                    "email": email,
                    "source": "grok2api-auth-auto-refill",
                    "user_id": sub,
                    "team_id": team_id,
                    "oidc_issuer": _first_non_empty(
                        record.get("oidc_issuer"), "https://auth.x.ai"
                    ),
                    "create_time": record.get("create_time"),
                    "auth_mode": _first_non_empty(record.get("auth_mode"), "oidc"),
                }
            ),
            "concurrency": concurrency,
            "priority": priority,
            "rate_multiplier": rate_multiplier,
            "expires_at": expires_at_unix,
            "auto_pause_on_expired": True,
        }
    )
    return account


def convert_export_to_sub2(
    export_payload: dict[str, Any],
    *,
    concurrency: int = 1,
    priority: int = 1,
    rate_multiplier: float = 1.0,
) -> dict[str, Any]:
    auth = export_payload.get("auth")
    if not isinstance(auth, dict):
        raise ValueError("export payload missing auth map")
    accounts: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_emails: set[str] = set()
    for key, rec in auth.items():
        if not isinstance(rec, dict):
            issues.append(f"{key}: not an object")
            continue
        try:
            acc = convert_auth_record(
                rec,
                concurrency=concurrency,
                priority=priority,
                rate_multiplier=rate_multiplier,
            )
        except Exception as e:  # noqa: BLE001
            issues.append(f"{key}: {e}")
            continue
        email_key = str(acc.get("name") or "").lower()
        if email_key and email_key in seen_emails:
            issues.append(f"{key}: duplicate name/email skipped")
            continue
        if email_key:
            seen_emails.add(email_key)
        accounts.append(acc)
    return {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "proxies": [],
        "accounts": accounts,
        "_meta": {"count": len(accounts), "issues": issues},
    }


# ── HTTP clients ───────────────────────────────────────────────────────────

class SourceClient:
    """Local grok2api admin client."""

    def __init__(self, cfg: dict[str, Any], session: requests.Session | None = None):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.s = session or requests.Session()
        self.token = (cfg.get("admin_token") or "").strip()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["X-Admin-Token"] = self.token
        return h

    def login(self) -> None:
        if self.token:
            # validate
            r = self.s.get(
                f"{self.base}/admin/api/session",
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code == 200:
                return
            log.warning("source token invalid, try password login")
            self.token = ""
        password = self.cfg.get("admin_password") or ""
        if not password:
            raise RuntimeError(
                "source.admin_password 或 source.admin_token 必须配置其一"
            )
        r = self.s.post(
            f"{self.base}/admin/api/login",
            json={"password": password},
            timeout=30,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "本地管理密码错误 (401)。请在控制面板「配置」里重新填写 "
                "source.admin_password（与 http://127.0.0.1:3000/admin/login 相同）"
            )
        if r.status_code >= 400:
            raise RuntimeError(
                f"本地登录失败 HTTP {r.status_code}: {(r.text or '')[:300]}"
            )
        data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"source login failed: {data}")
        self.token = str(token)
        log.info("source login ok")

    def list_accounts(
        self,
        *,
        status: str = "active",
        group: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "sort": "newest",
        }
        if status:
            params["status"] = status
        if group:
            params["group"] = group
        r = self.s.get(
            f"{self.base}/admin/api/accounts",
            params=params,
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def collect_export_ids(
        self,
        *,
        need: int,
        status: str,
        group: str,
        require_probe_ok: bool,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> list[str]:
        ids: list[str] = []
        page = 1
        while len(ids) < need and page <= max_pages:
            data = self.list_accounts(
                status=status, group=group, page=page, page_size=page_size
            )
            rows = data.get("accounts") or data.get("items") or []
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                acc_id = str(row.get("id") or "").strip()
                if not acc_id:
                    continue
                if require_probe_ok:
                    pool = row.get("_pool") or row.get("pool") or {}
                    probe = pool.get("last_probe") if isinstance(pool, dict) else None
                    if not isinstance(probe, dict) or not probe.get("ok"):
                        continue
                    if pool.get("enabled") is False:
                        continue
                ids.append(acc_id)
                if len(ids) >= need:
                    break
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1
        return ids

    def export_by_ids(self, ids: list[str]) -> dict[str, Any]:
        if not ids:
            return {"ok": True, "auth": {}, "count": 0}
        r = self.s.post(
            f"{self.base}/admin/api/accounts/export-batch",
            params={"download": 0, "async_job": 0},
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"ids": ids, "include_secrets": True},
            timeout=180,
        )
        r.raise_for_status()
        data = r.json()
        # sync path may wrap as {ok, auth, count} or just auth map
        if isinstance(data, dict) and "auth" in data:
            return data
        if isinstance(data, dict) and any(
            isinstance(v, dict) and ("key" in v or "access_token" in v or "refresh_token" in v)
            for v in data.values()
        ):
            return {"ok": True, "auth": data, "count": len(data)}
        raise RuntimeError(f"unexpected export response keys: {list(data)[:20]}")

    def delete_accounts(self, ids: list[str]) -> dict[str, Any]:
        if not ids:
            return {"ok": True, "deleted": 0}
        r = self.s.post(
            f"{self.base}/admin/api/accounts/delete-batch",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"ids": ids},
            timeout=120,
        )
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}

    def count_exportable(self, limit: int = 500) -> int:
        """Count local accounts that would pass export filters (capped for speed)."""
        # 只扫到 limit 即可，避免每轮全表扫描拖慢闭环
        ids = self.collect_export_ids(
            need=max(1, int(limit)),
            status=self.cfg.get("status") or "active",
            group=self.cfg.get("group") or "",
            require_probe_ok=bool(self.cfg.get("require_probe_ok", True)),
            page_size=100,
            max_pages=20,
        )
        return len(ids)


    def check_registration_health(self, *, auto_fix: bool = True) -> dict[str, Any]:
        """Health-check local registration machine; optionally fix stuck sessions."""
        out: dict[str, Any] = {
            "ok": True,
            "checks": {},
            "issues": [],
            "fixes": [],
            "ts": time.time(),
        }
        try:
            ss = self.get_registration_sessions(limit=50, active_only=False)
            out["checks"]["sessions_api"] = True
            out["checks"]["available"] = ss.get("available")
            out["checks"]["captcha_provider"] = ss.get("captcha_provider")
            out["checks"]["local_solver_configured"] = ss.get("local_solver_configured")
            out["checks"]["engine"] = ss.get("engine")
            if ss.get("available") is False:
                out["ok"] = False
                out["issues"].append("registration adapter unavailable")
        except Exception as e:  # noqa: BLE001
            out["ok"] = False
            out["checks"]["sessions_api"] = False
            out["issues"].append(f"sessions api: {e}")
            ss = {"sessions": []}

        try:
            r = self.s.get(
                f"{self.base}/admin/api/accounts/register-email/config",
                headers=self._headers(),
                timeout=30,
            )
            r.raise_for_status()
            cfg = (r.json() or {}).get("config") or {}
            out["checks"]["mail_provider"] = cfg.get("mail_provider")
            out["checks"]["domain"] = cfg.get("domain") or cfg.get("cfmail_domain")
            out["checks"]["captcha_provider_cfg"] = cfg.get("captcha_provider")
            out["checks"]["local_solver_url"] = cfg.get("local_solver_url")
            key = (
                cfg.get("api_key")
                or cfg.get("cfmail_api_key")
                or cfg.get("moemail_api_key")
                or ""
            )
            key_ok = bool(str(key).strip())
            out["checks"]["api_key_present"] = key_ok
            if not key_ok:
                out["ok"] = False
                out["issues"].append("mail api_key missing/empty")
            prov = str(cfg.get("mail_provider") or "").lower()
            dom = cfg.get("domain") or cfg.get("cfmail_domain") or cfg.get("moemail_domain")
            if prov in {"cfmail", "moemail", ""} and not dom:
                out["issues"].append("mail domain empty")
            cap = str(
                cfg.get("captcha_provider")
                or ss.get("captcha_provider")
                or ""
            ).lower()
            out["checks"]["captcha"] = cap or "unknown"
        except Exception as e:  # noqa: BLE001
            out["ok"] = False
            out["issues"].append(f"config api: {e}")

        terminal = {
            "imported",
            "error",
            "done",
            "finished",
            "completed",
            "cancelled",
            "stopped",
            "success",
            "failed",
        }
        sessions = ss.get("sessions") or []
        active = [
            x
            for x in sessions
            if str(x.get("status") or "").lower() not in terminal
        ]
        stuck = []
        now = time.time()
        for x in active:
            age = now - float(x.get("updated_at") or x.get("created_at") or now)
            st = str(x.get("status") or "").lower()
            if age > 300 or (
                st in {"solving_turnstile", "stopping", "queued"} and age > 180
            ):
                stuck.append(
                    {"id": x.get("id"), "status": st, "age_s": int(age)}
                )
        out["checks"]["active_sessions"] = len(active)
        out["checks"]["stuck_sessions"] = len(stuck)
        if stuck:
            out["issues"].append(f"stuck registration sessions: {len(stuck)}")
            if auto_fix:
                try:
                    stop = self.stop_all_registrations()
                    out["fixes"].append(
                        {"action": "stop_all_registrations", "result": stop}
                    )
                    time.sleep(1.0)
                    ss2 = self.get_registration_sessions(limit=50, active_only=False)
                    active2 = [
                        x
                        for x in (ss2.get("sessions") or [])
                        if str(x.get("status") or "").lower() not in terminal
                    ]
                    out["checks"]["active_sessions_after_fix"] = len(active2)
                    max_active = int(self.cfg.get("register_max_active_sessions") or 8)
                    if len(active2) > max_active:
                        out["ok"] = False
                        out["issues"].append(
                            f"still too many active sessions after stop: {len(active2)}"
                        )
                except Exception as e:  # noqa: BLE001
                    out["ok"] = False
                    out["issues"].append(f"auto fix stop failed: {e}")

        if (
            out["checks"].get("local_solver_configured") is False
            and str(out["checks"].get("captcha") or "").lower() == "local"
        ):
            out["ok"] = False
            out["issues"].append("local solver not configured")

        # if only stuck issues and fixed, mark ok
        hard = [i for i in out["issues"] if "stuck registration sessions" not in i]
        if hard:
            out["ok"] = False
        elif out["ok"] is not False:
            out["ok"] = True

        out["message"] = (
            "registration healthy"
            if out["ok"]
            else ("registration unhealthy: " + "; ".join(out["issues"][:5]))
        )
        return out

    def start_registration(self, count: int | None = None) -> dict[str, Any]:
        # Health gate: stop leftover sessions / refuse when too many active
        if self.cfg.get("register_health_gate", True):
            try:
                gate = self.ensure_register_capacity()
                if not gate.get("ok"):
                    raise RuntimeError(gate.get("message") or "register health gate blocked")
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("register health gate skipped: %s", e)

        # Pull saved registration config and inject secrets into start body.
        # Avoid empty overrides that can wipe stored keys on older backends.
        body = dict(self.cfg.get("register_body") or {})
        try:
            saved = self.s.get(
                f"{self.base}/admin/api/accounts/register-email/config",
                headers=self._headers(),
                timeout=30,
            )
            if saved.status_code == 200:
                sc = (saved.json() or {}).get("config") or {}
                # only fill missing keys; never send empty secret placeholders
                for k in (
                    "mail_provider",
                    "base_url",
                    "domain",
                    "cfmail_domain",
                    "captcha_provider",
                    "local_solver_url",
                    "proxy",
                    "proxy_username",
                    "proxy_password",
                    "expiry_ms",
                ):
                    if k not in body or body.get(k) in (None, ""):
                        if sc.get(k) not in (None, ""):
                            body[k] = sc.get(k)
                # secrets: only if look unmasked and non-empty
                for k in ("api_key", "cfmail_api_key", "moemail_api_key", "yescaptcha_key"):
                    v = sc.get(k)
                    if not v or not isinstance(v, str):
                        continue
                    if "…" in v or v == "****" or set(v) <= {"*"}:
                        continue
                    if k not in body or body.get(k) in (None, ""):
                        body[k] = v
        except Exception as e:  # noqa: BLE001
            log.warning("load register config for start body failed: %s", e)

        n = int(count if count is not None else (self.cfg.get("register_count") or 8))
        n = max(1, min(n, 25))  # hard safety cap per batch (local turnstile)
        body["count"] = n
        # sweet spot: 2 is usually faster+stabler than 3~5 under local camoufox
        body["concurrency"] = max(1, min(5, int(self.cfg.get("register_concurrency") or 4)))
        body["stagger_ms"] = int(self.cfg.get("register_stagger_ms") or 700)
        body["probe_delay_sec"] = int(self.cfg.get("register_probe_delay_sec") or 20)
        # force local turnstile — mis-set yescaptcha causes long timeouts & "no progress"
        body["captcha_provider"] = "local"
        body["local_solver_url"] = (
            body.get("local_solver_url")
            or "http://127.0.0.1:5072"
        )
        # clear fake yescaptcha key "local" which confuses provider fallbacks
        if str(body.get("yescaptcha_key") or "").strip().lower() in {"", "local"}:
            body.pop("yescaptcha_key", None)

        r = self.s.post(
            f"{self.base}/admin/api/accounts/register-email",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
        if r.status_code >= 400:
            log.error("register start failed %s: %s", r.status_code, r.text[:600])
        r.raise_for_status()
        return r.json()

    def get_registration_sessions(self, *, limit: int = 60, active_only: bool = False) -> dict[str, Any]:
        r = self.s.get(
            f"{self.base}/admin/api/accounts/register-email/sessions",
            params={"limit": limit, "active_only": 1 if active_only else 0},
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def stop_all_registrations(self) -> dict[str, Any]:
        r = self.s.post(
            f"{self.base}/admin/api/accounts/register-email/stop",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={},
            timeout=60,
        )
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "body": r.text[:300]}
        return r.json() if r.content else {"ok": True}

    def ensure_register_capacity(self) -> dict[str, Any]:
        """If too many active reg sessions, stop leftovers; block if still overloaded."""
        terminal = {
            "imported",
            "error",
            "done",
            "finished",
            "completed",
            "cancelled",
            "stopped",
            "success",
            "failed",
        }
        data = self.get_registration_sessions(limit=120, active_only=False)
        sessions = data.get("sessions") or []
        active = [
            s
            for s in sessions
            if str(s.get("status") or "").lower() not in terminal
        ]
        max_active = int(self.cfg.get("register_max_active_sessions") or 8)
        stuck = []
        now = time.time()
        for s in active:
            age = now - float(s.get("updated_at") or s.get("created_at") or now)
            st = str(s.get("status") or "").lower()
            # long-lived solving/stopping is almost always dead weight
            if age > 600 or st in {"stopping", "solving_turnstile"} and age > 300:
                stuck.append(s)

        if stuck or len(active) > max_active:
            log.warning(
                "本地注册过载/卡住: active=%s stuck=%s → 请求 stop-all",
                len(active),
                len(stuck),
            )
            stop_res = self.stop_all_registrations()
            time.sleep(2)
            data2 = self.get_registration_sessions(limit=80, active_only=True)
            active2 = data2.get("sessions") or []
            # API may ignore active_only on old builds
            active2 = [
                s
                for s in (data2.get("sessions") or sessions)
                if str(s.get("status") or "").lower() not in terminal
            ]
            if len(active2) > max_active:
                return {
                    "ok": False,
                    "message": (
                        f"本地注册仍过载 active={len(active2)}>{max_active}，"
                        f"已 stop-all={stop_res.get('ok')}；跳过本轮注册以免卡死后台"
                    ),
                    "active": len(active2),
                    "stop": stop_res,
                }
            return {
                "ok": True,
                "message": f"已清理注册会话，active≈{len(active2)}",
                "active": len(active2),
                "stop": stop_res,
            }
        return {"ok": True, "active": len(active), "message": "register capacity ok"}

    def get_registration_batch(self, batch_id: str) -> dict[str, Any]:
        r = self.s.get(
            f"{self.base}/admin/api/accounts/register-email/batches/{batch_id}",
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def wait_registration(
        self,
        batch_id: str,
        *,
        timeout_sec: int = 1800,
        poll_sec: int = 8,
        min_ok: int | None = None,
        stop_when_enough: bool = True,
    ) -> dict[str, Any]:
        """Poll batch until terminal, timeout, or enough successes (early stop)."""
        terminal = {
            "done",
            "finished",
            "completed",
            "success",
            "error",
            "failed",
            "cancelled",
            "stopped",
            "partial",
        }
        started = time.time()
        last: dict[str, Any] = {}
        enough_n = int(min_ok) if min_ok is not None else 0
        while time.time() - started < timeout_sec:
            try:
                last = self.get_registration_batch(batch_id)
            except Exception as e:  # noqa: BLE001
                log.warning("poll batch %s: %s", batch_id, e)
                time.sleep(poll_sec)
                continue
            st = str(last.get("status") or last.get("batch_status") or "").lower()
            finished = int(last.get("finished") or last.get("done") or 0)
            count = int(last.get("count") or last.get("total") or 0)
            ok_c = int(last.get("ok_count") or last.get("imported") or 0)
            fail_c = int(last.get("fail_count") or 0)
            inflight = last.get("inflight")
            log.info(
                "注册批次 %s status=%s finished=%s/%s ok=%s fail=%s inflight=%s",
                batch_id,
                st,
                finished,
                count,
                ok_c,
                fail_c,
                inflight,
            )
            if self._looks_rate_limited(last):
                last["rate_limited"] = True
            # Early success: enough imported accounts — stop waiting on slow failures
            if stop_when_enough and enough_n > 0 and ok_c >= enough_n:
                last["early_stop"] = True
                last["early_stop_reason"] = f"ok_count {ok_c} >= min_ok {enough_n}"
                log.info(
                    "注册批次 %s 已够数 ok=%s>=%s，提前结束等待并停止剩余会话",
                    batch_id,
                    ok_c,
                    enough_n,
                )
                try:
                    # stop leftover workers so next cycle can start faster
                    self.s.post(
                        f"{self.base}/admin/api/accounts/register-email/batches/"
                        f"{batch_id}/stop",
                        headers={**self._headers(), "Content-Type": "application/json"},
                        json={},
                        timeout=30,
                    )
                except Exception:
                    try:
                        self.stop_all_registrations()
                    except Exception:
                        pass
                return last
            if st in terminal or (
                count > 0
                and finished >= count
                and st not in ("running", "starting", "queued", "spawning", "stopping")
            ):
                return last
            if last.get("runner_alive") is False and count and finished >= count:
                return last
            # if all remaining look doomed (high fail, no inflight) end early
            if (
                count > 0
                and inflight == 0
                and finished >= count
            ):
                return last
            time.sleep(max(3, poll_sec))
        last["timeout"] = True
        return last

    def _looks_rate_limited(self, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("rate_limited"):
            return True
        parts: list[str] = []
        for k in ("message", "error", "detail"):
            if payload.get(k) is not None:
                parts.append(str(payload.get(k)))
        se = payload.get("spawn_errors")
        if isinstance(se, list):
            parts.extend(str(x) for x in se)
        elif se:
            parts.append(str(se))
        for s in payload.get("sessions") or []:
            if isinstance(s, dict):
                parts.append(str(s.get("error") or ""))
                parts.append(str(s.get("message") or ""))
        text = " ".join(parts).lower()
        return (
            "rate_limited" in text
            or "rate limit" in text
            or "error=rate_limited" in text
        )

    def ensure_local_pool(self, need_export: int) -> dict[str, Any]:
        """If exportable accounts < need, register with rate-limit backoff."""
        out: dict[str, Any] = {
            "exportable_before": 0,
            "registered": False,
            "batch_id": None,
            "batch": None,
            "attempts": [],
        }
        if not self.cfg.get("auto_register", True):
            out["skipped"] = "auto_register=false"
            return out

        if self.cfg.get("register_health_check", True):
            try:
                health = self.check_registration_health(auto_fix=True)
                out["health"] = {
                    "ok": health.get("ok"),
                    "message": health.get("message"),
                    "issues": health.get("issues"),
                    "fixes": health.get("fixes"),
                    "active": (health.get("checks") or {}).get("active_sessions"),
                    "stuck": (health.get("checks") or {}).get("stuck_sessions"),
                }
                if not health.get("ok"):
                    out["message"] = health.get("message") or "registration unhealthy"
                    log.error("注册机自检失败: %s", out["message"])
                    return out
                log.info("注册机自检通过: %s", health.get("message"))
            except Exception as e:  # noqa: BLE001
                log.warning("注册机自检异常(继续尝试注册): %s", e)

        scan_limit = max(need_export + 20, int(self.cfg.get("min_local_active") or 30) + 10, 50)
        exportable = self.count_exportable(limit=scan_limit)
        out["exportable_before"] = exportable
        min_local = int(self.cfg.get("min_local_active") or 0)
        threshold = max(need_export, min_local)
        if exportable >= threshold:
            out["message"] = f"本地可导出 {exportable} >= {threshold}"
            return out

        # keep batches moderate for local turnstile (faster than 1, stabler than 15x3)
        base_count = max(1, min(int(self.cfg.get("register_count") or 16), 25))
        deficit = max(1, threshold - exportable)
        reg_n = min(base_count, max(deficit, 4))
        retries = max(1, int(self.cfg.get("register_rate_limit_retries") or 3))
        backoff = max(30, int(self.cfg.get("register_rate_limit_backoff_sec") or 60))
        # stop waiting once we have enough for this cycle (+small buffer)
        min_ok_target = max(1, min(deficit, reg_n, int(self.cfg.get("register_early_stop_ok") or deficit)))

        for attempt in range(1, retries + 1):
            # on retry after rate limit / high fail: smaller batch + lower concurrency temporarily
            if attempt == 1:
                this_n = reg_n
            elif attempt == 2:
                this_n = max(2, min(4, reg_n))
            else:
                this_n = 1
            log.info(
                "本地可导出不足 (%s < %s)，注册 attempt %s/%s ×%s (min_ok≈%s)",
                exportable,
                threshold,
                attempt,
                retries,
                this_n,
                min_ok_target,
            )
            try:
                started = self.start_registration(this_n)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                out["attempts"].append({"attempt": attempt, "error": msg[:300]})
                if "mail api key" in msg.lower():
                    out["message"] = msg
                    raise
                if attempt < retries and (
                    "rate_limited" in msg.lower() or "rate limit" in msg.lower() or "400" in msg
                ):
                    log.warning("注册启动失败: %s；%ss 后重试", msg[:160], backoff * attempt)
                    interruptible_sleep(backoff * attempt)
                    continue
                raise

            batch_id = started.get("batch_id") or (started.get("batch") or {}).get("id")
            sid = started.get("id") or started.get("session_id")
            out["registered"] = True
            out["batch_id"] = batch_id
            out["register_count"] = this_n
            out["start"] = {
                k: started.get(k)
                for k in ("ok", "count", "concurrency", "message", "batch_id", "id", "status")
            }
            out["attempts"].append(
                {
                    "attempt": attempt,
                    "count": this_n,
                    "batch_id": batch_id,
                    "session_id": sid,
                }
            )

            # single-session path
            if not batch_id and sid:
                deadline = time.time() + int(self.cfg.get("register_timeout_sec") or 1800)
                final_sess: dict[str, Any] = {}
                while time.time() < deadline:
                    try:
                        rr = self.s.get(
                            f"{self.base}/admin/api/accounts/register-email/sessions/{sid}",
                            headers=self._headers(),
                            timeout=30,
                        )
                        if rr.status_code < 400:
                            final_sess = rr.json() if rr.content else {}
                            st = str(final_sess.get("status") or "").lower()
                            log.info("注册会话 %s status=%s", sid, st)
                            if self._looks_rate_limited(final_sess):
                                final_sess["rate_limited"] = True
                                break
                            if st in {
                                "imported",
                                "error",
                                "failed",
                                "cancelled",
                                "stopped",
                                "done",
                                "success",
                                "completed",
                            }:
                                break
                    except Exception as e:  # noqa: BLE001
                        log.warning("poll session %s: %s", sid, e)
                    time.sleep(8)
                out["session"] = {
                    "id": sid,
                    "status": final_sess.get("status"),
                    "error": final_sess.get("error"),
                    "message": final_sess.get("message"),
                    "rate_limited": bool(final_sess.get("rate_limited"))
                    or self._looks_rate_limited(final_sess),
                }
                if out["session"]["rate_limited"] and attempt < retries:
                    log.warning(
                        "检测到 rate_limited（session），退避 %ss 后重试…",
                        backoff * attempt,
                    )
                    interruptible_sleep(backoff * attempt)
                    continue
                break

            if not batch_id:
                log.warning("未返回 batch_id/session_id raw_keys=%s", list(started)[:20])
                break

            batch = self.wait_registration(
                str(batch_id),
                timeout_sec=int(self.cfg.get("register_timeout_sec") or 900),
                poll_sec=int(self.cfg.get("register_poll_interval_sec") or 8),
                min_ok=max(1, min(min_ok_target, this_n)),
                stop_when_enough=True,
            )
            ok_c = int(batch.get("ok_count") or batch.get("imported") or 0)
            fail_c = int(batch.get("fail_count") or 0)
            rate_hit = self._looks_rate_limited(batch)
            early = bool(batch.get("early_stop"))
            out["batch"] = {
                "status": batch.get("status") or batch.get("batch_status"),
                "finished": batch.get("finished"),
                "ok_count": ok_c,
                "fail_count": fail_c,
                "timeout": batch.get("timeout"),
                "rate_limited": rate_hit,
                "early_stop": early,
                "early_stop_reason": batch.get("early_stop_reason"),
                "spawn_errors": (batch.get("spawn_errors") or [])[:5],
                "message": batch.get("message"),
            }
            # enough successes — proceed to export even if batch not fully finished
            if ok_c >= max(1, min(min_ok_target, this_n)) or early:
                log.info("注册已获得足够成功账号 ok=%s，进入 settle/导出", ok_c)
                break
            if rate_hit and ok_c <= 0 and attempt < retries:
                log.warning(
                    "批次 rate_limited 且 ok=0，退避 %ss 后以更小批量重试…",
                    backoff * attempt,
                )
                interruptible_sleep(backoff * attempt)
                continue
            # high fail ratio with zero success -> retry smaller
            if ok_c == 0 and fail_c >= max(2, this_n // 2) and attempt < retries:
                log.warning(
                    "注册成功率过低 ok=%s fail=%s，退避 %ss 后重试…",
                    ok_c,
                    fail_c,
                    backoff * attempt,
                )
                interruptible_sleep(backoff * attempt)
                continue
            break

        settle = int(self.cfg.get("register_settle_sec") or 15)
        log.info("注册结束，等待 settle %ss 以便 probe 完成…", settle)
        interruptible_sleep(max(0, settle))
        out["exportable_after"] = self.count_exportable(limit=scan_limit)
        out["message"] = (
            f"注册结束 exportable {out['exportable_before']}→{out.get('exportable_after')}"
        )
        return out


class TargetClient:
    """Remote sub2api admin client."""

    def __init__(self, cfg: dict[str, Any], session: requests.Session | None = None):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.api = f"{self.base}/api/v1"
        self.s = session or requests.Session()
        self.token = (cfg.get("access_token") or "").strip()

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def login(self) -> None:
        if self.token:
            r = self.s.get(
                f"{self.api}/auth/me",
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code == 200:
                return
            # some deployments use /user
            r2 = self.s.get(f"{self.api}/user", headers=self._headers(), timeout=30)
            if r2.status_code == 200:
                return
            log.warning("target token invalid (%s), try password login", r.status_code)
            self.token = ""
        email = self.cfg.get("email") or ""
        password = self.cfg.get("password") or ""
        if not email or not password:
            raise RuntimeError(
                "target.access_token 或 (target.email + target.password) 必须配置"
            )
        r = self.s.post(
            f"{self.api}/auth/login",
            json={"email": email, "password": password},
            timeout=30,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "远程管理员账号/密码错误 (401)。请检查 target.email / target.password，"
                "或改用登录后浏览器 localStorage 的 access_token 填到 target.access_token"
            )
        if r.status_code >= 400:
            raise RuntimeError(
                f"远程登录失败 HTTP {r.status_code}: {(r.text or '')[:300]}"
            )
        data = r.json()
        # unwrap {code, data} style
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]
        token = data.get("access_token") or data.get("token")
        if not token:
            raise RuntimeError(f"target login failed: {data}")
        self.token = str(token)
        log.info("target login ok")

    def _unwrap(self, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "data" in data
            and data.get("code") in (0, None, "0", 200)
        ):
            return data["data"]
        return data

    def resolve_group_id(self) -> int | None:
        gid = self.cfg.get("group_id")
        if gid is not None and str(gid).strip() != "":
            return int(gid)
        name = (self.cfg.get("group_name") or "").strip()
        if not name:
            return None
        platform = self.cfg.get("platform") or "grok"
        r = self.s.get(
            f"{self.api}/admin/groups/all",
            params={"platform": platform},
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code == 404:
            r = self.s.get(
                f"{self.api}/admin/groups/all",
                headers=self._headers(),
                timeout=30,
            )
        r.raise_for_status()
        data = self._unwrap(r.json())
        groups = data if isinstance(data, list) else (data.get("items") or data.get("groups") or [])
        name_l = name.lower()
        for g in groups:
            if not isinstance(g, dict):
                continue
            gname = str(g.get("name") or g.get("group_name") or "").strip()
            if gname.lower() == name_l or gname == name:
                return int(g.get("id"))
        # partial match
        for g in groups:
            if not isinstance(g, dict):
                continue
            gname = str(g.get("name") or g.get("group_name") or "").strip()
            if name_l in gname.lower():
                log.info("group partial match: %s (id=%s)", gname, g.get("id"))
                return int(g.get("id"))
        raise RuntimeError(
            f"找不到分组 name={name!r} platform={platform}. "
            f"可用: {[ (x.get('id'), x.get('name')) for x in groups[:30] if isinstance(x, dict) ]}"
        )

    def count_group_accounts(self, group_id: int | None) -> int:
        """Count accounts in group (prefer status=active if supported)."""
        params: dict[str, Any] = {
            "page": 1,
            "page_size": 1,
            "platform": self.cfg.get("platform") or "grok",
        }
        if group_id is not None:
            params["group"] = str(group_id)
        # try active only first
        for status in ("active", ""):
            p = dict(params)
            if status:
                p["status"] = status
            r = self.s.get(
                f"{self.api}/admin/accounts",
                params=p,
                headers=self._headers(),
                timeout=60,
            )
            if r.status_code == 400 and status:
                continue
            r.raise_for_status()
            data = self._unwrap(r.json())
            if not isinstance(data, dict):
                continue
            for key in ("total", "total_count", "count"):
                if data.get(key) is not None:
                    return int(data[key])
            # pagination wrappers
            pag = data.get("pagination") or data.get("meta") or {}
            if isinstance(pag, dict) and pag.get("total") is not None:
                return int(pag["total"])
        raise RuntimeError("无法解析远程账号总数")

    def import_data(
        self,
        sub2_doc: dict[str, Any],
        *,
        skip_default_group_bind: bool = True,
    ) -> dict[str, Any]:
        # strip internal meta
        payload = {
            "exported_at": sub2_doc.get("exported_at"),
            "proxies": sub2_doc.get("proxies") or [],
            "accounts": sub2_doc.get("accounts") or [],
        }
        body = {
            "data": payload,
            "skip_default_group_bind": skip_default_group_bind,
        }
        r = self.s.post(
            f"{self.api}/admin/accounts/data",
            headers=self._headers(),
            json=body,
            timeout=300,
        )
        if r.status_code >= 400:
            log.error("import failed %s: %s", r.status_code, r.text[:800])
        r.raise_for_status()
        return self._unwrap(r.json()) if r.content else {}

    def bulk_bind_group(
        self,
        account_ids: list[int],
        group_id: int,
    ) -> dict[str, Any]:
        if not account_ids:
            return {"success": 0}
        body = {
            "account_ids": account_ids,
            "group_ids": [group_id],
        }
        r = self.s.post(
            f"{self.api}/admin/accounts/bulk-update",
            headers=self._headers(),
            json=body,
            timeout=180,
        )
        if r.status_code >= 400:
            # try alternate shape
            r2 = self.s.post(
                f"{self.api}/admin/accounts/bulk-update",
                headers=self._headers(),
                json={"account_ids": account_ids, "updates": {"group_ids": [group_id]}},
                timeout=180,
            )
            if r2.status_code < 400:
                return self._unwrap(r2.json()) if r2.content else {}
            log.error("bulk bind failed: %s %s", r.status_code, r.text[:500])
            r.raise_for_status()
        return self._unwrap(r.json()) if r.content else {}

    def list_group_accounts(
        self,
        group_id: int | None,
        *,
        status: str = "active",
        page_size: int = 100,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            params: dict[str, Any] = {
                "page": page,
                "page_size": page_size,
                "platform": self.cfg.get("platform") or "grok",
            }
            if group_id is not None:
                params["group"] = str(group_id)
            if status:
                params["status"] = status
            r = self.s.get(
                f"{self.api}/admin/accounts",
                params=params,
                headers=self._headers(),
                timeout=60,
            )
            r.raise_for_status()
            data = self._unwrap(r.json())
            rows: list[Any] = []
            total = None
            total_pages = None
            if isinstance(data, dict):
                rows = (
                    data.get("items")
                    or data.get("accounts")
                    or data.get("data")
                    or []
                )
                total = data.get("total") or data.get("total_count")
                total_pages = data.get("total_pages") or data.get("pages")
                pag = data.get("pagination") or data.get("meta") or {}
                if isinstance(pag, dict):
                    total = total or pag.get("total")
                    total_pages = total_pages or pag.get("total_pages") or pag.get("pages")
            elif isinstance(data, list):
                rows = data
            if not rows:
                break
            for row in rows:
                if isinstance(row, dict):
                    items.append(row)
            if total_pages and page >= int(total_pages):
                break
            if total is not None and len(items) >= int(total):
                break
            if len(rows) < page_size:
                break
            page += 1
        return items

    def test_account(self, account_id: int) -> dict[str, Any]:
        r = self.s.post(
            f"{self.api}/admin/accounts/{account_id}/test",
            headers=self._headers(),
            timeout=90,
        )
        http_status = r.status_code
        body: Any
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
        data = self._unwrap(body) if isinstance(body, dict) else body
        success = None
        message = ""
        if isinstance(data, dict):
            if "success" in data:
                success = bool(data.get("success"))
            elif "ok" in data:
                success = bool(data.get("ok"))
            message = str(data.get("message") or data.get("error") or "")
        # 你要求的：状态码不是 200 → 判失败
        ok = http_status == 200 and (success is not False)
        if success is False:
            ok = False
        return {
            "ok": ok,
            "http_status": http_status,
            "success": success,
            "message": message,
            "raw": data if isinstance(data, dict) else {"value": data},
        }

    def delete_account(self, account_id: int) -> dict[str, Any]:
        r = self.s.delete(
            f"{self.api}/admin/accounts/{account_id}",
            headers=self._headers(),
            timeout=60,
        )
        try:
            body = r.json() if r.content else {}
        except Exception:
            body = {"raw": r.text[:300]}
        return {
            "http_status": r.status_code,
            "ok": r.status_code in (200, 204),
            "body": self._unwrap(body) if isinstance(body, dict) else body,
        }


    def _account_unusable_reason(self, acc: dict[str, Any]) -> str | None:
        """Heuristic: account clearly unusable from list metadata."""
        if not isinstance(acc, dict):
            return None
        status = str(acc.get("status") or "").lower()
        if status in {"inactive", "error", "expired", "disabled", "quota_exhausted", "banned", "invalid"}:
            return f"status={status}"
        if acc.get("schedulable") is False:
            return "not_schedulable"
        err = acc.get("error_message") or acc.get("last_error") or acc.get("error")
        if err:
            msg = str(err).strip()
            if msg:
                return f"error_message:{msg[:80]}"
        exp = acc.get("expires_at")
        try:
            if isinstance(exp, (int, float)):
                ts = float(exp)
                if ts > 1e12:
                    ts /= 1000.0
                if ts and ts < time.time() - 3600:
                    return "expires_at_past"
        except Exception:
            pass
        return None

    def cleanup_unusable_metadata(
        self,
        group_id: int | None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete accounts clearly bad by metadata (no live test needed)."""
        max_n = max(0, int(self.cfg.get("cleanup_metadata_max") or 500))
        del_unsched = bool(self.cfg.get("cleanup_delete_unschedulable", True))
        del_errmsg = bool(self.cfg.get("cleanup_delete_error_message", True))
        stats: dict[str, Any] = {
            "scanned": 0,
            "candidates": 0,
            "deleted": 0,
            "delete_failed": 0,
            "items": [],
            "dry_run": dry_run,
        }
        if max_n <= 0:
            return stats

        pages: list[dict[str, Any]] = []
        for status in ("active", ""):
            try:
                for page in range(1, 81):  # full-ish scan
                    rows = self._list_group_page(
                        group_id, status=status, page=page, page_size=50
                    )
                    if not rows:
                        break
                    pages.extend(rows)
                    if len(pages) >= 5000:
                        break
            except Exception as e:  # noqa: BLE001
                log.debug("metadata scan status=%s failed: %s", status, e)

        seen: set[int] = set()
        accounts: list[dict[str, Any]] = []
        for a in pages:
            if not isinstance(a, dict) or a.get("id") is None:
                continue
            aid = int(a["id"])
            if aid in seen:
                continue
            seen.add(aid)
            accounts.append(a)
        stats["scanned"] = len(accounts)

        for acc in accounts:
            reason = self._account_unusable_reason(acc)
            if not reason:
                continue
            if reason == "not_schedulable" and not del_unsched:
                continue
            if reason.startswith("error_message") and not del_errmsg:
                continue
            stats["candidates"] += 1
            aid = int(acc["id"])
            name = acc.get("name") or acc.get("email") or aid
            item: dict[str, Any] = {"id": aid, "name": name, "reason": reason}
            if dry_run:
                stats["items"].append(item)
                if stats["candidates"] >= max_n:
                    break
                continue
            d = self.delete_account(aid)
            if d.get("ok"):
                stats["deleted"] += 1
                item["deleted"] = True
                log.info("元数据清理删除 id=%s name=%s reason=%s", aid, name, reason)
            else:
                stats["delete_failed"] += 1
                item["deleted"] = False
                log.warning("元数据清理删除失败 id=%s %s", aid, d)
            stats["items"].append(item)
            if stats["candidates"] >= max_n:
                break

        log.info(
            "元数据清理: scanned=%s candidates=%s deleted=%s failed=%s dry_run=%s",
            stats["scanned"],
            stats["candidates"],
            stats["deleted"],
            stats["delete_failed"],
            dry_run,
        )
        return stats

    def _pick_cleanup_batch(
        self,
        group_id: int | None,
        *,
        max_n: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Pick a batch via prefer-status + page-based cursor (no full-pool list)."""
        meta: dict[str, Any] = {"mode": "paged", "cursor": 0, "pool_size": None}
        prefer = list(self.cfg.get("cleanup_prefer_status") or [])
        selected: list[dict[str, Any]] = []
        seen: set[int] = set()
        page_size = max(20, min(100, int(self.cfg.get("cleanup_page_size") or 100)))

        def _add(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if not isinstance(row, dict) or row.get("id") is None:
                    continue
                aid = int(row["id"])
                if aid in seen:
                    continue
                seen.add(aid)
                selected.append(row)

        # 1) prefer bad statuses first (up to half batch)
        prefer_budget = max(1, max_n // 2) if prefer else 0
        for st in prefer:
            if len(selected) >= prefer_budget:
                break
            try:
                rows = self.list_group_accounts(
                    group_id,
                    status=str(st),
                    page_size=min(page_size, prefer_budget),
                    max_pages=1,
                )
                _add(rows)
            except Exception as e:  # noqa: BLE001
                log.debug("prefer status %s skipped: %s", st, e)

        # 2) page-based cursor on active/all pool
        status = "active" if self.cfg.get("cleanup_active_only", True) else ""
        cursor = int(self.cfg.get("cleanup_cursor") or get_cleanup_cursor() or 0)
        if cursor < 0:
            cursor = 0
        page = cursor // page_size + 1
        need = max(0, max_n - len(selected))
        pages_to_fetch = max(1, (need + page_size - 1) // page_size)

        # probe total via first page
        first = self.list_group_accounts(
            group_id, status=status, page_size=page_size, max_pages=1
        )
        # re-fetch desired page window
        pool_chunk: list[dict[str, Any]] = []
        # If cursor page beyond end, wrap
        # We don't always know total; use empty page as wrap signal
        for i in range(pages_to_fetch + 1):  # +1 for wrap retry
            p = page + i
            rows = self.list_group_accounts(
                group_id, status=status, page_size=page_size, max_pages=1
            )
            # list_group_accounts always starts page=1; need a paged variant
            rows = self._list_group_page(
                group_id, status=status, page=p, page_size=page_size
            )
            if not rows:
                # wrap to start
                if p > 1:
                    page = 1
                    rows = self._list_group_page(
                        group_id, status=status, page=1, page_size=page_size
                    )
                if not rows:
                    break
            pool_chunk.extend(rows)
            if len(pool_chunk) >= need:
                break

        # fallback: if paged helper unavailable, use first page only
        if not pool_chunk and first:
            pool_chunk = first

        _add(pool_chunk[:need])
        advanced = max(1, min(need, len(pool_chunk) or 1))
        # approximate next cursor as offset in stream
        next_cursor = cursor + advanced
        # soft wrap every ~50 pages to avoid unbounded growth
        if next_cursor > page_size * 50:
            next_cursor = 0
        meta["cursor"] = cursor
        meta["next_cursor"] = next_cursor
        meta["page"] = page
        meta["page_size"] = page_size
        meta["batch_size"] = len(selected[:max_n] if max_n > 0 else selected)
        self.cfg["cleanup_cursor"] = next_cursor
        if max_n > 0 and len(selected) > max_n:
            selected = selected[:max_n]
        return selected, meta

    def _list_group_page(
        self,
        group_id: int | None,
        *,
        status: str,
        page: int,
        page_size: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": max(1, int(page)),
            "page_size": max(1, int(page_size)),
            "platform": self.cfg.get("platform") or "grok",
        }
        if group_id is not None:
            params["group"] = str(group_id)
        if status:
            params["status"] = status
        r = self.s.get(
            f"{self.api}/admin/accounts",
            params=params,
            headers=self._headers(),
            timeout=60,
        )
        if r.status_code == 400 and status:
            params.pop("status", None)
            r = self.s.get(
                f"{self.api}/admin/accounts",
                params=params,
                headers=self._headers(),
                timeout=60,
            )
        r.raise_for_status()
        data = self._unwrap(r.json())
        if isinstance(data, dict):
            rows = (
                data.get("items")
                or data.get("accounts")
                or data.get("data")
                or []
            )
            return [x for x in rows if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []

    def cleanup_bad_accounts(
        self,
        group_id: int | None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Test a rotating batch; delete only after consecutive failures."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_n = int(self.cfg.get("cleanup_max") or 0)
        if max_n <= 0:
            max_n = 200
            log.warning("cleanup_max<=0，已自动限制为 200/轮（避免全量测活）")

        # seed cursor from state
        self.cfg["cleanup_cursor"] = int(
            self.cfg.get("cleanup_cursor") or get_cleanup_cursor() or 0
        )
        accounts, pick_meta = self._pick_cleanup_batch(group_id, max_n=max_n)
        workers = max(1, min(16, int(self.cfg.get("cleanup_workers") or 6)))
        delay = float(self.cfg.get("cleanup_delay_sec") or 0)
        do_delete = bool(self.cfg.get("cleanup_fail_delete", True))
        need_streak = max(1, int(self.cfg.get("cleanup_fail_streak") or 2))
        timeout = float(self.cfg.get("cleanup_timeout_sec") or 45)
        streaks = get_fail_streaks()

        stats: dict[str, Any] = {
            "total": len(accounts),
            "pool_size": pick_meta.get("pool_size"),
            "cursor": pick_meta.get("cursor"),
            "next_cursor": pick_meta.get("next_cursor"),
            "page": pick_meta.get("page"),
            "tested": 0,
            "ok": 0,
            "bad": 0,
            "deleted": 0,
            "delete_failed": 0,
            "errors": 0,
            "soft_bad": 0,  # failed but not yet deleted (streak)
            "bad_ids": [],
            "dry_run": dry_run,
            "fail_streak_need": need_streak,
        }

        log.info(
            "远程清理: group_id=%s 本批=%s cursor=%s→%s page=%s workers=%s streak=%s dry_run=%s",
            group_id,
            len(accounts),
            pick_meta.get("cursor"),
            pick_meta.get("next_cursor"),
            pick_meta.get("page"),
            workers,
            need_streak,
            dry_run,
        )
        if not accounts:
            if pick_meta.get("next_cursor") is not None:
                set_cleanup_cursor(int(pick_meta["next_cursor"]))
            return stats

        def _work(acc: dict[str, Any]) -> dict[str, Any]:
            aid = acc.get("id")
            name = acc.get("name") or acc.get("email") or aid
            if aid is None:
                return {"skip": True}
            try:
                if delay > 0:
                    time.sleep(delay)
                r = self.s.post(
                    f"{self.api}/admin/accounts/{int(aid)}/test",
                    headers=self._headers(),
                    timeout=timeout,
                )
                http_status = r.status_code
                # transient server errors: do not count as account-bad
                if http_status in (429, 500, 502, 503, 504):
                    return {
                        "id": int(aid),
                        "name": name,
                        "transient": True,
                        "http_status": http_status,
                    }
                try:
                    body = r.json()
                except Exception:
                    body = {"raw": r.text[:500]}
                data = self._unwrap(body) if isinstance(body, dict) else body
                success = None
                message = ""
                if isinstance(data, dict):
                    if "success" in data:
                        success = bool(data.get("success"))
                    elif "ok" in data:
                        success = bool(data.get("ok"))
                    message = str(data.get("message") or data.get("error") or "")
                ok = http_status == 200 and success is not False
                if success is False:
                    ok = False
                return {
                    "id": int(aid),
                    "name": name,
                    "test": {
                        "ok": ok,
                        "http_status": http_status,
                        "success": success,
                        "message": message,
                    },
                }
            except Exception as e:  # noqa: BLE001
                # network/timeout → soft error, no streak
                return {
                    "id": int(aid) if aid is not None else None,
                    "name": name,
                    "error": str(e)[:300],
                    "transient": True,
                }

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cleanup") as ex:
            futs = [ex.submit(_work, a) for a in accounts]
            for fut in as_completed(futs):
                results.append(fut.result())

        for res in results:
            if res.get("skip"):
                continue
            if res.get("transient") or res.get("error"):
                stats["errors"] += 1
                if res.get("error"):
                    log.warning("测活异常 id=%s: %s", res.get("id"), res.get("error"))
                continue
            stats["tested"] += 1
            test = res.get("test") or {}
            aid = res.get("id")
            key = str(aid)
            if test.get("ok"):
                stats["ok"] += 1
                if key in streaks:
                    streaks.pop(key, None)
            else:
                stats["bad"] += 1
                streaks[key] = int(streaks.get(key) or 0) + 1
                streak_n = streaks[key]
                stats["bad_ids"].append(
                    {
                        "id": aid,
                        "name": res.get("name"),
                        "http_status": test.get("http_status"),
                        "message": test.get("message"),
                        "streak": streak_n,
                    }
                )
                log.warning(
                    "坏号 id=%s name=%s http=%s streak=%s/%s msg=%s",
                    aid,
                    res.get("name"),
                    test.get("http_status"),
                    streak_n,
                    need_streak,
                    (test.get("message") or "")[:120],
                )
                if do_delete and aid is not None and streak_n >= need_streak:
                    if dry_run:
                        stats["deleted"] += 1
                        continue
                    d = self.delete_account(int(aid))
                    if d.get("ok"):
                        stats["deleted"] += 1
                        streaks.pop(key, None)
                        log.info("已删除坏号 id=%s (streak=%s)", aid, streak_n)
                    else:
                        stats["delete_failed"] += 1
                        log.error("删除失败 id=%s %s", aid, d)
                else:
                    stats["soft_bad"] += 1

        if not dry_run:
            set_fail_streaks(streaks)
            if pick_meta.get("next_cursor") is not None:
                set_cleanup_cursor(int(pick_meta["next_cursor"]))

        log.info(
            "远程清理完成: tested=%s ok=%s bad=%s soft_bad=%s deleted=%s errors=%s cursor→%s",
            stats["tested"],
            stats["ok"],
            stats["bad"],
            stats["soft_bad"],
            stats["deleted"],
            stats["errors"],
            stats.get("next_cursor"),
        )
        return stats

    def list_recent_account_ids(
        self,
        *,
        group_id: int | None,
        limit: int,
        search_emails: list[str] | None = None,
    ) -> list[int]:
        """Best-effort: find newly imported account ids by email search."""
        found: list[int] = []
        if search_emails:
            for email in search_emails:
                r = self.s.get(
                    f"{self.api}/admin/accounts",
                    params={
                        "page": 1,
                        "page_size": 5,
                        "platform": self.cfg.get("platform") or "grok",
                        "search": email,
                        **({"group": str(group_id)} if group_id is not None else {}),
                    },
                    headers=self._headers(),
                    timeout=60,
                )
                if r.status_code >= 400:
                    continue
                data = self._unwrap(r.json())
                items = []
                if isinstance(data, dict):
                    items = data.get("items") or data.get("accounts") or data.get("data") or []
                for it in items:
                    if isinstance(it, dict) and it.get("id") is not None:
                        found.append(int(it["id"]))
                if len(found) >= limit:
                    break
        return found


# ── core cycle ─────────────────────────────────────────────────────────────

def run_cycle(cfg: dict[str, Any], source: SourceClient, target: TargetClient) -> dict[str, Any]:
    policy = cfg["policy"]
    src_cfg = cfg["source"]
    tgt_cfg = cfg["target"]
    dry = bool(policy.get("dry_run"))

    group_id = target.resolve_group_id()
    min_count = int(policy["min_count"])
    target_count = int(policy["target_count"])
    max_per = int(policy.get("max_per_cycle") or 50)

    result: dict[str, Any] = {
        "ts": time.time(),
        "group_id": group_id,
        "min_count": min_count,
        "target_count": target_count,
        "cleanup": None,
        "register": None,
        "need": 0,
        "exported": 0,
        "imported": 0,
        "failed": 0,
        "dry_run": dry,
        "message": "",
    }

    # ── A. 远程测活清理 ───────────────────────────────────────────────────
    if policy.get("enable_cleanup", True) and tgt_cfg.get("cleanup_bad", True):
        try:
            # cursor 由 cleanup_bad_accounts 写入 STATE_FILE，不再写含密 config
            meta = target.cleanup_unusable_metadata(group_id, dry_run=dry)
            live = target.cleanup_bad_accounts(group_id, dry_run=dry)
            result["cleanup"] = {
                "metadata": meta,
                "live_test": live,
                "deleted": int(meta.get("deleted") or 0) + int(live.get("deleted") or 0),
                "tested": live.get("tested"),
                "ok": live.get("ok"),
                "bad": live.get("bad"),
                "soft_bad": live.get("soft_bad"),
                "errors": live.get("errors"),
                "next_cursor": live.get("next_cursor"),
                "dry_run": dry,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("远程清理失败: %s", e)
            result["cleanup"] = {"error": str(e)}
    else:
        result["cleanup"] = {"skipped": True}

    current = target.count_group_accounts(group_id)
    result["current"] = current
    log.info(
        "group_id=%s current=%s min=%s target=%s",
        group_id,
        current,
        min_count,
        target_count,
    )

    if not policy.get("enable_refill", True) and not policy.get("enable_register", True):
        result["message"] = f"仅清理完成，当前 {current}"
        return result

    # 分层水位 + 紧急多波补号（单轮内 register→import 可连续多波）
    if current >= target_count and policy.get("enable_refill", True):
        result["message"] = f"号池已达目标 ({current} >= {target_count})，本轮不补号"
        log.info(result["message"])
        return result

    emergency = current < min_count
    if emergency:
        batch_cap = max_per
        # 紧急：单轮内多波，尽快追上消耗
        max_waves = max(1, int(policy.get("emergency_waves") or 4))
    elif current < target_count:
        batch_cap = int(policy.get("normal_batch") or max(1, max_per // 2))
        max_waves = max(1, int(policy.get("normal_waves") or 2))
    else:
        batch_cap = max_per
        max_waves = 1

    total_need = max(0, target_count - current)
    result["need"] = total_need
    result["batch_cap"] = batch_cap
    result["emergency"] = emergency
    result["max_waves"] = max_waves
    if total_need <= 0 and policy.get("enable_refill", True):
        result["message"] = "need=0"
        return result

    if not policy.get("enable_refill", True):
        # register-only path (single wave prep)
        buffer = int(src_cfg.get("local_buffer") or 20)
        min_local = int(src_cfg.get("min_local_active") or 40)
        pool_target = max(min(total_need, batch_cap), min_local) + buffer
        if policy.get("enable_register", True) and src_cfg.get("auto_register", True):
            try:
                result["register"] = source.ensure_local_pool(pool_target)
            except Exception as e:  # noqa: BLE001
                log.exception("本地注册失败: %s", e)
                result["register"] = {"error": str(e)}
        result["message"] = "注册阶段完成（refill 已关闭）"
        return result

    log.info(
        "需要补充 total_need=%s batch_cap=%s waves<=%s emergency=%s",
        total_need,
        batch_cap,
        max_waves,
        emergency,
    )

    waves: list[dict[str, Any]] = []
    total_imported = 0
    total_exported = 0
    total_failed = 0
    all_bound: list[int] = []
    last_register: dict[str, Any] | None = None
    remaining = min(total_need, batch_cap * max_waves)

    for wave in range(1, max_waves + 1):
        wave_need = min(batch_cap, max(0, target_count - (current + total_imported)))
        # if count API lags, still try to push batch_cap each emergency wave
        if wave_need <= 0:
            wave_need = batch_cap if emergency and wave == 1 else 0
        if wave_need <= 0:
            break

        log.info("── 补号波次 %s/%s need=%s ──", wave, max_waves, wave_need)
        wave_info: dict[str, Any] = {"wave": wave, "need": wave_need}

        # register if local short
        buffer = int(src_cfg.get("local_buffer") or 15)
        min_local = int(src_cfg.get("min_local_active") or 20)
        pool_target = max(wave_need, min_local) + buffer
        if policy.get("enable_register", True) and src_cfg.get("auto_register", True) and not dry:
            try:
                reg = source.ensure_local_pool(pool_target)
                last_register = reg
                wave_info["register"] = {
                    "exportable_before": reg.get("exportable_before"),
                    "exportable_after": reg.get("exportable_after"),
                    "register_count": reg.get("register_count"),
                    "early_stop": (reg.get("batch") or {}).get("early_stop"),
                    "ok_count": (reg.get("batch") or {}).get("ok_count"),
                    "fail_count": (reg.get("batch") or {}).get("fail_count"),
                }
            except Exception as e:  # noqa: BLE001
                log.exception("波次 %s 注册失败: %s", wave, e)
                wave_info["register_error"] = str(e)[:200]
                waves.append(wave_info)
                # if register hard-fails, stop more waves
                break
        elif dry:
            exp = source.count_exportable(limit=pool_target + 10)
            wave_info["register"] = {
                "dry_run": True,
                "exportable": exp,
                "would_register": exp < pool_target,
            }

        ids = source.collect_export_ids(
            need=wave_need,
            status=src_cfg.get("status") or "active",
            group=src_cfg.get("group") or "",
            require_probe_ok=bool(src_cfg.get("require_probe_ok", True)),
            page_size=int(src_cfg.get("export_batch_size") or 50),
        )
        if not ids:
            wave_info["exported"] = 0
            wave_info["message"] = "no exportable"
            waves.append(wave_info)
            log.warning("波次 %s 无本地可导出账号", wave)
            # try next wave only if register might produce more later
            if not (policy.get("enable_register", True) and src_cfg.get("auto_register", True)):
                break
            continue

        log.info("波次 %s 选中本地账号 %s 个", wave, len(ids))
        export_payload = source.export_by_ids(ids)
        auth_count = int(export_payload.get("count") or len(export_payload.get("auth") or {}))
        wave_info["exported"] = auth_count
        total_exported += auth_count
        if auth_count <= 0:
            waves.append(wave_info)
            continue

        sub2 = convert_export_to_sub2(
            export_payload,
            concurrency=int(policy.get("concurrency") or 1),
            priority=int(policy.get("priority") or 1),
            rate_multiplier=float(policy.get("rate_multiplier") or 1.0),
        )
        accounts = sub2.get("accounts") or []
        if not accounts:
            wave_info["message"] = "convert empty"
            waves.append(wave_info)
            continue

        dump_dir = SCRIPT_DIR / "exports"
        dump_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dump_path = dump_dir / f"sub2-import-w{wave}-{len(accounts)}-{stamp}.json"
        dump_body = {
            "exported_at": sub2.get("exported_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "proxies": [],
            "accounts": accounts,
        }
        with dump_path.open("w", encoding="utf-8") as f:
            json.dump(dump_body, f, ensure_ascii=False, indent=2)
        log.info("波次 %s 写出 %s", wave, dump_path)

        if dry:
            wave_info["dry_run_accounts"] = len(accounts)
            waves.append(wave_info)
            # dry-run only one wave
            break

        # import chunks
        chunk_size = max(5, int(policy.get("import_chunk_size") or 40))
        created = 0
        failed = 0
        for i in range(0, len(accounts), chunk_size):
            chunk = accounts[i : i + chunk_size]
            body = {
                "exported_at": dump_body["exported_at"],
                "proxies": [],
                "accounts": chunk,
            }
            try:
                imp = target.import_data(
                    body,
                    skip_default_group_bind=bool(
                        tgt_cfg.get("skip_default_group_bind", True)
                    ),
                )
                c = int(
                    imp.get("account_created")
                    or imp.get("created")
                    or imp.get("success")
                    or 0
                )
                f = int(imp.get("account_failed") or imp.get("failed") or 0)
                created += c
                failed += f
                log.info(
                    "波次 %s 导入 %s-%s created=%s failed=%s",
                    wave,
                    i,
                    i + len(chunk),
                    c,
                    f,
                )
            except Exception as e:  # noqa: BLE001
                failed += len(chunk)
                log.error("波次 %s 导入失败 offset=%s: %s", wave, i, e)

        total_imported += created
        total_failed += failed
        wave_info["imported"] = created
        wave_info["failed"] = failed

        # bind
        bound_ids: list[int] = []
        bind_ok = False
        want_bind = bool(tgt_cfg.get("bind_group_after_import")) and group_id is not None
        if want_bind:
            emails = [
                str((a.get("credentials") or {}).get("email") or a.get("name") or "")
                for a in accounts
            ]
            emails = [e for e in emails if e]
            new_ids = target.list_recent_account_ids(
                group_id=None,
                limit=len(emails),
                search_emails=emails,
            )
            if new_ids:
                bind_res = target.bulk_bind_group(new_ids, int(group_id))
                log.info(
                    "波次 %s 绑定 group_id=%s n=%s res=%s",
                    wave,
                    group_id,
                    len(new_ids),
                    bind_res,
                )
                bound_ids = new_ids
                bind_ok = True
                all_bound.extend(new_ids)
            else:
                log.warning("波次 %s 未能定位新账号 id 绑组", wave)
        else:
            bind_ok = True

        wave_info["bound"] = len(bound_ids)
        wave_info["bind_ok"] = bind_ok

        only_after_bind = bool(src_cfg.get("delete_only_after_bind", True))
        can_delete = bool(src_cfg.get("delete_after_export")) and ids and created > 0
        if can_delete:
            if only_after_bind and want_bind and not bind_ok:
                log.warning("波次 %s 绑组失败，跳过本地删除", wave)
                wave_info["deleted_local"] = {"skipped": True}
            else:
                del_res = source.delete_accounts(ids[:auth_count])
                log.info("波次 %s 本地删除: %s", wave, del_res)
                wave_info["deleted_local"] = del_res

        waves.append(wave_info)

        # stop waves if little progress
        if created <= 0:
            log.warning("波次 %s 导入 0，停止后续波次", wave)
            break
        # if not emergency, one solid wave may be enough
        if not emergency and total_imported >= batch_cap:
            break

    result["waves"] = waves
    result["wave_count"] = len(waves)
    result["exported"] = total_exported
    result["imported"] = total_imported
    result["failed"] = total_failed
    result["bound_ids"] = all_bound
    result["bind_ok"] = bool(all_bound) or not bool(
        tgt_cfg.get("bind_group_after_import")
    )
    result["register"] = last_register

    if dry:
        result["message"] = (
            f"dry-run: current={current}; waves={len(waves)} "
            f"would_export≈{total_exported}"
        )
        log.info(result["message"])
        return result

    after = target.count_group_accounts(group_id)
    result["after"] = after
    cu = result.get("cleanup") or {}
    result["message"] = (
        f"闭环完成: 删坏号≈{cu.get('deleted', 0)} · "
        f"{current} → {after} (imported≈{created}, failed={failed}, bound={len(bound_ids)})"
    )
    log.info(result["message"])
    return result


def setup_logging(cfg: dict[str, Any]) -> None:
    level = getattr(logging, str(cfg.get("logging", {}).get("level") or "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = cfg.get("logging", {}).get("file")
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            print(f"cannot open log file: {e}", file=sys.stderr)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def write_example_config(path: Path) -> None:
    if path.exists():
        print(f"已存在: {path}")
        return
    cfg = default_config()
    # clear secrets placeholders more clearly
    cfg["source"]["admin_password"] = "你的本地 grok2api 管理密码"
    cfg["target"]["email"] = "admin@example.com"
    cfg["target"]["password"] = "你的 sub2api 管理员密码"
    cfg["target"]["access_token"] = ""  # 优先填 JWT 可免密
    cfg["target"]["group_name"] = "grok"
    cfg["policy"]["min_count"] = 50
    cfg["policy"]["target_count"] = 100
    cfg["policy"]["interval_sec"] = 300
    save_json(path, cfg)
    print(f"已生成示例配置: {path}")
    print("请填写 source.admin_password 与 target 登录信息 / group_name 后运行。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="全自动闭环：远程测活删号 + 本地注册 + 导出导入 sub2api Grok 分组"
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"配置文件路径 (默认 {DEFAULT_CONFIG})",
    )
    parser.add_argument("--init-config", action="store_true", help="生成示例配置后退出")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模拟：不删除/不注册/不导入，只打印将要做什么",
    )
    parser.add_argument("--min-count", type=int, default=None, help="低于此数量触发补号")
    parser.add_argument("--target-count", type=int, default=None, help="补到此数量")
    parser.add_argument("--max-per-cycle", type=int, default=None, help="单轮最多导入数")
    parser.add_argument("--interval", type=int, default=None, help="循环间隔秒")
    parser.add_argument("--group-name", type=str, default=None, help="远程分组名")
    parser.add_argument("--group-id", type=int, default=None, help="远程分组 ID")
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="本轮跳过远程测活删除",
    )
    parser.add_argument(
        "--skip-register",
        action="store_true",
        help="本轮跳过本地自动注册",
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="只做远程坏号清理",
    )
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="只做本地注册补源",
    )
    args = parser.parse_args(argv)

    if args.init_config:
        write_example_config(args.config)
        return 0

    cfg = load_config(args.config, args)
    setup_logging(cfg)

    if not args.config.is_file() and not (
        os.environ.get("SOURCE_ADMIN_PASSWORD")
        or os.environ.get("SOURCE_ADMIN_TOKEN")
        or os.environ.get("TARGET_ACCESS_TOKEN")
    ):
        log.error(
            "找不到配置 %s。请先运行:\n  python %s --init-config",
            args.config,
            Path(__file__).name,
        )
        return 2

    if not acquire_lock(owner="cli"):
        return 3
    try:
        source = SourceClient(cfg["source"])
        target = TargetClient(cfg["target"])

        try:
            source.login()
            target.login()
        except Exception as e:  # noqa: BLE001
            log.exception("登录失败: %s", e)
            return 1

        once = bool(cfg.get("_once") or args.once)
        interval = max(30, int(cfg["policy"].get("interval_sec") or 300))

        while True:
            try:
                result = run_cycle(cfg, source, target)
                state = load_state()
                history = state.get("history") or []
                history.append(result)
                state["history"] = history[-100:]
                state["last"] = result
                save_state(state)
            except requests.HTTPError as e:
                log.error("HTTP 错误: %s %s", e, getattr(e.response, "text", "")[:500])
                if e.response is not None and e.response.status_code == 401:
                    try:
                        source.login()
                        target.login()
                    except Exception:  # noqa: BLE001
                        log.exception("重新登录失败")
            except Exception as e:  # noqa: BLE001
                log.exception("本轮异常: %s", e)

            if once:
                break
            log.info("休眠 %ss …", interval)
            time.sleep(interval)

        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
