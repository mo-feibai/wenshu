#!/bin/bash
set -e
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SRC/custom/server.py" /opt/wenshu/app/server.py
python3 - <<'PY'
import json, pathlib, secrets
p = pathlib.Path('/opt/wenshu/app/server_config.json')
cfg = json.loads(p.read_text(encoding='utf-8'))
cfg.setdefault('docs_dir', '/opt/wenshu/docs')
if 'api_token' not in cfg:
    cfg['api_token'] = secrets.token_urlsafe(24)
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
print('config updated; token_len =', len(cfg['api_token']))
PY
systemctl restart wenshu
sleep 2
echo "service: $(systemctl is-active wenshu)"
KEY=$(python3 -c "import json;print(json.load(open('/opt/wenshu/app/server_config.json'))['api_token'])")
echo "--- pending ---"
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8903/api/admin/pending | head -c 400
echo
echo "--- context count ---"
curl -s -H "X-API-Key: $KEY" "http://127.0.0.1:8903/api/admin/context" | python3 -c "import json,sys;d=json.load(sys.stdin);print('docs:',d['count'],'pending:',d['pending_count'],'projects:',[p['name'] for p in d['projects']])"
echo "--- no key -> expect 401 ---"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8903/api/admin/pending
