// Build the Pagefind search index after `astro build`: one record per article (linking out to the source),
// rather than per site page, so a search returns stories, not category pages.
import * as pagefind from 'pagefind';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const items = JSON.parse(readFileSync(join(here, '..', 'src', 'data', 'items.json'), 'utf8'));

const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// Meta values are plain strings; category/form links travel as "slug:Label|slug:Label".
const pairs = (list) => list.map((c) => `${c.slug}:${c.label}`).join('|');

const { index, errors } = await pagefind.createIndex({ forceLanguage: 'en' });
if (errors.length) throw new Error(errors.join('\n'));

for (const i of items) {
  const cats = [...i.categories, ...(i.subcategories ?? [])];
  const forms = i.forms ?? [];
  const meta = { title: i.title, source: i.source, date: i.published, cats: pairs(cats), forms: pairs(forms) };
  if (i.image) meta.image = i.image;
  const attrs = Object.entries(meta).map(([k, v]) => `<span data-pagefind-meta="${k}:${esc(v)}"></span>`).join('');
  const filters = [
    ...cats.map((c) => `<span data-pagefind-filter="category">${esc(c.label)}</span>`),
    ...forms.map((f) => `<span data-pagefind-filter="form">${esc(f.label)}</span>`),
    `<span data-pagefind-filter="source">${esc(i.source)}</span>`,
  ].join('');
  const { errors } = await index.addHTMLFile({
    url: i.link,
    content: `<html lang="en"><body data-pagefind-sort="date:${esc(i.published)}">${attrs}<div data-pagefind-ignore>${filters}</div>
      <h1>${esc(i.title)}</h1><p>${esc(i.excerpt)}</p><p>${esc(i.source)} ${cats.map((c) => esc(c.label)).join(' ')}</p></body></html>`,
  });
  if (errors.length) throw new Error(`${i.link}: ${errors.join('; ')}`);
}

const out = join(here, '..', 'dist', 'pagefind');
const { errors: writeErrors } = await index.writeFiles({ outputPath: out });
if (writeErrors.length) throw new Error(writeErrors.join('\n'));
await pagefind.close();
console.log(`search: indexed ${items.length} articles -> ${out}`);
