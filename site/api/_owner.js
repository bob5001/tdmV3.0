// Owner mode: one owner, no accounts. Visiting /api/garage?k=<OWNER_KEY> sets two cookies:
// tdm_owner (HttpOnly, a hash of the key) proves ownership to the API, and tdm_o=1 tells the
// page script to show the tag menus. OWNER_KEY lives only in Vercel's environment (the repo is public).
// Files starting with _ are not deployed as functions.

const enc = new TextEncoder();

export async function hash(s) {
  const d = await crypto.subtle.digest('SHA-256', enc.encode(`tdm-owner:${s}`));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function cookie(request, name) {
  const m = (request.headers.get('cookie') ?? '').match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return m ? m[1] : null;
}

function same(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function isOwner(request) {
  const key = process.env.OWNER_KEY;
  const got = cookie(request, 'tdm_owner');
  return Boolean(key && got && same(got, await hash(key)));
}

export async function keyMatches(k) {
  const key = process.env.OWNER_KEY;
  return Boolean(key && k && same(await hash(k), await hash(key)));
}
