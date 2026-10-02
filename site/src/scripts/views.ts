// Reader views: each reader shapes their own home page. Sections can be hidden, expanded to show more stories,
// reordered, and added from any category (MotoGP, Cafe Racers...). The Latest News ticker and Featured story can
// be hidden too. The view lives in this browser only (localStorage) as a small spec:
//   { v: 1, hidden: ['latest' | 'featured'], sections: [{ slug, hidden?, expanded?, added? }] }
// It is the same shape a client view will use once there are accounts. The inline script in index.astro applies a
// saved view before first paint; this module adds the controls, renders added sections, and saves changes.
import { relativeTimes } from './ago';

type Sec = { slug: string; hidden?: boolean; expanded?: boolean; added?: boolean };
type View = { v: 1; hidden: string[]; sections: Sec[] };
type Cat = { slug: string; label: string; parent: string | null };
type Story = { id: string; title: string; link: string; source: string; published: string; image: string | null; excerpt: string; tags: string[] };

const KEY = 'tdm.view.v1';
const PANELS: Record<string, string> = { latest: 'Latest news ticker', featured: 'Featured story' };

const box = document.querySelector<HTMLElement>('.sections')!;
const addable: Cat[] = JSON.parse(document.getElementById('addable')?.textContent ?? '[]');
const labelOf = new Map(addable.map((c) => [c.slug, c.label]));
const builtIn = [...box.querySelectorAll<HTMLElement>('[data-sec]')].map((el) => el.dataset.sec!);
const fetched = new Map<string, Promise<HTMLElement | null>>();

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]!);

function load(): View {
  let v: View | null = null;
  try { v = JSON.parse(localStorage.getItem(KEY) ?? 'null'); } catch {}
  const base: View = { v: 1, hidden: [], sections: builtIn.map((slug) => ({ slug })) };
  if (!v || !Array.isArray(v.sections)) return base;
  const known = new Set(v.sections.map((s) => s.slug));
  v.sections = v.sections.filter((s) => (s.added ? labelOf.has(s.slug) : builtIn.includes(s.slug)));
  builtIn.forEach((slug) => { if (!known.has(slug)) v!.sections.push({ slug }); });   // sections new to the site
  v.hidden = Array.isArray(v.hidden) ? v.hidden.filter((p) => p in PANELS) : [];
  return v;
}

let view = load();

function save() {
  const isDefault = !view.hidden.length && view.sections.length === builtIn.length &&
    view.sections.every((s, i) => s.slug === builtIn[i] && !s.hidden && !s.expanded && !s.added);
  try { isDefault ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, JSON.stringify(view)); } catch {}
}

// A section a reader added: same markup as the built-in ones, filled from /sec/<slug>.json.
function addedSection(slug: string): Promise<HTMLElement | null> {
  if (!fetched.has(slug)) {
    fetched.set(slug, fetch(`/sec/${slug}.json`).then((r) => (r.ok ? r.json() : null)).then((d) => {
      if (!d?.items?.length) return null;
      const items: Story[] = d.items;
      const lead = items.find((i) => i.image) ?? items[0];
      const others = items.filter((i) => i !== lead);
      const ext = 'target="_blank" rel="noopener noreferrer"';
      const meta = (i: Story) =>
        `<p class="meta" data-v="${i.id}" data-tags="${esc(i.tags.join(' '))}">${esc(i.source)} · <time datetime="${i.published}">${i.published.slice(0, 10)}</time></p>`;
      const line = (i: Story, more = false) =>
        `<div class="line${more ? ' x-more' : ''}"${more ? ' hidden' : ''}><h3><a href="${esc(i.link)}" ${ext}>${esc(i.title)}</a></h3>${meta(i)}</div>`;
      const el = document.createElement('section');
      el.className = 'sec';
      el.dataset.sec = slug;
      el.dataset.added = '';
      el.innerHTML =
        `<h2><a href="/c/${slug}/">${esc(d.label)}</a></h2>` +
        `<div class="lead">${lead.image ? `<a class="img" href="${esc(lead.link)}" ${ext} tabindex="-1" aria-hidden="true">` +
          `<img src="${esc(lead.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer"></a>` : ''}` +
        `<h3><a href="${esc(lead.link)}" ${ext}>${esc(lead.title)}</a></h3>${meta(lead)}` +
        `${lead.excerpt ? `<p class="ex">${esc(lead.excerpt)}…</p>` : ''}</div>` +
        others.slice(0, 3).map((i) => line(i)).join('') + others.slice(3, 9).map((i) => line(i, true)).join('') +
        `<p class="more"><a href="/c/${slug}/">See more “${esc(d.label)}” (${d.total}) »</a></p>`;
      relativeTimes(el);
      return el;
    }).catch(() => null));
  }
  return fetched.get(slug)!;
}

async function apply() {
  document.querySelectorAll<HTMLElement>('[data-panel]').forEach((el) => { el.hidden = view.hidden.includes(el.dataset.panel!); });
  for (const s of view.sections) {
    let el = box.querySelector<HTMLElement>(`:scope > [data-sec="${s.slug}"]`);
    const fresh = !el && s.added;
    if (fresh) {
      el = await addedSection(s.slug);
      if (el) controls(el);
    }
    if (!el) continue;
    box.append(el);
    if (fresh) el.dispatchEvent(new CustomEvent('tdm:content', { bubbles: true }));   // e.g. owner-mode tag buttons
    el.hidden = !!s.hidden;
    el.classList.toggle('x-open', !!s.expanded);
  }
  box.querySelectorAll<HTMLElement>(':scope > [data-added]').forEach((el) => {
    if (!view.sections.some((s) => s.slug === el.dataset.sec)) el.remove();
  });
  dedupe();
}

// A story shows once per page: going down the page, each section shows its lead plus its next stories that
// aren't already showing above it (3 lines, or 9 when expanded), drawing on its spare stories to fill gaps.
// Matters most for sections a reader adds (MotoGP overlaps Racing).
function dedupe() {
  const shown = new Set<string>();
  document.querySelectorAll<HTMLElement>('[data-panel="featured"]:not([hidden]) p.meta[data-v]').forEach((m) => shown.add(m.dataset.v!));
  box.querySelectorAll<HTMLElement>(':scope > .sec:not([hidden])').forEach((sec) => {
    const open = sec.classList.contains('x-open');
    const lead = sec.querySelector<HTMLElement>('.lead p.meta')?.dataset.v;
    if (lead) shown.add(lead);                     // the lead keeps its place even if repeated: it carries the picture
    let room = open ? 9 : 3;
    sec.querySelectorAll<HTMLElement>('.line').forEach((line) => {
      const id = line.querySelector<HTMLElement>('p.meta')?.dataset.v ?? '';
      line.hidden = room === 0 || shown.has(id);
      if (!line.hidden) { shown.add(id); room--; }
    });
    const btn = sec.querySelector<HTMLButtonElement>('.v-more');
    if (btn) { btn.textContent = open ? 'less' : 'more'; btn.setAttribute('aria-expanded', String(open)); }
  });
}

function update(slug: string, change: Partial<Sec>) {
  const s = view.sections.find((x) => x.slug === slug);
  if (s) Object.assign(s, change);
  save();
  apply();
  if (panelEl?.isConnected) renderPanel();
}

function controls(sec: HTMLElement) {
  const h2 = sec.querySelector('h2');
  if (!h2 || h2.querySelector('.v-ctl')) return;
  const slug = sec.dataset.sec!;
  const name = labelOf.get(slug) ?? slug;
  const span = document.createElement('span');
  span.className = 'v-ctl';
  span.innerHTML = `<button type="button" class="v-more" aria-expanded="false" title="Show more stories here">more</button>` +
    `<button type="button" class="v-hide" title="Hide this section" aria-label="Hide ${esc(name)}">×</button>`;
  span.querySelector('.v-more')!.addEventListener('click', () =>
    update(slug, { expanded: !view.sections.find((s) => s.slug === slug)?.expanded }));
  span.querySelector('.v-hide')!.addEventListener('click', () => {
    update(slug, { hidden: true });
    toast(`${name} hidden. Bring it back under Customize.`);
  });
  h2.append(span);
}

let toastTimer = 0;
function toast(msg: string) {
  let t = document.querySelector<HTMLElement>('.v-toast');
  if (!t) { t = document.createElement('div'); t.className = 'v-toast'; t.setAttribute('role', 'status'); document.body.append(t); }
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { t!.hidden = true; }, 3500);
}

// The Customize panel: everything in one list, phone-friendly (buttons, no drag and drop).
let panelEl: HTMLElement | null = null;

function renderPanel() {
  if (!panelEl) return;
  const rows = view.sections.map((s, i) => {
    const name = esc(labelOf.get(s.slug) ?? s.slug);
    return `<li data-slug="${s.slug}"${s.hidden ? ' class="off"' : ''}>` +
      `<label class="v-name"><input type="checkbox" data-act="show"${s.hidden ? '' : ' checked'}> ${name}</label>` +
      `<label class="v-opt"><input type="checkbox" data-act="expand"${s.expanded ? ' checked' : ''}> more</label>` +
      `<button type="button" data-act="up" aria-label="Move ${name} up"${i === 0 ? ' disabled' : ''}>↑</button>` +
      `<button type="button" data-act="down" aria-label="Move ${name} down"${i === view.sections.length - 1 ? ' disabled' : ''}>↓</button>` +
      (s.added ? `<button type="button" data-act="remove" aria-label="Remove ${name}">remove</button>` : '<span class="v-pad"></span>') +
      `</li>`;
  }).join('');
  const onPage = new Set(view.sections.map((s) => s.slug));
  const options = addable.filter((c) => !onPage.has(c.slug) && !c.parent).map((c) => {
    const kids = addable.filter((k) => k.parent === c.slug && !onPage.has(k.slug));
    return `<option value="${c.slug}">${esc(c.label)}</option>` + kids.map((k) => `<option value="${k.slug}">   ${esc(c.label)} › ${esc(k.label)}</option>`).join('');
  }).join('') + addable.filter((k) => k.parent && onPage.has(k.parent) && !onPage.has(k.slug))
    .map((k) => `<option value="${k.slug}">${esc(labelOf.get(k.parent!) ?? '')} › ${esc(k.label)}</option>`).join('');
  panelEl.innerHTML =
    `<div class="v-head"><strong>Your front page</strong><span>Saved in this browser.</span></div>` +
    `<div class="v-panels">${Object.entries(PANELS).map(([p, l]) =>
      `<label><input type="checkbox" data-panel-toggle="${p}"${view.hidden.includes(p) ? '' : ' checked'}> ${l}</label>`).join('')}</div>` +
    `<ol class="v-list">${rows}</ol>` +
    (options ? `<div class="v-add"><select aria-label="Add a section"><option value="">Add a section…</option>${options}</select>` +
      `<button type="button" data-act="add">Add</button></div>` : '') +
    `<div class="v-foot"><button type="button" data-act="reset">Reset to default</button><button type="button" data-act="done">Done</button></div>`;
}

function onPanel(e: Event) {
  const t = e.target as HTMLElement;
  const act = t.dataset.act;
  const panel = (t as HTMLInputElement).dataset.panelToggle;
  if (panel) {
    view.hidden = (t as HTMLInputElement).checked ? view.hidden.filter((p) => p !== panel) : [...view.hidden, panel];
    save(); apply(); return;
  }
  if (!act) return;
  const slug = t.closest('li')?.dataset.slug;
  const i = view.sections.findIndex((s) => s.slug === slug);
  if (act === 'show' && slug) update(slug, { hidden: !(t as HTMLInputElement).checked });
  else if (act === 'expand' && slug) update(slug, { expanded: (t as HTMLInputElement).checked });
  else if ((act === 'up' && i > 0) || (act === 'down' && i >= 0 && i < view.sections.length - 1)) {
    const j = act === 'up' ? i - 1 : i + 1;
    [view.sections[i], view.sections[j]] = [view.sections[j], view.sections[i]];
    save(); apply(); renderPanel();
    panelEl?.querySelector<HTMLButtonElement>(`li[data-slug="${slug}"] [data-act="${act}"]:not(:disabled)`)?.focus();
  } else if (act === 'remove' && slug) {
    view.sections = view.sections.filter((s) => s.slug !== slug);
    save(); apply(); renderPanel();
  } else if (act === 'add') {
    const sel = panelEl!.querySelector('select')!;
    if (!sel.value) return;
    view.sections.unshift({ slug: sel.value, added: true });     // new sections go to the top, where they're seen
    save(); apply(); renderPanel();
    toast(`${labelOf.get(sel.value)} added to the top of your page.`);
  } else if (act === 'reset') {
    view = { v: 1, hidden: [], sections: builtIn.map((s) => ({ slug: s })) };
    save(); apply(); renderPanel();
  } else if (act === 'done') {
    panelEl?.remove();
    panelEl = null;
    document.querySelector<HTMLButtonElement>('.v-open')?.focus();
  }
}

function openPanel() {
  if (panelEl) { panelEl.remove(); panelEl = null; return; }
  panelEl = document.createElement('div');
  panelEl.className = 'v-panel-home';
  panelEl.addEventListener('click', (e) => { if ((e.target as HTMLElement).matches('button')) onPanel(e); });
  panelEl.addEventListener('change', onPanel);
  document.querySelector('.v-bar')!.after(panelEl);
  renderPanel();
  panelEl.querySelector<HTMLElement>('input')?.focus();
}

export function startViews() {
  const bar = document.createElement('div');
  bar.className = 'v-bar';
  bar.innerHTML = `<button type="button" class="v-open">Customize this page</button>`;
  bar.querySelector('button')!.addEventListener('click', openPanel);
  document.querySelector('main')!.prepend(bar);
  box.querySelectorAll<HTMLElement>('[data-sec]').forEach(controls);
  apply();
}
