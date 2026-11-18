# DeepSeek Harness 插件：dsh-wenshu（文枢）

把文枢（WenShu）文档库接进 DeepSeek Harness：14 个 `wenshu_*` 工具 + 文档库直写守卫 + 系统提示段。
流程约束（先查重、补丁还是重写、状态翻转要用户确认、API 文档格式规范）留在配套的 `wenshu` skill 里，插件只提供「工具」与「护栏」。

## 能力

| 工具 | 作用 |
| --- | --- |
| `wenshu_context` | 全局结构：项目→主题→文档（含状态/版本/日期/待办），可按 project/topic/query 过滤 |
| `wenshu_find` | 语义检索（本地嵌入模型），建文档前查重 |
| `wenshu_similar` | 找与某篇文档相似的其它文档 |
| `wenshu_read` | 读单篇（元数据 + 正文 + revision） |
| `wenshu_create` | 新建文档（服务端自动重建站点并 git 提交） |
| `wenshu_patch` | 精确替换打补丁，不升版（**改文档默认用它**） |
| `wenshu_update` | 整篇重写 pending/draft 文档（不换 id、不换状态） |
| `wenshu_new_version` | 出新版本（服务端强制门槛：implemented + major_change + changes） |
| `wenshu_set_status` | 状态翻转（须先问用户） |
| `wenshu_update_meta` | 改 topic/domain/summary/title/related 或补「本版变更」 |
| `wenshu_review` | 淘汰候选清单（只读） |
| `wenshu_retire` | stale / retired / restore |
| `wenshu_lint` | API/OpenAPI 文档格式体检（只读） |
| `wenshu_pending` | 待办清单 |

## 守卫

`/opt/wenshu/docs`（含 UNC `\\wsl.localhost\Debian\opt\wenshu\docs`）只允许通过 `wenshu_*` 工具读写：

- 写工具（`write`/`edit`/`patch`/`wenshu_patch` 这类）参数里的路径命中文档库 → 拒绝。
- shell 工具（`bash`/`pwsh`/`ops_bash` …）命令里出现文档库路径，且不是只读命令（`cat`/`ls`/`grep`/`rg`/`head`/`tail`/`wc`/`find`/`stat`/`git log|status|diff` 等）→ 拒绝；命中写入模式（`>`、`tee`、`sed -i`、`cp`/`mv`/`rm`、`git add|commit|…`）也拒绝。
- 守卫自身异常一律放行，避免误伤无关工具。

守卫通过两条路径同时注册：`tools/pre-execute` 瀑布（返回 `deny`）与 `ctx.tools.guard`（兜底）。

## 连接与凭据

解析顺序（每次调用重新解析，不缓存）：

1. 凭据中心 `refs.<credentialRef>`（默认 `WENSHU_API_KEY`，见 `~/.dsh/.credentials.yaml`）
2. 环境变量 `<tokenEnv>`（默认同样是 `WENSHU_API_KEY`）
3. 旧 opencode 配置 `~/.config/opencode/wenshu.json` 的 `apiToken`（`migrateLegacyToken` 为真时自动写入凭据中心，只尝试一次）

地址：`baseUrl`（默认 `http://127.0.0.1:8903`）优先，回退旧配置的 `baseUrl`。

> 安全约定：不要把 API Key 写进 profile 的插件 Config，进凭据中心。插件只读不落盘。

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `baseUrl` | `http://127.0.0.1:8903` | 文枢 API 地址 |
| `siteUrl` | `http://192.168.0.71:8903` | 站点地址（局域网） |
| `credentialRef` | `WENSHU_API_KEY` | 凭据中心 ref |
| `tokenEnv` | `WENSHU_API_KEY` | 密钥环境变量名 |
| `legacyConfigPath` | `~/.config/opencode/wenshu.json` | 旧配置（迁移源） |
| `migrateLegacyToken` | `true` | 是否把旧配置 apiToken 迁入凭据中心 |
| `timeoutMs` | `15000` | 单次请求超时 |
| `maxOutputChars` | `14000` | 返回正文截断上限（`wenshu_read` 至少 30000） |
| `guardEnabled` | `true` | 文档库守卫开关 |
| `extraWriteTools` | `[]` | 追加被视为写工具的工具名 |
| `extraShellTools` | `[]` | 追加被视为 shell 的工具名 |
| `extraDocsPaths` | `[]` | 追加受保护路径片段（小写、`/` 分隔） |

## 安装

本目录即插件 bundle 根目录（`index.js` + `lib/guard.js` + `cordis.patch.yml`），装法二选一：

1. 直接从仓库挂载：

   ```text
   plugin_manager:
     action: install_bundle
     target: <仓库根>/dsh
   ```

2. 复制到本地插件目录后挂载：

   ```bash
   cp -r dsh ~/dsh-plugins/dsh-wenshu
   ```

`cordis.patch.yml` 只做一件事：把 `dsh-wenshu` 插进 profile（`id: wenshu`），配置留空以便热挂载；配置项全部由 `index.js` 的 Config schema 给默认值。

## 测试

守卫是纯函数，直接跑（DSH 沙箱下 `node --test` 会在 spawn 子进程时 EPERM，所以直接执行测试文件）：

```bash
node dsh/test/guard.test.mjs
```

## 已知边界

- 语义检索依赖本机 `llama-embed.service`（127.0.0.1:8910）；不可用时 `wenshu_find` 返回 503，改用 `wenshu_context` 拆关键词。
- 服务端每次写操作都会重建站点并 git 提交，写工具返回的 `url` 即站点路径。
