// "3h ago" for timestamps within the last week; null for older ones (they keep their printed date).
export const ago = (iso: string) => {
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (!(m >= 0)) return null;
  if (m < 60) return m < 2 ? 'just now' : `${m}m ago`;
  if (m < 60 * 24) return `${Math.round(m / 60)}h ago`;
  if (m < 60 * 24 * 7) return `${Math.round(m / 1440)}d ago`;
  return null;
};

// Rewrite <time datetime> elements under root as relative times, keeping the full date in the tooltip.
export const relativeTimes = (root: ParentNode = document) => {
  root.querySelectorAll<HTMLTimeElement>('time[datetime]').forEach((t) => {
    const since = ago(t.dateTime);
    if (since) { t.title = t.textContent ?? ''; t.textContent = since; }
  });
};
