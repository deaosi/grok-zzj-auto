# 全自动号池闭环

监控远程 sub2api 的 **Grok 分组**，坏号自动删；不足时本地 **协议注册** 补源，再导出 → 转 sub2 → 导入远程。

## 每轮流程

```
A. 远程清理
   列出 group 内账号 → POST /api/v1/admin/accounts/{id}/test
   HTTP≠200 或 success=false → DELETE 删除

B. 统计可用数，若 < min_count：
   B1. 本地可导出不足 → POST /admin/api/accounts/register-email 注册
       轮询 batch 直到完成（使用后台已保存的邮件/打码/代理配置）
   B2. 导出 active + last_probe.ok 账号
   B3. 本地转 sub2 格式（等同 localhost:6501，无需浏览器）
   B4. POST /api/v1/admin/accounts/data 导入 + 绑定分组
   B5. 可选：本地删除已导出账号
```

## 控制面板（推荐）

```bat
cd D:\1111\grok-zc\scripts
start_panel.bat
```

浏览器打开 **http://127.0.0.1:8787**

面板功能：

- 表单 / JSON 编辑配置并保存  
- 测试本地 + 远程登录  
- 启动 / 停止循环  
- 单轮：完整闭环 / 只清理 / 只注册 / 只补号 / Dry-run  
- 实时日志 + 最近历史  

## 快速开始（命令行）

```bat
cd D:\1111\grok-zc\scripts
copy config.auto_refill.example.json config.auto_refill.json
notepad config.auto_refill.json
```

**必填：**

| 字段 | 说明 |
|------|------|
| `source.admin_password` | 本地 `127.0.0.1:3000` 管理密码 |
| `target.email` + `target.password` | 远程管理员（或只填 `access_token`） |
| `target.group_name` 或 `group_id` | 如 `grok` |

**本地注册前提：** 已在 `http://127.0.0.1:3000/admin/accounts` 配好邮件（cfmail 等）、打码、代理，并确认手动点「注册」能成功。脚本默认 `register_body: {}`，沿用后台保存的 `registration_config`。

### 建议试跑顺序

```bat
:: 1) 只测活统计，不删不导
python auto_refill_sub2api.py --once --dry-run --cleanup-only

:: 2) 真删坏号
python auto_refill_sub2api.py --once --cleanup-only

:: 3) 完整闭环一轮
python auto_refill_sub2api.py --once

:: 4) 常驻
start_auto_refill.bat
```

## 配置要点

| 字段 | 默认 | 含义 |
|------|------|------|
| `policy.min_count` | 50 | 低于此触发补号 |
| `policy.target_count` | 100 | 补到此数量 |
| `policy.max_per_cycle` | 50 | 单轮最多导入 |
| `policy.interval_sec` | 300 | 循环间隔 |
| `target.cleanup_bad` | true | 是否测活删号 |
| `target.cleanup_workers` | 4 | 测活并发 |
| `source.auto_register` | true | 本地不足时自动注册 |
| `source.register_count` | 30 | 每次注册数 |
| `source.delete_after_export` | true | 导出后删本地，防重复 |

## CLI

```text
--once / --dry-run
--cleanup-only          只清理远程坏号
--register-only         只本地注册
--skip-cleanup / --skip-register
--min-count / --target-count / --group-name / --group-id
```

## 安全

- `config.auto_refill.json` 与 `exports/` 含 token，勿提交公开仓库  
- 远程测活会真实请求上游，`cleanup_workers` 不宜过大  
- 删除不可恢复，建议先 `--dry-run --cleanup-only`

## 相关

- 本地后台：http://127.0.0.1:3000/admin/accounts  
- 远程后台：https://fd.diamondruby.xyz/admin/accounts  
- 转换页（脚本已内置，可不启动）：http://localhost:6501  
