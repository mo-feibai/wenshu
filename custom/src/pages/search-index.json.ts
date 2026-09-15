import { getCollection } from 'astro:content';
import { docUrl } from '../lib/helpers';

export async function GET() {
  const entries = await getCollection('docs');
  const items = entries.map((e) => ({
    id: e.id,
    title: e.data.title,
    project: e.data.project,
    topic: e.data.topic,
    type: e.data.type,
    status: e.data.status,
    summary: e.data.summary ?? '',
    url: docUrl(e.id),
  }));
  return new Response(JSON.stringify(items), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}
