#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""急救：停止卡死注册会话，恢复 3000 后台响应。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
CFG = Path(__file__).resolve().parent / "config.auto_refill.json"


def load_password() -> str:
    if CFG.is_file():
        try:
            c = json.loads(CFG.read_text(encoding="utf-8"))
            pw = (c.get("source") or {}).get("admin_password") or ""
            if pw:
                return str(pw)
        except Exception:
            pass
    if ENV.is_file():
        text = ENV.read_text(encoding="utf-8")
        m = re.search(r"^GROK2API_ADMIN_PASSWORD=(.*)$", text, re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="清理本地 grok2api 卡死注册会话")
    ap.add_argument("--base", default="http://127.0.0.1:3000")
    ap.add_argument("--password", default="")
    args = ap.parse_args()
    pw = args.password or load_password()
    if not pw:
        print("缺少管理密码（--password 或 config/env）", file=sys.stderr)
        return 2
    s = requests.Session()
    r = s.post(f"{args.base.rstrip('/')}/admin/api/login", json={"password": pw}, timeout=20)
    if r.status_code != 200:
        print(f"登录失败 {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return 1
    token = r.json().get("token")
    headers = {"X-Admin-Token": token, "Content-Type": "application/json"}
    before = s.get(
        f"{args.base.rstrip('/')}/admin/api/accounts/register-email/sessions",
        params={"limit": 100},
        headers=headers,
        timeout=30,
    ).json()
    sessions = before.get("sessions") or []
    print(f"清理前 sessions={len(sessions)} total={before.get('total')} active={before.get('active')}")
    stop = s.post(
        f"{args.base.rstrip('/')}/admin/api/accounts/register-email/stop",
        headers=headers,
        json={},
        timeout=60,
    )
    print(f"stop-all -> {stop.status_code} {stop.text[:200]}")
    time.sleep(2)
    after = s.get(
        f"{args.base.rstrip('/')}/admin/api/accounts/register-email/sessions",
        params={"limit": 100},
        headers=headers,
        timeout=30,
    ).json()
    print(
        f"清理后 returned={after.get('returned')} total={after.get('total')} active={after.get('active')}"
    )
    # accounts page smoke
    t0 = time.time()
    acc = s.get(
        f"{args.base.rstrip('/')}/admin/api/accounts",
        params={"page": 1, "page_size": 25},
        headers=headers,
        timeout=30,
    )
    print(f"accounts page {acc.status_code} {int((time.time()-t0)*1000)}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
