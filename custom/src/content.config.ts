import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

export const collections = {
  docs: defineCollection({
    loader: glob({ pattern: '**/*.md', base: './src/content/docs' }),
    schema: z.object({
      title: z.string(),
      project: z.string(),
      domain: z.string().optional(),
      topic: z.string(),
      type: z.enum(['sql', 'api', 'adr', 'analysis', 'scheme', 'report', 'other']),
      status: z.enum(['pending', 'executed', 'implemented', 'draft', 'final', 'accepted', 'archived']),
      version: z.number().optional(),
      date: z.coerce.date(),
      revision: z.number().optional(),
      updated: z.coerce.date().optional(),
      summary: z.string().optional(),
      related: z.array(z.string()).default([]),
      supersedes: z.string().optional(),
      superseded_by: z.string().optional(),
      attachment: z.string().optional(),
      attachments: z.array(z.string()).default([]),
    }),
  }),
};
