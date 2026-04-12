import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';

const chapters = defineCollection({
  loader: glob({ pattern: '(ch[0-9]+-|appendix-)*.md', base: '../book' }),
});

export const collections = { chapters };
