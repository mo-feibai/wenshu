/**
 * dsh-wenshu —— 文枢（WenShu）文档库的 DSH 工具层。
 *
 * 职责（与 wenshu skill 分工）：
 *   - 本插件只提供「能力 + 硬边界」：14 个 wenshu_* 工具，以及禁止绕过工具直写文档库的守卫。
 *   - 流程约束（先查重、补丁还是重写、状态翻转要用户确认、API 文档格式规范）留在 skill 里。
 *
 * 鉴权：优先读 harness 凭据中心（~/.dsh/.credentials.yaml）的 refs.WENSHU_API_KEY；
 * 首次运行会把旧 opencode 配置 ~/.config/opencode/wenshu.json 里的 apiToken 迁移进凭据中心，
 * 迁移失败则退回环境变量 / 旧配置读取，密钥不会写进 profile 的补丁文件。
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { createGuard } from "./lib/guard.js";

export const name = "dsh-wenshu";

/** 只需要 tools 服务；credentials / systemPrompt 都按「可能晚于本插件挂载」动态获取。 */
export const inject = ["tools"];

export const Config = z.object({
  baseUrl: z
    .string()
    .default("http://127.0.0.1:8903")
    .description("文枢服务地址（不带尾部斜杠）"),
  siteUrl: z
    .string()
    .default("http://192.168.0.71:8903")
    .description("文档站点根地址，仅用于提示文案"),
  credentialRef: z
    .string()
    .default("WENSHU_API_KEY")
    .description("凭据中心里保存文枢 API Key 的引用名"),
  tokenEnv: z
    .string()
    .default("WENSHU_API_KEY")
    .description("凭据中心未命中时回退读取的环境变量名"),
  legacyConfigPath: z
    .string()
    .default(join(homedir(), ".config", "opencode", "wenshu.json"))
    .description("旧 opencode 文枢配置路径（用于迁移 apiToken）"),
  migrateLegacyToken: z
    .boolean()
    .default(true)
    .description("凭据中心没有 key 时，是否把旧配置里的 apiToken 自动迁移进去"),
  timeoutMs: z.number().default(15000).description("单次请求超时（毫秒）"),
  maxOutputChars: z
    .number()
    .default(14000)
    .description("工具输出最大字符数，超出截断"),
  guardEnabled: z
    .boolean()
    .default(true)
    .description("是否拦截对文档库的直接写入（skill 承诺的硬拦截）"),
  extraWriteTools: z
    .array(z.string())
    .default([])
    .description("额外需要按路径检查的写类工具名（如第三方插件提供的 write_file）"),
  extraShellTools: z
    .array(z.string())
    .default([])
    .description("额外需要按命令检查的 shell 工具名"),
  extraDocsPaths: z
    .array(z.string())
    .default([])
    .description("额外的受保护路径（小写、用 / 分隔的片段即可）"),
});

// ---------------------------------------------------------------------------
// 基础工具函数
// ---------------------------------------------------------------------------

function safeParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function errorText(error) {
  if (error instanceof Error) {
    if (error.name === "AbortError" || error.name === "TimeoutError") return "请求超时";
    return error.message;
  }
  return String(error);
}

function fmt(text, max) {
  const cap = Number.isFinite(max) && max > 0 ? max : 14000;
  return text.length > cap ? text.slice(0, cap) + "…(已截断)" : text;
}

/** 合并调用方 AbortSignal 与超时，返回可清理的句柄（不依赖 AbortSignal.any 的 Node 版本）。 */
function withTimeout(signal, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort(new Error(`请求超时（${ms}ms）`));
  }, ms);
  timer.unref?.();
  const onAbort = () => {
    try {
      controller.abort(signal?.reason);
    } catch {
      controller.abort();
    }
  };
  if (signal) {
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  }
  return {
    signal: controller.signal,
    dispose() {
      clearTimeout(timer);
      signal?.removeEventListener?.("abort", onAbort);
    },
  };
}

// ---------------------------------------------------------------------------
// 连接信息：凭据中心 > 环境变量 > 旧 opencode 配置（并做一次性迁移）
// ---------------------------------------------------------------------------

function readLegacyConfig(config) {
  try {
    const raw = JSON.parse(readFileSync(config.legacyConfigPath, "utf8"));
    if (raw && typeof raw.baseUrl === "string") {
      return {
        baseUrl: raw.baseUrl,
        apiToken: typeof raw.apiToken === "string" ? raw.apiToken.trim() : "",
      };
    }
  } catch {
    // 没有旧配置是正常情况
  }
  return null;
}

let legacyMigrationAttempted = false;

async function resolveConnection(ctx, config) {
  const credentials = typeof ctx.get === "function" ? ctx.get("credentials") : undefined;
  let token = "";

  if (credentials && typeof credentials.resolve === "function") {
    try {
      const resolved = await credentials.resolve(config.credentialRef);
      if (resolved && typeof resolved.value === "string" && resolved.value.trim()) {
        token = resolved.value.trim();
      }
    } catch {
      // 凭据服务不可用时继续走后面的来源
    }
  }

  if (!token) token = String(process.env[config.tokenEnv] ?? "").trim();

  const legacy = readLegacyConfig(config);
  if (!token && legacy && legacy.apiToken) {
    token = legacy.apiToken;
    if (
      config.migrateLegacyToken &&
      !legacyMigrationAttempted &&
      credentials &&
      typeof credentials.set === "function"
    ) {
      legacyMigrationAttempted = true;
      try {
        await credentials.set(config.credentialRef, token);
        ctx.logger?.info?.(
          `[wenshu] 已把旧配置里的 apiToken 迁移进凭据中心（refs.${config.credentialRef}）`
        );
      } catch (error) {
        ctx.logger?.warn?.(
          `[wenshu] 迁移 apiToken 到凭据中心失败，本次继续使用旧配置：${errorText(error)}`
        );
      }
    }
  }

  const baseUrl = String(config.baseUrl || legacy?.baseUrl || "").replace(/\/+$/, "");
  return { baseUrl, token, siteUrl: String(config.siteUrl || "").replace(/\/+$/, "") };
}

async function callApi(ctx, config, exec, path, method = "GET", body) {
  const connection = await resolveConnection(ctx, config);
  if (!connection.baseUrl) {
    return JSON.stringify({
      error: "文枢服务地址未配置：请在插件 Config 里设置 baseUrl，或设置环境变量 WENSHU_BASE_URL。",
    });
  }
  if (!connection.token) {
    return JSON.stringify({
      error:
        `文枢 API Key 缺失：请把 key 写入凭据中心（refs.${config.credentialRef}），` +
        `或设置环境变量 ${config.tokenEnv}，或在 ${config.legacyConfigPath} 里配置 apiToken。`,
    });
  }

  const timeout = withTimeout(exec?.signal, config.timeoutMs);
  try {
    const response = await fetch(connection.baseUrl + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": connection.token,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: timeout.signal,
    });
    const text = await response.text();
    return JSON.stringify({ status: response.status, result: safeParse(text) });
  } catch (error) {
    return JSON.stringify({
      error: `文枢服务不可达（${connection.baseUrl}）：${errorText(error)}`,
    });
  } finally {
    timeout.dispose();
  }
}

function installGuard(ctx, config) {
  if (!config.guardEnabled) return;
  const guardReason = createGuard(config);

  // 1) 执行前瀑布：返回 deny 决策，可携带结构化原因。
  ctx.on("tools/pre-execute", async (exec, next) => {
    let reason = "";
    try {
      reason = guardReason(exec?.name, exec?.arguments);
    } catch {
      // 守卫自身异常绝不阻塞无关工具
      return next();
    }
    if (reason) {
      return {
        kind: "deny",
        reason,
        info: { name: "WenShuGuard", code: "WENSHU_DOCS_PROTECTED" },
      };
    }
    return next();
  });

  // 2) 同步守卫：老 profile 或其它调用路径的兜底。
  if (typeof ctx.tools.guard === "function") {
    const register = () => {
      try {
        return ctx.tools.guard((exec) => {
          try {
            return guardReason(exec?.name, exec?.arguments) || undefined;
          } catch {
            return undefined;
          }
        });
      } catch {
        return undefined;
      }
    };
    try {
      ctx.effect(register, "dsh-wenshu: docs guard");
    } catch {
      register();
    }
  }
}

// ---------------------------------------------------------------------------
// 工具注册
// ---------------------------------------------------------------------------

const TEXT_OUTPUT = {
  schema: {
    type: "object",
    additionalProperties: false,
    properties: {
      text: { type: "string", required: true },
    },
  },
  render: (_args, value) => [{ type: "text", text: value.text }],
};

function buildTools(config) {
  const guardMessage = config.guardEnabled
    ? "（文档库直写会被插件拒绝，一律走 wenshu_* 工具）"
    : "";

  return [
    {
      name: "wenshu_context",
      description:
        "文枢：读取全局文档库结构（项目→主题→文档，含状态/版本/日期与待办），可用 project/topic/query 过滤。" +
        "生成或更新文档前先调用，用于确定 project/topic 命名与可关联的文档 id。返回的 url 是站点路径。",
      parameters: {
        project: { type: "string", description: "按项目过滤，如 xingtai-smart-store-api" },
        topic: { type: "string", description: "按主题过滤，如 订餐支付" },
        query: { type: "string", description: "关键词过滤（标题/主题/摘要/正文片段）" },
      },
      async execute(args, exec, send) {
        const qs = new URLSearchParams();
        if (args.project) qs.set("project", args.project);
        if (args.topic) qs.set("topic", args.topic);
        if (args.query) qs.set("query", args.query);
        const q = qs.toString();
        return send(`/api/admin/context${q ? `?${q}` : ""}`);
      },
    },
    {
      name: "wenshu_find",
      description:
        "文枢：语义检索（本地嵌入模型），按「意思」找文档，补 wenshu_context 子串匹配的短板。" +
        "新建文档前用它查重，或找可关联的旧文档；相似度 <0.6 一般不算相关。语义服务不可用时会返回 503，此时改用 wenshu_context 拆关键词检索。",
      parameters: {
        query: {
          type: "string",
          required: true,
          description: "自然语言查询，如「订餐退款怎么处理」「考勤导出最大跨度限制」",
        },
        k: { type: "integer", description: "返回条数，默认 5（1–20）" },
        project: { type: "string", description: "限定项目，如 xingtai-smart-store-api" },
        topic: { type: "string", description: "限定主题，如 订餐支付" },
        type: {
          type: "string",
          description: "限定类型：sql/api/openapi/adr/analysis/scheme/report/other",
        },
      },
      async execute(args, exec, send) {
        const k = Math.max(1, Math.min(20, Math.round(Number(args.k) || 5)));
        const qs = new URLSearchParams({ query: String(args.query), k: String(k) });
        if (args.project) qs.set("project", args.project);
        if (args.topic) qs.set("topic", args.topic);
        if (args.type) qs.set("type", args.type);
        return send(`/api/admin/semantic?${qs.toString()}`);
      },
    },
    {
      name: "wenshu_similar",
      description:
        "文枢：找与某篇文档最相似的其它文档（本地嵌入模型）。用于判断是否重复、给 related 找候选、审阅时找内容重叠文档。" +
        "返回的 already_related 表示该文档已在 related 里。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id（文档库相对路径去掉扩展名）" },
        k: { type: "integer", description: "返回条数，默认 5（1–20）" },
      },
      async execute(args, exec, send) {
        const k = Math.max(1, Math.min(20, Math.round(Number(args.k) || 5)));
        const qs = new URLSearchParams({ id: String(args.id), k: String(k) });
        return send(`/api/admin/similar?${qs.toString()}`);
      },
    },
    {
      name: "wenshu_read",
      description:
        "文枢：读取单篇文档（元数据 + 正文 + revision）。id 例如 xingtai-smart-store-api/餐厅/sql/迁移-20260912-订餐支付唯一键。" +
        "改文档前先读原文；wenshu_update 的 content 必须是这里的完整正文（去掉 frontmatter/-- @meta）。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id（文档库相对路径去掉扩展名）" },
      },
      async execute(args, exec, send) {
        const max = Math.max(config.maxOutputChars || 14000, 30000);
        return send(`/api/admin/doc?id=${encodeURIComponent(String(args.id))}`, "GET", undefined, max);
      },
    },
    {
      name: "wenshu_create",
      description:
        "文枢：新建文档。sql 类型自动命名 迁移-YYYYMMDD-<topic>.sql（可用 filename 覆盖）；api/openapi 等写入 <title>.md 的 frontmatter，" +
        "写入后服务自动重建站点并 git 提交。related 只能引用已存在的文档 id（先用 wenshu_context 查）。" +
        "type=api/openapi 必须遵守《API 文档规范（人读 + OpenAPI + curl）》（id=通用规范/文档/API 文档规范（人读 + OpenAPI + curl）），" +
        "先 wenshu_read 读它再动手；服务端会补齐缺失小节并在返回值 format 里报缺口。" +
        guardMessage,
      parameters: {
        project: { type: "string", required: true, description: "项目名，如 xingtai-smart-store-api" },
        topic: { type: "string", required: true, description: "主题（功能主题，如 订餐支付）" },
        type: {
          type: "string",
          required: true,
          description: "文档类型：sql / api / openapi / adr / analysis / scheme / report / other",
        },
        title: {
          type: "string",
          required: true,
          description: "标题；api/openapi 类型即文件名（OpenAPI 规格以 -OpenAPI 结尾）",
        },
        content: {
          type: "string",
          required: true,
          description: "正文：sql 为原始 SQL；其他为 Markdown（不含 frontmatter）",
        },
        domain: { type: "string", description: "领域/子目录，如 餐厅（可省略）" },
        status: { type: "string", description: "初始状态：sql[pending|executed] api/openapi[pending|implemented] 等" },
        related: {
          type: "array",
          items: { type: "string" },
          description: "关联文档 id 数组（api 必须含配套 openapi，反之亦然）",
        },
        summary: { type: "string", description: "一句话摘要" },
        date: { type: "string", description: "日期 YYYY-MM-DD（默认今天）" },
        version: { type: "integer", description: "版本号（一般 API 文档用）" },
        filename: { type: "string", description: "显式文件名（sql 类型专用）" },
        scaffold: { type: "boolean", description: "api/openapi 专用：默认 true 自动补规范骨架" },
      },
      async execute(args, exec, send) {
        return send("/api/admin/create", "POST", args);
      },
    },
    {
      name: "wenshu_new_version",
      description:
        "文枢：为已有文档出新版本（服务端强制门槛）。api/openapi 必须当前 status=implemented 且显式 major_change=true 并填 changes；" +
        "SQL 必须当前 status=executed（未执行的 SQL 用 wenshu_update 原地改）。服务端自动注入「本版变更」，content 不要重复写。" +
        "只改措辞/示例请用 wenshu_patch；未完成文档整篇重写用 wenshu_update。",
      parameters: {
        id: { type: "string", required: true, description: "被版本化的文档 id" },
        changes: {
          type: "array",
          items: { type: "string" },
          required: true,
          description: "本版变更清单（必填，一条一个改动点）",
        },
        content: {
          type: "string",
          required: true,
          description: "新版本完整正文（Markdown 或 SQL），不含「本版变更」小节",
        },
        major_change: {
          type: "boolean",
          description: "API 升版闸门：仅当当前版本已实现且改动较大时传 true",
        },
        title: { type: "string", description: "新版本标题（默认原标题+vN）" },
        status: { type: "string", description: "新版本初始状态，默认 pending" },
        date: { type: "string", description: "日期 YYYY-MM-DD（默认今天）" },
        version: { type: "integer", description: "显式版本号（默认旧版本+1）" },
        related: {
          type: "array",
          items: { type: "string" },
          description: "覆盖新版本的关联 id 数组",
        },
        filename: { type: "string", description: "SQL 专用：显式文件名" },
      },
      async execute(args, exec, send) {
        const changes = (Array.isArray(args.changes) ? args.changes : [])
          .map((item) => String(item).trim())
          .filter(Boolean);
        if (changes.length === 0) {
          return {
            text: JSON.stringify({
              error: "changes 必填：请提供本版变更清单（一条一个改动点，如「4.3 出参新增 6 个字段」）",
            }),
          };
        }
        return send("/api/admin/version", "POST", { ...args, changes });
      },
    },
    {
      name: "wenshu_update",
      description:
        "文枢：原地完整更新未完成文档（不升版、不换 id、不改状态），仅 pending/draft 可用；写入 revision+1。" +
        "整篇重写优先用它，零散小改用 wenshu_patch，已实现/已执行的大改才用 wenshu_new_version。" +
        "content 传不含 frontmatter/-- @meta 的完整正文（可从 wenshu_read 获取）；expected_revision 可做乐观锁。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id" },
        content: {
          type: "string",
          required: true,
          description: "新的完整正文（Markdown 或 SQL，不含 frontmatter/-- @meta）",
        },
        reason: { type: "string", required: true, description: "更新原因（必填，写入 git commit）" },
        changes: {
          type: "array",
          items: { type: "string" },
          description: "本版变更清单（可选，提供则替换正文中的「本版变更」小节）",
        },
        expected_revision: {
          type: "integer",
          description: "乐观锁：传当前 revision（wenshu_read 可见），不匹配则拒绝",
        },
      },
      async execute(args, exec, send) {
        return send("/api/admin/update", "POST", args);
      },
    },
    {
      name: "wenshu_set_status",
      description:
        "文枢：翻转文档状态。SQL: pending→executed；接口: pending→implemented；ADR: accepted；过程文档: draft→final；均可 →archived（单向终态）。" +
        "调用前必须先获得用户确认（尤其 executed/implemented）。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id" },
        status: {
          type: "string",
          required: true,
          description: "目标状态：executed / implemented / accepted / final / archived / pending / draft",
        },
      },
      async execute(args, exec, send) {
        return send("/api/admin/status", "POST", args);
      },
    },
    {
      name: "wenshu_update_meta",
      description:
        "文枢：更新文档元数据（topic/domain/summary/title/related）或补写「本版变更」小节（changes）。" +
        "related 必须引用已存在的 id；传空数组可清空关联。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id" },
        topic: { type: "string", description: "新主题" },
        domain: { type: "string", description: "新领域" },
        summary: { type: "string", description: "新摘要" },
        title: { type: "string", description: "新标题" },
        related: {
          type: "array",
          items: { type: "string" },
          description: "新的关联 id 数组",
        },
        changes: {
          type: "array",
          items: { type: "string" },
          description: "本版变更清单（补写/替换「本版变更」小节）",
        },
      },
      async execute(args, exec, send) {
        return send("/api/admin/meta", "POST", args);
      },
    },
    {
      name: "wenshu_patch",
      description:
        "文枢：给当前版文档打补丁（不改版本号、不出新版），**改文档默认用它**。措辞/描述/示例/口径等小改动都用补丁" +
        "（最多 5 条、合计 ≤800 字）；bulk:true 放宽到 40 条/20000 字（批量维护用，reason 写明用途）。" +
        "edits 是精确替换数组，每条 find 必须在正文中恰好命中 1 次；reason 必填并写入 git 记录。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id" },
        reason: { type: "string", required: true, description: "补丁原因（必填，写入 git commit）" },
        edits: {
          type: "array",
          required: true,
          description: "精确替换列表：find 必须在正文中恰好命中 1 次，replace 可为空串（删除）",
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              find: { type: "string", required: true, description: "被替换的原文（须唯一命中）" },
              replace: { type: "string", required: true, description: "替换后的文本，可为空串" },
            },
          },
        },
        bulk: {
          type: "boolean",
          description: "维护模式：放宽到 40 条/20000 字，日常微调不要用",
        },
      },
      async execute(args, exec, send) {
        return send("/api/admin/patch", "POST", args);
      },
    },
    {
      name: "wenshu_review",
      description:
        "文枢：扫描全库输出淘汰候选清单（只读）。按「已失效 + 闲置时长」分类（历史版本/独立归档/僵尸待办/孤儿/一次性/废弃当前版），" +
        "每项含类别/阶段/闲置天数/建议动作。executed SQL、当前版、被引用、有附件的文档自动排除。",
      parameters: {
        overrides: {
          type: "string",
          description: "测试用：阈值覆盖 JSON，如 {\"T_A1\":1,\"T_B\":1}（日常不要传）",
        },
      },
      async execute(args, exec, send) {
        const qs = args.overrides
          ? `?overrides=${encodeURIComponent(String(args.overrides))}`
          : "";
        return send(`/api/admin/review${qs}`);
      },
    },
    {
      name: "wenshu_retire",
      description:
        "文枢：文档生命周期操作。action=stale 标记过期（需先在候选清单，或 force=true+reason）；" +
        "action=retired 淘汰隐藏（需已 stale 满 7 天，或 force=true+reason）；action=restore 恢复。" +
        "已淘汰文档只读（patch/update/set_status/update_meta 均拒绝）。",
      parameters: {
        id: { type: "string", required: true, description: "文档 id" },
        action: { type: "string", required: true, description: "stale / retired / restore" },
        force: { type: "boolean", description: "强制跳过候选/冷静期校验（需配 reason）" },
        reason: { type: "string", description: "原因（force 时必填，写入 git 记录）" },
      },
      async execute(args, exec, send) {
        return send("/api/admin/retire", "POST", args);
      },
    },
    {
      name: "wenshu_lint",
      description:
        "文枢：API/OpenAPI 文档格式体检（只读）。按《API 文档规范》逐篇检查必填小节与「（待填写）」占位，返回缺失清单。" +
        "交付前用它排队清零；只报机械可判定项，不评价内容质量。",
      parameters: {
        project: { type: "string", description: "只看某个项目，如 plat_jk" },
        type: { type: "string", description: "只看某类型：api / openapi" },
        id: { type: "string", description: "只看某一篇（文档 id）" },
      },
      async execute(args, exec, send) {
        const qs = new URLSearchParams();
        for (const key of ["project", "type", "id"]) {
          const value = args[key];
          if (value) qs.set(key, String(value));
        }
        const q = qs.toString();
        return send(`/api/admin/lint${q ? `?${q}` : ""}`);
      },
    },
    {
      name: "wenshu_pending",
      description: "文枢：列出当前所有待办（待执行 SQL / 待实现接口）。",
      parameters: {},
      async execute(args, exec, send) {
        return send("/api/admin/pending");
      },
    },
  ];
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

export function apply(ctx, config = {}) {
  const resolved = {
    baseUrl: config.baseUrl || "http://127.0.0.1:8903",
    siteUrl: config.siteUrl || "http://192.168.0.71:8903",
    credentialRef: config.credentialRef || "WENSHU_API_KEY",
    tokenEnv: config.tokenEnv || "WENSHU_API_KEY",
    legacyConfigPath: config.legacyConfigPath || join(homedir(), ".config", "opencode", "wenshu.json"),
    migrateLegacyToken: config.migrateLegacyToken !== false,
    timeoutMs: Number(config.timeoutMs) || 15000,
    maxOutputChars: Number(config.maxOutputChars) || 14000,
    guardEnabled: config.guardEnabled !== false,
    extraWriteTools: Array.isArray(config.extraWriteTools) ? config.extraWriteTools : [],
    extraShellTools: Array.isArray(config.extraShellTools) ? config.extraShellTools : [],
    extraDocsPaths: Array.isArray(config.extraDocsPaths) ? config.extraDocsPaths : [],
  };

  const tools = buildTools(resolved);

  for (const definition of tools) {
    ctx.tools.register(
      defineTool({
        name: definition.name,
        description: definition.description,
        parameters: definition.parameters,
        output: TEXT_OUTPUT,
        async execute(args, exec) {
          const send = async (path, method = "GET", body, max) => ({
            text: fmt(await callApi(ctx, resolved, exec, path, method, body), max ?? resolved.maxOutputChars),
          });
          return definition.execute(args ?? {}, exec, send);
        },
      })
    );
  }

  // 系统提示：把「文档库只能走 wenshu_*」这条硬边界告诉模型，避免它先撞守卫。
  const systemPrompt = typeof ctx.get === "function" ? ctx.get("systemPrompt") : undefined;
  if (systemPrompt && typeof systemPrompt.section === "function") {
    try {
      systemPrompt.section({
        name: "tool:wenshu",
        order: systemPrompt.getSectionOrder?.("TOOLS_SDK") ?? 5000,
        text:
          "文枢文档库（WSL /opt/wenshu/docs）只能通过 wenshu_* 工具读写：直接 edit/write 文档库文件、或 shell 里对文档库做写入，都会被插件硬拒绝。" +
          "新建前先 wenshu_context + wenshu_find 查重；微调用 wenshu_patch，未完成文档整篇重写用 wenshu_update，已实现/已执行的大改才用 wenshu_new_version；状态翻转前先问用户。",
      });
    } catch {
      // systemPrompt 未挂载时静默跳过
    }
  }

  installGuard(ctx, resolved);
}
