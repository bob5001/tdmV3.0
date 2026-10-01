// The subject taxonomy for the owner's tag menu: top-level categories with their children.
// Language pages and Odd Ball are pages, not subjects, so they're left out.
import categories from '../data/categories.json';

const NOT_SUBJECTS = new Set(['odd-ball', 'other-languages', 'french', 'swedish', 'german', 'spanish', 'italian', 'dutch']);

export function GET() {
  const subjects = categories.filter((c) => !NOT_SUBJECTS.has(c.slug));
  const tree = subjects
    .filter((c) => !c.parent)
    .map((c) => ({
      slug: c.slug,
      label: c.label,
      children: subjects.filter((k) => k.parent === c.slug).map((k) => ({ slug: k.slug, label: k.label })),
    }));
  return new Response(JSON.stringify(tree), { headers: { 'Content-Type': 'application/json' } });
}
