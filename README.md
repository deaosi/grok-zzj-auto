# grok-zzj-auto

Grok 号池 **全自动补号闭环**：监控远程 sub2api 分组水位 → 坏号测活删除 → 本地不足时注册补源 → 导出并转换为 sub2 格式 → 导入远程绑定分组。

> 本仓库上传的是 **自动补号全流程**（脚本 + 控制面板）。  
> 协议注册机（camoufox / grok-build-auth）体积大且依赖本地 Docker 环境，默认不整仓上传；脚本通过 HTTP 调用已部署的本地 `grok2api`（`127.0.0.1:3000`）完成注册。

## 架构

```
控制面板 http://127.0.0.1:8787  或  CLI 循环
              │
              ▼
   auto_refill_sub2api.py（单实例锁）
      ├─ A. 远程 sub2api：抽样测活 / 删坏号
      ├─ B. 统计分组数量
      ├─ C. 本地 grok2api：不足则协议注册
      ├─ D. 导出验活账号 → 转 sub2 JSON
      └─ E. 导入远程 + 绑定分组 +（可选）删本地
```

## 目录

```
scripts/
  auto_refill_sub2api.py          # 核心闭环
  panel_server.py                 # Web 控制面板
  fix_local_admin.py              # 急救：停卡死注册、恢复后台
  config.auto_refill.example.json # 配置模板（无密钥）
  start_panel.bat                 # 启动面板
  start_auto_refill.bat           # CLI 常驻
  fix_local_admin.bat             # 急救脚本
  README_auto_refill.md           # 详细说明
```

## 依赖

- Python 3.10+
- `pip install requests fastapi uvicorn`
- 本地已部署可登录的 grok2api：`http://127.0.0.1:3000`
- 远程 sub2api 管理员账号：如 `https://fd.diamondruby.xyz`

## 快速开始

```bat
cd scripts
copy config.auto_refill.example.json config.auto_refill.json
notepad config.auto_refill.json
```

填写：

| 字段 | 说明 |
|------|------|
| `source.admin_password` | 本地 3000 管理密码 |
| `target.email` / `password` | 远程管理员（或 `access_token`） |
| `target.group_id` 或 `group_name` | 如 `11` / `grok` |
| `policy.min_count` / `target_count` | 水位，建议 300 / 500 |

启动控制面板：

```bat
start_panel.bat
```

浏览器打开 **http://127.0.0.1:8787**

1. 点「测试两边登录」  
2. 先「模拟运行」  
3. 再「启动自动循环」或「执行完整闭环」

## 安全

**切勿提交：**

- `config.auto_refill.json`（含真实密码）
- `.auto_refill_state.json` / `.auto_refill.lock`
- `exports/*.json`（含 access/refresh token）
- `auto_refill.log`
- 任何 `.env`

仓库已提供 `.gitignore`。

## 运维急救

本地 3000 卡顿、注册会话堆积时：

```bat
fix_local_admin.bat
```

会：登录本地 → `stop-all` 注册会话 → 探测账号页延迟。

## 许可

仅供授权环境下的账号池运维使用。请遵守上游服务条款与当地法律。
