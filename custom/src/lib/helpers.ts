export const statusText = {
  pending: '待处理',
  executed: '已执行',
  implemented: '已实现',
  draft: '草稿',
  final: '定稿',
  accepted: '已采纳',
  archived: '已归档',
} as Record<string, string>;

export const typeText = {
  sql: 'SQL',
  api: 'API',
  adr: 'ADR',
  analysis: '分析',
  scheme: '方案',
  report: '报告',
  other: '其他',
} as Record<string, string>;

export const docUrl = (id: string) =>
  '/docs/' + id.split('/').map(encodeURIComponent).join('/') + '/';

export const topicUrl = (topic: string) => '/topics/' + encodeURIComponent(topic) + '/';

export const fmtDate = (d: Date) => d.toISOString().slice(0, 10);

export const chainTitle = (title: string) => title.replace(/[（(]\s*v\d+\s*[)）]\s*$/, '').trim();

export function versionFamilies(entries: any[]) {
  const byId = new Map(entries.map((e) => [e.id, e]));
  const byLower = new Map(entries.map((e) => [e.id.toLowerCase(), e]));
  const find = (id: string) => byId.get(id) ?? byLower.get(id.toLowerCase());
  const rootOf = (e: any): string => {
    let cur = e;
    const seen = new Set([e.id]);
    while (cur?.data?.supersedes) {
      const prev = find(cur.data.supersedes);
      if (!prev || seen.has(prev.id)) break;
      seen.add(prev.id);
      cur = prev;
    }
    return cur.id;
  };
  const families = new Map<string, any[]>();
  for (const e of entries) {
    if (!e.data.version) continue;
    const root = rootOf(e);
    const arr = families.get(root) ?? [];
    arr.push(e);
    families.set(root, arr);
  }
  for (const arr of families.values()) arr.sort((a, b) => (a.data.version ?? 0) - (b.data.version ?? 0));
  return { rootOf, families };
}
