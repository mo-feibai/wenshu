import { type Plugin, tool } from "@opencode-ai/plugin"
import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

type Cfg = { baseUrl: string; apiToken: string }

const CFG_PATH = join(homedir(), ".config", "opencode", "wenshu.json")

function loadCfg(): Cfg | null {
  try {
    const cfg = JSON.parse(readFileSync(CFG_PATH, "utf8"))
    if (cfg && cfg.baseUrl && cfg.apiToken) return cfg as Cfg
  } catch {}
  return null
}

function isDocsPath(value: string): boolean {
  const low = value.toLowerCase().replace(/\\/g, "/")
  return low.includes("/opt/wenshu/docs") || low.includes("wsl.localhost/debian/opt/wenshu/docs")
}

const WRITE_TOOLS = new Set(["edit", "write", "patch", "multiedit", "apply_patch"])
const READONLY_BASH =
  /^\s*(cat|ls|grep|rg|head|tail|wc|find|file|stat|sed\s+-n|git\s+(log|status|diff|show|ls-files))\b/
const WRITE_BASH =
  /(>|>>|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\btouch\b|\bchmod\b|\bchown\b|\btruncate\b|\bdd\b|python3?\s+-c|node\s+-e|\bgit\s+(add|commit|checkout|reset|apply|clean|rm)\b)/

function safeParse(text: string) {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function callApi(cfg: Cfg, path: string, method = "GET", body?: unknown): Promise<string> {
  try {
    const res = await fetch(cfg.baseUrl + path, {
      method,
      headers: { "Content-Type": "application/json", "X-API-Key": cfg.apiToken },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const text = await res.text()
    return JSON.stringify({ status: res.status, result: safeParse(text) })
  } catch (err) {
    return JSON.stringify({ error: `文枢服务不可达（${cfg.baseUrl}）：${String(err)}` })
  }
}

function fmt(text: string, max = 14000): string {
  return text.length > max ? text.slice(0, max) + "…(已截断)" : text
}

const guardMessageWrite =
  "文枢文档库受保护：禁止直接编辑文档库文件。请改用 wenshu_* 工具——新建用 wenshu_create；原地改未完成文档（pending/draft）用 wenshu_update；错别字/措辞微调用 wenshu_patch（不升版）；已实现/已执行的大改动才用 wenshu_new_version；翻转状态用 wenshu_set_status；改主题/摘要/关联用 wenshu_update_meta。"
const guardMessageBash =
  "文枢文档库受保护：bash 中禁止对文档库做写入/变更操作。请改用 wenshu_* 工具；只读查看请用不带重定向的 cat/ls/grep/rg 等。"

export const WenShuPlugin: Plugin = async ({ client }) => {
  const cfg = loadCfg()
  client?.app
    ?.log?.({
      body: {
        service: "wenshu",
        level: cfg ? "info" : "warn",
        message: cfg ? "wenshu plugin ready" : `wenshu config missing: ${CFG_PATH}`,
      },
    })
    .catch(() => {})

  const requireCfg = (): Cfg => {
    if (!cfg) throw new Error(`文枢配置缺失：请创建 ${CFG_PATH}（baseUrl + apiToken）后重启 opencode`)
    return cfg
  }

  return {
    "tool.execute.before": async (input, output) => {
      const args = (output?.args ?? {}) as Record<string, unknown>
      if (WRITE_TOOLS.has(input.tool)) {
        const target = String(args.filePath ?? args.path ?? args.file ?? "")
        if (target && isDocsPath(target)) throw new Error(guardMessageWrite)
      }
      if (input.tool === "bash") {
        const cmd = String(args.command ?? "")
        if (cmd && isDocsPath(cmd)) {
          const trimmed = cmd.trim()
          if (!READONLY_BASH.test(trimmed) || WRITE_BASH.test(cmd)) throw new Error(guardMessageBash)
        }
      }
    },
    tool: {
      wenshu_context: tool({
        description:
          "文枢：读取全局文档库结构（项目→主题→文档，含状态/版本/日期与待办）。生成或更新文档前先调用，用于确定 project/topic 命名与关联 id。可用 project/topic/query 过滤。返回的 url 字段是站点路径（站点根 http://192.168.0.71:8903）。",
        args: {
          project: tool.schema.string().optional().describe("按项目过滤，如 xingtai-smart-store-api"),
          topic: tool.schema.string().optional().describe("按主题过滤，如 订餐支付"),
          query: tool.schema.string().optional().describe("关键词过滤（标题/主题/摘要/正文片段）"),
        },
        async execute(args) {
          const cfg = requireCfg()
          const qs = new URLSearchParams()
          if (args.project) qs.set("project", args.project)
          if (args.topic) qs.set("topic", args.topic)
          if (args.query) qs.set("query", args.query)
          const q = qs.toString()
          return fmt(await callApi(cfg, "/api/admin/context" + (q ? `?${q}` : "")))
        },
      }),

      wenshu_read: tool({
        description: "文枢：读取单篇文档（元数据 + 正文）。id 例如 xingtai-smart-store-api/餐厅/sql/迁移-20260912-订餐支付唯一键。",
        args: {
          id: tool.schema.string().describe("文档 id（文档库相对路径去掉扩展名）"),
        },
        async execute(args) {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, `/api/admin/doc?id=${encodeURIComponent(args.id)}`))
        },
      }),

      wenshu_create: tool({
        description:
          "文枢：新建文档。sql 类型自动命名 迁移-YYYYMMDD-<topic>.sql 并写入 -- @meta（可用 filename 覆盖文件名）；api/其他类型写入 <title>.md 的 frontmatter。写库后服务自动重建站点并 git 提交。related 必须引用已存在的文档 id（先用 wenshu_context 查询）。",
        args: {
          project: tool.schema.string().describe("项目名，如 xingtai-smart-store-api"),
          topic: tool.schema.string().describe("主题（功能主题，如 订餐支付）"),
          type: tool.schema
            .enum(["sql", "api", "adr", "analysis", "scheme", "report", "other"])
            .describe("文档类型"),
          title: tool.schema.string().describe("标题（sql 类型用于命名提示，api 类型即文件名）"),
          content: tool.schema.string().describe("正文：sql 为原始 SQL 脚本；其他为 Markdown 正文（不含 frontmatter）"),
          domain: tool.schema.string().optional().describe("领域/子目录，如 餐厅（可省略）"),
          status: tool.schema.string().optional().describe("初始状态：sql[pending|executed] api[pending|implemented] 等"),
          related: tool.schema.array(tool.schema.string()).optional().describe("关联文档 id 数组"),
          summary: tool.schema.string().optional().describe("一句话摘要"),
          date: tool.schema.string().optional().describe("日期 YYYY-MM-DD（默认今天）"),
          version: tool.schema.number().optional().describe("版本号（一般 API 文档用）"),
          filename: tool.schema.string().optional().describe("显式文件名（sql 类型专用，如 迁移-20260914-订餐支付.sql）"),
        },
        async execute(args) {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, "/api/admin/create", "POST", args))
        },
      }),

      wenshu_new_version: tool({
        description:
          "文枢：为已有文档出新版本（严格门槛，服务端强制）。API：必须当前 status=implemented，且显式传 major_change=true 并填写 changes（仅新增/删除接口、契约实质变化、大范围改写）。SQL：必须当前 status=executed（未执行的 SQL 请用 wenshu_update 原地改）。未实现/未执行的文档调用会被服务端拒绝——改未完成内容用 wenshu_update，改措辞/示例用 wenshu_patch。API 升版生成 vN+1（全量快照）并自动归档旧版；SQL 追加新迁移文件并默认关联旧脚本。changes 一条一个改动点（前端据此知道要改哪些接口）；服务端自动注入「本版变更」（SQL 为 -- 注释），content 里不要重复写。",
        args: {
          id: tool.schema.string().describe("被版本化的文档 id"),
          changes: tool.schema
            .array(tool.schema.string())
            .describe("本版变更清单（必填，一条一个改动点，如「4.3 全部人员记录：出参新增 6 个字段」）"),
          content: tool.schema.string().describe("新版本完整正文（Markdown 或 SQL），不含「本版变更」小节"),
          major_change: tool.schema
            .boolean()
            .optional()
            .describe("API 升版闸门：仅当当前版本已实现且改动较大时传 true（服务端强制校验）"),
          title: tool.schema.string().optional().describe("新版本标题（默认原标题+vN）"),
          status: tool.schema.string().optional().describe("新版本初始状态，默认 pending"),
          date: tool.schema.string().optional().describe("日期 YYYY-MM-DD（默认今天）"),
          version: tool.schema.number().optional().describe("显式版本号（默认旧版本+1）"),
          related: tool.schema.array(tool.schema.string()).optional().describe("覆盖新版本的关联 id 数组"),
          filename: tool.schema.string().optional().describe("SQL 专用：显式文件名"),
        },
        async execute(args) {
          const cfg = requireCfg()
          const changes = (Array.isArray(args.changes) ? args.changes : [])
            .map((c) => String(c).trim())
            .filter(Boolean)
          if (!changes.length) {
            throw new Error("changes 必填：请提供本版变更清单（一条一个改动点，如「4.3 出参新增 6 个字段」）")
          }
          return fmt(await callApi(cfg, "/api/admin/version", "POST", { ...args, changes }))
        },
      }),

      wenshu_update: tool({
        description:
          "文枢：原地完整更新未完成的文档（不升版、不换 ID、不改状态）。仅 pending/draft（未实现接口、未执行 SQL、未定稿方案）可用；保留原版本号与关联，写入 revision+1 与 updated（页面显示「最近修订」）。pending SQL 直接改原脚本、不新建迁移。整篇重写优先用本工具；零散小改动用 wenshu_patch；已实现/已执行的大改动才用 wenshu_new_version。content 传不含 frontmatter/-- @meta 的完整正文（可从 wenshu_read 获取）；changes 可选（提供则替换「本版变更」小节）；expected_revision 可选做乐观锁。",
        args: {
          id: tool.schema.string().describe("文档 id"),
          content: tool.schema.string().describe("新的完整正文（Markdown 或 SQL，不含 frontmatter/-- @meta）"),
          reason: tool.schema.string().describe("更新原因（必填，一句话；写入 git commit）"),
          changes: tool.schema
            .array(tool.schema.string())
            .optional()
            .describe("本版变更清单（可选；提供则替换正文中的「本版变更」小节，一条一个改动点）"),
          expected_revision: tool.schema
            .number()
            .optional()
            .describe("乐观锁：传入当前 revision（wenshu_read 可见），不匹配则拒绝"),
        },
        async execute(args) {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, "/api/admin/update", "POST", args))
        },
      }),

      wenshu_set_status: tool({
        description:
          "文枢：翻转文档状态。SQL: pending→executed；接口: pending→implemented；ADR: accepted；过程文档: draft→final；均可 →archived。注意：必须先获得用户确认再调用（尤其 executed/implemented）。",
        args: {
          id: tool.schema.string().describe("文档 id"),
          status: tool.schema.string().describe("目标状态：executed / implemented / accepted / final / archived / pending / draft"),
        },
        async execute(args) {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, "/api/admin/status", "POST", args))
        },
      }),

      wenshu_update_meta: tool({
        description:
          "文枢：更新文档元数据（topic/domain/summary/related/title）或补写「本版变更」（changes：md 文档，服务端插入/替换正文中的该小节）。related 必须引用已存在的 id；空数组可清空关联。",
        args: {
          id: tool.schema.string().describe("文档 id"),
          topic: tool.schema.string().optional().describe("新主题"),
          domain: tool.schema.string().optional().describe("新领域"),
          summary: tool.schema.string().optional().describe("新摘要"),
          title: tool.schema.string().optional().describe("新标题"),
          related: tool.schema.array(tool.schema.string()).optional().describe("新的关联 id 数组"),
          changes: tool.schema
            .array(tool.schema.string())
            .optional()
            .describe("本版变更清单（md 文档补写/替换「本版变更」小节；一条一个改动点）"),
        },
        async execute(args) {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, "/api/admin/meta", "POST", args))
        },
      }),

      wenshu_patch: tool({
        description:
          "文枢：给当前版文档打补丁（不改版本号、不出新版）。**改文档默认用这个**：措辞/描述/示例/口径等小改动都用补丁（最多 5 条、合计 ≤800 字）；bulk:true 放宽到 40 条、单条 ≤20000 字、合计 ≤20000 字（批量维护、大改但当前版未实现时使用，reason 写明用途）。edits 为精确替换数组：每条 find 必须在正文中恰好命中 1 次；reason 必填并写入 git 记录。仅当「当前版本已实现且改动量较大」才改用 wenshu_new_version（需 major_change=true）；未完成的文档整篇重写用 wenshu_update。未执行（pending）的 SQL 可打补丁，已执行 SQL（请出新迁移）、已被新版替代的历史版本、归档文档会被拒绝。",
        args: {
          id: tool.schema.string().describe("文档 id"),
          reason: tool.schema.string().describe("补丁原因（必填，一句话；写入 git commit）"),
          edits: tool.schema
            .array(tool.schema.object({ find: tool.schema.string(), replace: tool.schema.string() }))
            .describe("精确替换列表：find 必须在正文中恰好命中 1 次，replace 可为空串（删除）"),
          bulk: tool.schema
            .boolean()
            .optional()
            .describe("维护模式：仅用于规范回补等批量修改（放宽至 40 条/20000 字），日常微调不要使用"),
        },
        async execute(args) {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, "/api/admin/patch", "POST", args))
        },
      }),

      wenshu_pending: tool({
        description: "文枢：列出当前所有待办（待执行 SQL / 待实现接口）。",
        args: {},
        async execute() {
          const cfg = requireCfg()
          return fmt(await callApi(cfg, "/api/admin/pending"))
        },
      }),
    },
  }
}
