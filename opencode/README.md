# opencode 插件：wenshu（文枢）

本目录是文枢文档库的 opencode 插件，提供 `wenshu_*` 工具（读取文档库结构、创建文档、出新版本、翻转状态、更新元数据），并拦截对文档库的直接写入（`edit`/`write`/`bash` 重定向等），强制走插件工具。

## 安装

1. 把插件复制到 opencode 全局插件目录：

   ```bash
   # Linux / macOS
   mkdir -p ~/.config/opencode/plugins
   cp plugins/wenshu.ts ~/.config/opencode/plugins/wenshu.ts

   # Windows (PowerShell)
   New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\opencode\plugins"
   Copy-Item .\plugins\wenshu.ts "$env:USERPROFILE\.config\opencode\plugins\wenshu.ts"
   ```

2. 基于 `wenshu.example.json` 创建配置（插件固定读取 `~/.config/opencode/wenshu.json`）：

   ```json
   {
     "baseUrl": "http://127.0.0.1:8903",
     "apiToken": "<server_config.json 中的 api_token>"
   }
   ```

   `apiToken` 由文枢服务端 `server_config.json` 提供，可通过 `deploy_admin.sh` 首次生成。

3. 重启 opencode 使插件生效。

## 工具

| 工具 | 用途 |
|---|---|
| `wenshu_context` | 读取文档库结构（项目→主题→文档，含状态/版本/待办） |
| `wenshu_read` | 读取单篇文档（元数据 + 正文） |
| `wenshu_create` | 新建文档（SQL 自动写 `-- @meta`，md 写 frontmatter） |
| `wenshu_new_version` | 出 vN+1（回填旧版 `superseded_by`，自动注入「本版变更」） |
| `wenshu_set_status` | 翻转状态（SQL executed / 接口 implemented / ADR accepted …） |
| `wenshu_update_meta` | 更新主题、摘要、关联 id 等元数据 |
| `wenshu_pending` | 列出待执行 SQL / 待实现接口 |
