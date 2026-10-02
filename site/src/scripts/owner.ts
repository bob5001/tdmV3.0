// Owner mode only (loaded when the tdm_o cookie is set): a tag menu on every article, so the owner can
// add and remove categories and say why. Verdicts go to /api/verdict; the publisher applies them on its
// next publish, overruling Jev for that article, and keeps them as training data for the outlier loop.

type Node = { slug: string; label: string; children: { slug: string; label: string }[] };

const css = `
  .v-btn { margin-left: 0.5rem; padding: 0 0.45rem; font: inherit; font-size: 0.72rem; line-height: 1.5; color: var(--brand);
           background: none; border: 1px solid var(--brand); border-radius: 0.8rem; cursor: pointer; }
  .v-panel { margin: 0.4rem 0 0.6rem; padding: 0.7rem 0.8rem; border: 1px solid var(--line); border-radius: 0.4rem;
             background: #fafafa; font-size: 0.8rem; clear: both; }
  .v-grid { columns: 3 11rem; column-gap: 1rem; }
  .v-group { break-inside: avoid; margin-bottom: 0.35rem; }
  .v-group label { display: block; cursor: pointer; }
  .v-group .v-kid { padding-left: 1.1rem; color: #666; }
  .v-group input { margin: 0 0.3rem 0 0; vertical-align: -1px; }
  .v-panel .v-on { font-weight: 700; color: var(--ink); }
  .v-why { width: 100%; margin: 0.5rem 0; font: inherit; padding: 0.35rem 0.5rem; border: 1px solid #ccc; border-radius: 0.3rem; }
  .v-row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .v-row button { font: inherit; padding: 0.25rem 0.8rem; border-radius: 1rem; cursor: pointer; border: 1px solid var(--brand); }
  .v-save { background: var(--brand); color: #fff; }
  .v-ok, .v-cancel { background: #fff; color: var(--brand); }
  .v-status { color: var(--mute); }
`;

let tree: Node[] = [];
const label = new Map<string, string>();

function panel(meta: HTMLElement): HTMLFormElement {
  const shown = new Set((meta.dataset.tags ?? '').split(' ').filter((t) => label.has(t)));
  const form = document.createElement('form');
  form.className = 'v-panel';
  const box = (slug: string, text: string, kid = false) =>
    `<label class="${kid ? 'v-kid' : ''}"><input type="checkbox" value="${slug}"${shown.has(slug) ? ' checked' : ''}>` +
    `<span class="${shown.has(slug) ? 'v-on' : ''}">${text}</span></label>`;
  form.innerHTML =
    `<div class="v-grid">${tree.map((n) =>
      `<div class="v-group">${box(n.slug, n.label)}${n.children.map((k) => box(k.slug, k.label, true)).join('')}</div>`).join('')}</div>` +
    `<input class="v-why" name="why" placeholder="Why? (optional, but it's what teaches the system)" maxlength="2000">` +
    `<div class="v-row"><button class="v-save" type="submit">Save changes</button>` +
    `<button class="v-ok" type="button">Tags are right</button><button class="v-cancel" type="button">Cancel</button>` +
    `<span class="v-status" role="status"></span></div>`;

  const status = form.querySelector<HTMLElement>('.v-status')!;
  const send = async (confirmOnly: boolean) => {
    const checked = new Set([...form.querySelectorAll<HTMLInputElement>('input[type=checkbox]:checked')].map((i) => i.value));
    const add = confirmOnly ? [] : [...checked].filter((t) => !shown.has(t));
    const remove = confirmOnly ? [] : [...shown].filter((t) => !checked.has(t));
    if (!confirmOnly && !add.length && !remove.length) { status.textContent = 'Nothing changed. Use “Tags are right” to confirm.'; return; }
    const a = meta.parentElement?.querySelector<HTMLAnchorElement>('h3 a');
    status.textContent = 'Saving…';
    try {
      const r = await fetch('/api/verdict', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: meta.dataset.v, title: a?.textContent ?? null, link: a?.href ?? null,
          shown: [...shown], add, remove, reason: (form.elements.namedItem('why') as HTMLInputElement).value }),
      });
      if (r.status === 403) { status.textContent = 'Owner mode has expired: open your garage link again.'; return; }
      if (!r.ok) throw new Error(String(r.status));
      const next = confirmOnly ? shown : checked;
      meta.dataset.tags = [...next].join(' ');
      const btn = meta.querySelector<HTMLButtonElement>('.v-btn')!;
      btn.textContent = confirmOnly ? 'tags ✓' : 'tags ✎ saved';
      form.remove();
    } catch (e) {
      status.textContent = `Couldn't save (${(e as Error).message}). Try again.`;
    }
  };
  form.addEventListener('submit', (e) => { e.preventDefault(); send(false); });
  form.querySelector('.v-ok')!.addEventListener('click', () => send(true));
  form.querySelector('.v-cancel')!.addEventListener('click', () => form.remove());
  return form;
}

export async function startOwnerMode() {
  try {
    tree = await (await fetch('/tags.json')).json();
  } catch {
    return;
  }
  tree.forEach((n) => { label.set(n.slug, n.label); n.children.forEach((k) => label.set(k.slug, k.label)); });
  const style = document.createElement('style');
  style.textContent = css;
  document.head.append(style);

  attach(document);
  // Sections a reader adds to their home page arrive later (src/scripts/views.ts).
  document.addEventListener('tdm:content', (e) => attach(e.target as HTMLElement));
}

function attach(root: ParentNode) {
  root.querySelectorAll<HTMLElement>('p.meta[data-v]').forEach((meta) => {
    if (meta.querySelector('.v-btn')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'v-btn';
    btn.textContent = 'tags';
    btn.title = 'Fix this article’s categories';
    btn.addEventListener('click', () => {
      const open = meta.nextElementSibling;
      if (open?.classList.contains('v-panel')) { open.remove(); return; }
      meta.after(panel(meta));
    });
    meta.append(btn);
  });
}
