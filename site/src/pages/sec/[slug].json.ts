// /sec/<slug>.json: the top stories of one category, for sections a reader adds to their home page
// (src/scripts/views.ts). Built at build time like everything else; fetched only when a reader asks for one.
const pages = import.meta.glob('../../data/categories/*.json', { eager: true });
const NOT_SUBJECTS = new Set(['odd-ball', 'other-languages', 'french', 'swedish', 'german', 'spanish', 'italian', 'dutch']);

export function getStaticPaths() {
  return Object.values(pages)
    .map((m: any) => m.default)
    .filter((p) => p.items.length > 0 && !NOT_SUBJECTS.has(p.slug))
    .map((p) => ({ params: { slug: p.slug }, props: { page: p } }));
}

export function GET({ props }) {
  const { page } = props;
  const pick = ({ id, title, link, source, published, image, excerpt, tags }) =>
    ({ id, title, link, source, published, image, excerpt: excerpt ? excerpt.slice(0, 140) : '', tags });
  return new Response(JSON.stringify({
    slug: page.slug, label: page.label, total: page.items.length, items: page.items.slice(0, 10).map(pick),
  }), { headers: { 'Content-Type': 'application/json' } });
}
