// /api/garage?k=<OWNER_KEY>  turns owner mode on for this browser; /api/garage?off  turns it off.
import { hash, keyMatches } from './_owner.js';

const YEAR = 60 * 60 * 24 * 365;

export async function GET(request) {
  const url = new URL(request.url);
  const headers = new Headers({ Location: '/', 'Cache-Control': 'no-store' });
  if (url.searchParams.has('off')) {
    headers.append('Set-Cookie', 'tdm_owner=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax');
    headers.append('Set-Cookie', 'tdm_o=; Path=/; Max-Age=0; Secure; SameSite=Lax');
    return new Response(null, { status: 303, headers });
  }
  const k = url.searchParams.get('k');
  if (!(await keyMatches(k))) return new Response('Not found', { status: 404 });
  headers.append('Set-Cookie', `tdm_owner=${await hash(k)}; Path=/; Max-Age=${YEAR}; HttpOnly; Secure; SameSite=Lax`);
  headers.append('Set-Cookie', `tdm_o=1; Path=/; Max-Age=${YEAR}; Secure; SameSite=Lax`);
  return new Response(null, { status: 303, headers });
}
