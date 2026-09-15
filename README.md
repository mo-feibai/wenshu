# 文枢（WenShu）

文档库驱动的知识站点：以目录结构组织 Markdown / SQL 源文件，构建为带全文搜索、版本差异、状态看板的静态站点，并提供单文件部署的轻量服务（登录、临时分享、Admin API）与 opencode 插件。

> 本仓库不含任何真实业务数据：`sample-docs/` 为虚构示例，用于演示与本地构建。

## 特性

- **docs-as-code**：md 用 YAML frontmatter、SQL 用 `-- @meta` 声明元数据（项目/主题/类型/状态/版本/日期/关联）
- **版本链与差异**：`supersedes` / `superseded_by` 自动生成「与上一版 vN 的改动」diff 卡片
- **全文搜索**：构建期生成 pagefind 索引（中文可用）
- **状态看板**：待执行 SQL（pending）与待实现接口（pending）自动汇总
- **访问控制**：共享密码登录、单文档临时分享（含附件白名单）
- **Admin API**：供 opencode 插件写入文档库，自动重建站点并 git 提交
- **备份**：systemd timer 每日备份文档库与服务配置（可异地复制）

## 目录结构

| 路径 | 说明 |
|---|---|
| `custom/` | Astro 站点 + `server.py`（Python 标准库服务） |
| `custom/scripts/sync_files.py` | 同步可下载文件（SQL/规格/资产）到 `public/files/`，生成 `file_keys.json` |
| `custom/scripts/gen_site_content.py` | 从文档库生成站点内容（`src/content/docs`、`src/data/assets.json`） |
| `custom/src/` | 页面、布局、样式、内容集合定义 |
| `custom/public/` | 字体、vendor 资源、下载文件输出目录 |
| `sample-docs/` | 合成示例文档库（虚构内容） |
| `opencode/` | opencode 插件（`wenshu.ts`）与配置示例 |
| `deploy_*.sh` / `backup.sh` / `wenshu*.service` / `wenshu-backup.timer` | WSL 部署与备份脚本 |
| `test_server.py` / `test_admin_api.py` | 服务鉴权/分享与 Admin API 测试 |
| `make_manifest.py` / `compare_manifest.py` | 目录校验工具（sha256 清单、双端比对） |

## 快速开始

### 1. 构建站点（示例数据）

```bash
cd custom
npm ci

# WSL / Linux
WENSHU_DOCS=../sample-docs npm run build

# Windows (PowerShell)
$env:WENSHU_DOCS="..\sample-docs"; npm run build:win
```

构建产物在 `custom/dist/`。`npm run build` 会依次执行：同步下载文件 → 生成站点内容 → `astro build` → `pagefind` 建索引。

### 2. 本地运行服务

```bash
cd custom
cp server_config.example.json server_config.json   # 修改密码
python3 server.py                                   # 需要先完成构建（dist/）
```

打开 http://127.0.0.1:8903 。首次使用 Admin API 前，用 `deploy_admin.sh`（或手动）在 `server_config.json` 中生成 `api_token`。

### 3. 部署到 WSL（/opt/wenshu）

```bash
# 文档库放在 /opt/wenshu/docs，应用放在 /opt/wenshu/app
sudo mkdir -p /opt/wenshu
sudo cp -r sample-docs /opt/wenshu/docs

# 同步应用并安装依赖
sudo cp -r custom /opt/wenshu/app
cd /opt/wenshu/app && sudo npm ci

# 生成配置（含 api_token）并注册服务
sudo bash /path/to/deploy_admin.sh
sudo cp /path/to/wenshu.service /etc/systemd/system/ && sudo systemctl enable --now wenshu
```

### 4. opencode 插件

见 [`opencode/README.md`](opencode/README.md)：将 `wenshu.ts` 放入 `~/.config/opencode/plugins/`，按 `wenshu.example.json` 创建 `~/.config/opencode/wenshu.json`，重启 opencode 即可获得 `wenshu_*` 工具。

## 文档库格式

### Markdown

```markdown
---
title: "示例接口文档"
project: "demo-project"
topic: "示例接口"
type: "api"            # sql / api / adr / analysis / scheme / report / other
status: "implemented"  # 见下方状态表
version: 1
date: "2026-09-01"
summary: "一句话摘要"
related: ["demo-project/sql/迁移-20260901-示例建表"]
supersedes: "demo-project/api/示例接口文档"    # 指向上一版
---

正文…
```

### SQL（`-- @meta`）

```sql
-- @meta
-- title: 示例建表迁移
-- project: demo-project
-- topic: 示例建表
-- type: sql
-- status: executed
-- date: 2026-09-01
-- related: demo-project/api/示例接口文档
-- @end
CREATE TABLE ...
```

### 状态语义

| 类型 | 状态 |
|---|---|
| sql | `pending`（待执行）→ `executed`（已执行） |
| api | `pending`（待实现）→ `implemented`（已实现） |
| adr | `accepted` |
| analysis / scheme / report / other | `draft` → `final` |
| 任意 | `archived` |

### 非文档产物（assets.yml）

项目根目录的 `assets.yml` 登记附件，构建时同步到 `/files/` 并挂到主题页：

```yaml
- path: "测试页面/示例测试页面.html"
  topic: "示例测试页面"
  date: "2026-09-01"
```

## 配置（server_config.json）

| 字段 | 说明 |
|---|---|
| `password` | 站点访问密码（必填，无默认值） |
| `port` | 监听端口，默认 `8903` |
| `docs_dir` | 文档库目录，默认 `/opt/wenshu/docs` |
| `api_token` | Admin API 令牌（opencode 插件使用） |

## 环境变量

| 变量 | 作用 | 默认值 |
|---|---|---|
| `WENSHU_DOCS` | 文档库目录（相对路径基于应用根解析） | `/opt/wenshu/docs` |
| `WENSHU_APP` | 应用根目录 | 脚本所在应用的上一级 |
| `WENSHU_BACKUP_WIN` | 备份异地目录（未设置则跳过异地副本） | 空 |
| `WENSHU_BASE` | 测试脚本目标地址 | `http://127.0.0.1:8903` |
| `WENSHU_CONFIG` | 测试脚本读取的配置文件 | `/opt/wenshu/app/server_config.json` |
| `WENSHU_PASSWORD` / `WENSHU_TEST_DOC_A` / `WENSHU_TEST_DOC_B` / `WENSHU_TEST_ATTACH_OK` / `WENSHU_TEST_ATTACH_OTHER` / `WENSHU_TEST_RELATED` | 测试脚本覆盖项 | 示例文档 |

## 说明

- 仓库中的 `custom/public/fonts/` 为 Maple Mono 字体（SIL Open Font License 1.1），随站点静态资源分发。
- 构建生成的 `custom/src/content/docs/`、`custom/public/files/`、`custom/scripts/file_keys.json`、`custom/src/data/assets.json` 不入库，每次构建重新生成。
