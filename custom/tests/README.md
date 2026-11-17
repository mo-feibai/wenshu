# 文枢测试入口

## 默认使用隔离验证

完整边界、前置条件、已知缺陷与证据格式见[隔离测试说明](<../../tests/README.md>)。

从仓库根目录运行：

```bash
python -B custom/tests/static_check.py
python -B tests/baseline/run.py
python -B tests/baseline/build_site.py
```

静态工具检查应用与新测试的 Python 语法，编译在内存中完成；JSON 与密钥扫描只用 Git 跟踪列表。Git 不可用时阻断，不回退读取本地运行态；受保护运行态即使误跟踪也拒读，扫描失败不输出密钥值。

[CI](<../../.github/workflows/ci.yml>) 现在配置了 Linux/Windows 的静态检查、白名单基线与真实隔离构建，不再仅跑静态层。Git/build 合成故障只服务于契约测试，真实构建仍单独执行在副本中。

## 旧真实集成脚本：不进入默认入口或 CI

| 文件 | 风险边界 |
|---|---|
| [run_integration.sh](<run_integration.sh>) | 依赖已部署服务，会读取真实凭据、触发库写入与重建 |
| [test_module_share.py](<test_module_share.py>) | 真实模块分享流程；清理可能删除整个测试项目 |
| [test_share_ui.py](<test_share_ui.py>) | 真实登录、分享页面、搜索与附件流程 |
| [test_status_ui.py](<test_status_ui.py>) | 会翻转真实文档状态；清理可能删除整个测试项目 |

没有本次授权下的安全自动清理保证。不得 broad discovery，不要对真实部署运行这些脚本；后续确需验证，必须先明确测试部署、库、备份、凭据与清理边界，再取得单独授权。本阶段不删除或移动这些文件。
