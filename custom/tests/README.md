# 文枢测试

分两层，互不依赖：

## 1. 静态检查（任意平台，秒级）

```powershell
python custom/tests/static_check.py
```

覆盖：`custom/**/*.py` 语法编译、JSON 合法性、跟踪文件密钥扫描。
CI（`.github/workflows/ci.yml`）只跑这一层。

## 2. 集成测试（WSL 内，需运行中的文枢服务）

```powershell
wsl -d Debian -- bash /mnt/e/TSCODE/project/site-compare/custom/tests/run_integration.sh
```

| 文件 | 覆盖 |
|---|---|
| `test_module_share.py` | 模块分享核心流程 + 12 条负向用例（越权、撤销、到期、多 token） |
| `test_share_ui.py` | 分享端到端（登录、创建、访客落地页、模块内搜索、附件白名单） |
| `test_status_ui.py` | 页面状态切换（8 条：翻转、终态保护、类型校验、未登录 401） |

注意：
- 读取 `/opt/wenshu/app/server_config.json` 中的 `password` / `api_token`
- 会创建带随机后缀的临时文档与分享，脚本结束时自动清理
- 每个写操作触发服务端全量重建（约 15 秒），整套 3~6 分钟
- 请勿在别人正使用站点时运行（期间页面内容会经历多次重建）
