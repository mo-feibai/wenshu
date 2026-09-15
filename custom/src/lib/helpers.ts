export const statusText = {
  pending: '待处理',
  executed: '已执行',
  implemented: '已实现',
  draft: '草稿',
  final: '定稿',
  accepted: '已采纳',
  archived: '归档',
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
