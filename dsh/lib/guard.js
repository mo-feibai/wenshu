/**
 * 文档库写保护守卫（纯函数，无外部依赖，便于单测）。
 *
 * 判定思路：
 *   1. 写类工具（edit/write/patch/…）只看「路径类参数」，避免正文里出现文档库路径被误判；
 *      patch 类工具额外扫描补丁文本，因为路径藏在补丁正文里。
 *   2. shell 工具只看命令文本：命中文档库路径 + 有写语义 → 拒；命中但整条链是只读命令 → 放行；
 *      命中却不是只读 → 拒（严格档，用户已选定）。
 */

/** 文档库路径标记（小写、/ 分隔）。WSL 原生路径与 Windows UNC 路径归一后都能命中。 */
export const DOCS_MARKERS = ["/opt/wenshu/docs", "wsl.localhost/debian/opt/wenshu/docs"];

export const DEFAULT_WRITE_TOOLS = [
  "write",
  "edit",
  "patch",
  "multiedit",
  "apply_patch",
  "ops_apply_patch",
  "ops_write_file",
  "ops_edit_file",
  "write_file",
  "edit_file",
];

export const DEFAULT_SHELL_TOOLS = [
  "bash",
  "sh",
  "shell",
  "zsh",
  "pwsh",
  "pwsh_persistent",
  "powershell",
  "ops_bash",
  "ops_shell",
  "ops_pwsh",
];

export const GUARD_MESSAGE_WRITE =
  "文枢文档库受保护：禁止直接编辑文档库文件。请改用 wenshu_* 工具——新建用 wenshu_create；" +
  "原地改未完成文档（pending/draft）用 wenshu_update；错别字/措辞微调用 wenshu_patch（不升版）；" +
  "已实现/已执行的大改动才用 wenshu_new_version；翻转状态用 wenshu_set_status；" +
  "改主题/摘要/关联用 wenshu_update_meta。";

export const GUARD_MESSAGE_BASH_WRITE =
  "文枢文档库受保护：shell 里禁止对文档库做写入/变更（重定向、tee、sed -i、cp/mv/rm、git 写操作等）。" +
  "请改用 wenshu_* 工具；只读查看请用不带重定向的 cat/ls/grep/rg 等。";

export const GUARD_MESSAGE_BASH_STRICT =
  "文枢文档库受保护：该命令涉及文档库路径，但不是只读命令。" +
  "只允许 cat/ls/grep/rg/head/tail/wc/find/stat/git log|status|diff 等只读查看；" +
  "写入请改用 wenshu_create / wenshu_update / wenshu_patch / wenshu_new_version / wenshu_set_status / wenshu_update_meta。";

const WRITE_CMD =
  /(>>?|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\btouch\b|\bchmod\b|\bchown\b|\btruncate\b|\bdd\b|python3?\s+-c|node\s+-e|\bgit(\s+-C\s+\S+)?\s+(add|commit|checkout|reset|apply|clean|rm|restore|stash|pull|push|merge|rebase)\b|\bset-content\b|\bnew-item\b|\bremove-item\b|\bmove-item\b|\bcopy-item\b|\bout-file\b|\badd-content\b|\bclear-content\b)/i;

const READONLY_CMD =
  /^\s*(cat|bat|ls|dir|grep|rg|head|tail|wc|find|file|stat|tree|du|md5sum|sha1sum|sha256sum|sed\s+-n|less|more|git(\s+-C\s+\S+)?\s+(log|status|diff|show|ls-files|rev-parse|blame))\b/i;

const PATH_ARG = /(path|file|target|dest|destination|dir|directory|output|out_file|filename)$/i;

/** 归一化：小写 + 反斜杠转正斜杠（opencode 版只替换双反斜杠，是 bug，这里修正）。 */
export function normalizePathLike(value) {
  return String(value).toLowerCase().replace(/\\/g, "/");
}

export function buildDocsMatchers(extraDocsPaths = []) {
  const markers = [...DOCS_MARKERS];
  for (const extra of extraDocsPaths) {
    const normalized = normalizePathLike(extra).trim();
    if (normalized) markers.push(normalized);
  }
  return markers;
}

export function touchesDocs(value, markers) {
  if (typeof value !== "string" || value === "") return false;
  const normalized = normalizePathLike(value);
  return markers.some((marker) => normalized.includes(marker));
}

/**
 * 扫描工具参数里的文档库路径。
 * @param args 工具入参
 * @param markers 归一化后的路径标记
 * @param scanAll true 时扫描所有字符串值（patch 类工具，路径可能藏在补丁正文里）
 */
export function scanPathArguments(args, markers, scanAll = false, depth = 0) {
  if (depth > 4 || args === null || args === undefined) return "";
  if (typeof args === "string") return scanAll && touchesDocs(args, markers) ? args : "";
  if (Array.isArray(args)) {
    for (const item of args) {
      const hit = scanPathArguments(item, markers, scanAll, depth + 1);
      if (hit) return hit;
    }
    return "";
  }
  if (typeof args !== "object") return "";
  for (const [key, value] of Object.entries(args)) {
    if (typeof value === "string") {
      const isPath = PATH_ARG.test(key);
      if ((isPath || scanAll) && touchesDocs(value, markers)) return value;
      continue;
    }
    if (value && typeof value === "object") {
      const hit = scanPathArguments(value, markers, scanAll, depth + 1);
      if (hit) return hit;
    }
  }
  return "";
}

/** 去掉命令开头的 `cd somewhere &&` 前缀，便于识别 `cd docs && ls` 这类只读组合。 */
export function stripCdPrefix(command) {
  return String(command)
    .trim()
    .replace(/^(cd\s+[^\s&;|]+\s*(&&|;)\s*)+/i, "")
    .trim();
}

/** 去掉 `2>&1`、`>/dev/null`、`&>nul` 这类无害的 fd 重定向，避免被当成写文件。 */
const BENIGN_REDIRECT = /(?:\d*)>>?\s*(?:&\d+|&-|\/dev\/null\b|nul\b)|&>\s*(?:\/dev\/null\b|nul\b)/gi;

export function stripBenignRedirects(command) {
  return String(command ?? "").replace(BENIGN_REDIRECT, " ");
}

/** 整条命令链是否全部由只读命令组成（支持 | 、&& 、; 、|| 组合）。 */
export function isReadonlyChain(command) {
  const segments = String(command)
    .split(/\|\||&&|;|\|/)
    .map((segment) => segment.trim())
    .filter(Boolean);
  if (segments.length === 0) return false;
  return segments.every(
    (segment) => READONLY_CMD.test(segment) || /^(pwd|echo|printf)\b/i.test(segment)
  );
}

export function decideShell(command, markers) {
  const raw = String(command ?? "");
  if (!touchesDocs(raw, markers)) return "";
  const cleaned = stripBenignRedirects(raw);
  if (WRITE_CMD.test(cleaned)) return GUARD_MESSAGE_BASH_WRITE;
  const noCd = stripCdPrefix(cleaned);
  if (isReadonlyChain(noCd)) return "";
  return GUARD_MESSAGE_BASH_STRICT;
}

/**
 * 生成守卫判定函数：返回空串表示放行，返回非空字符串即为拒绝原因。
 * @param options { guardEnabled, extraWriteTools, extraShellTools, extraDocsPaths }
 */
export function createGuard(options = {}) {
  const markers = buildDocsMatchers(options.extraDocsPaths ?? []);
  const writeTools = new Set(
    [...DEFAULT_WRITE_TOOLS, ...(options.extraWriteTools ?? [])].map((item) => String(item).toLowerCase())
  );
  const shellTools = new Set(
    [...DEFAULT_SHELL_TOOLS, ...(options.extraShellTools ?? [])].map((item) => String(item).toLowerCase())
  );

  return function guardReason(toolName, args) {
    if (options.guardEnabled === false) return "";
    const tool = String(toolName ?? "").toLowerCase();
    if (!tool || tool.startsWith("wenshu_")) return "";
    if (args === null || typeof args !== "object") return "";

    if (writeTools.has(tool)) {
      const scanAll = /patch/.test(tool);
      return scanPathArguments(args, markers, scanAll) ? GUARD_MESSAGE_WRITE : "";
    }

    if (shellTools.has(tool) && typeof args.command === "string") {
      // 带 script_path 的调用先放行：命令在脚本文件里，本层看不到内容（与 dsh-ops 下载守卫一致）。
      if (typeof args.script_path === "string" && args.script_path !== "") return "";
      return decideShell(args.command, markers);
    }

    return "";
  };
}
