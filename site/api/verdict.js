// POST /api/verdict  {item_id, title, link, shown: [slug], add: [slug], remove: [slug], reason}
// Owner only. Stores one row per added/removed tag, or a single 'confirm' row when nothing changed.
// The publisher pulls these into the pipeline, where they overrule Jev for that article.
import { neon } from '@neondatabase/serverless';
import { isOwner } from './_owner.js';

const SLUG = /^[a-z0-9-]{1,60}$/;
const slugs = (v) => (Array.isArray(v) ? v.filter((s) => typeof s === 'string' && SLUG.test(s)).slice(0, 40) : []);
const text = (v, n) => (typeof v === 'string' && v.trim() ? v.trim().slice(0, n) : null);
const json = (body, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });

export async function POST(request) {
  if (!(await isOwner(request))) return json({ error: 'not owner' }, 403);
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }
  const id = typeof b.item_id === 'string' && /^[0-9a-f]{40}$/.test(b.item_id) ? b.item_id : null;
  if (!id) return json({ error: 'bad item_id' }, 400);

  const shown = slugs(b.shown), add = slugs(b.add), remove = slugs(b.remove);
  const title = text(b.title, 300), link = text(b.link, 1000), reason = text(b.reason, 2000);
  const rows = [...remove.map((t) => ['remove', t]), ...add.map((t) => ['add', t])];
  if (!rows.length) rows.push(['confirm', null]);

  const sql = neon(process.env.DATABASE_URL);
  await sql.transaction(rows.map(([action, tag]) => sql`
    INSERT INTO verdicts (item_id, title, link, action, tag, reason, shown_tags)
    VALUES (${id}, ${title}, ${link}, ${action}, ${tag}, ${reason}, ${shown})`));
  return json({ ok: true, saved: rows.length });
}

export async function GET(request) {
  // Lets the page script check owner mode is still valid and list recent verdicts.
  if (!(await isOwner(request))) return json({ owner: false }, 403);
  const sql = neon(process.env.DATABASE_URL);
  const recent = await sql`SELECT item_id, action, tag, reason, created_at FROM verdicts ORDER BY id DESC LIMIT 50`;
  return json({ owner: true, recent });
}
