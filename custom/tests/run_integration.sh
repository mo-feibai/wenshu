#!/usr/bin/env bash
# 文枢集成测试：在 WSL 内运行，目标为 127.0.0.1:8903 上运行中的文枢服务。
# Windows 侧用法：
#   wsl -d Debian -- bash /mnt/e/TSCODE/project/site-compare/custom/tests/run_integration.sh
# 说明：
#   - 读取 /opt/wenshu/app/server_config.json 的 password / api_token
#   - 会创建带随机后缀的临时文档、分享，结束时自动清理
#   - 每个写操作触发服务端全量重建（~15s），整套约 3~6 分钟
set -u
cd "$(dirname "$0")" || exit 1

rc=0
# test_share_context.py 是纯 mock 单元测试（导入 server.py，不连服务、不写文档库），
# 先跑它拿下授权匹配逻辑的快速反馈，再跑需要真实重建的端到端用例。
for t in test_doc_format.py test_share_context.py test_module_share.py test_share_ui.py test_status_ui.py; do
  echo "===== $t ====="
  python3 "$t" || rc=1
done

echo "===== 集成测试结束：$([ $rc -eq 0 ] && echo ALL-PASS || echo HAS-FAIL) ====="
exit $rc
