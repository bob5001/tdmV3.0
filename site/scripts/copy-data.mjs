// Fill src/data (the local, gitignored working copy the pages read) from, in order of preference:
//   1. the pipeline output next to this repo (../TDM-jev-ClaudeProposalCurrent/out): dev machines and the publisher
//   2. site/snapshot: the tracked snapshot the publisher maintains. This is what Vercel builds from.
// TDM_PUBLISH=1 (set by ops/publish.py only) also refreshes site/snapshot from the pipeline output.
// meta.json records when the pipeline last exported: the site shows "Updated 2h ago" from it, not from build time.
import { cpSync, mkdirSync, existsSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, '..', '..', 'TDM-jev-ClaudeProposalCurrent', 'out');
const snap = join(here, '..', 'snapshot');
const to = join(here, '..', 'src', 'data');
const FILES = ['items.json', 'categories.json', 'sources.json', 'forms.json'];
const DIRS = ['categories', 'forms'];

function copyTree(from, dest) {
  rmSync(dest, { recursive: true, force: true });
  mkdirSync(dest, { recursive: true });
  for (const f of FILES) cpSync(join(from, f), join(dest, f));
  for (const d of DIRS) cpSync(join(from, d), join(dest, d), { recursive: true });
}

let from;
if (existsSync(join(out, 'items.json'))) {
  from = out;
} else if (existsSync(join(snap, 'items.json'))) {
  from = snap;
  console.log('No pipeline output here; using the tracked snapshot in site/snapshot');
} else {
  console.error(`No pipeline output at ${out} and no snapshot. Run: python3 dm.py run  (in TDM-jev-ClaudeProposalCurrent)`);
  process.exit(1);
}
copyTree(from, to);
const publishing = process.env.TDM_PUBLISH === '1' && from === out;
if (from === out) {
  const meta = JSON.stringify({ updated: statSync(join(out, 'items.json')).mtime.toISOString() }) + '\n';
  writeFileSync(join(to, 'meta.json'), meta);
  if (publishing) {
    copyTree(out, snap);
    writeFileSync(join(snap, 'meta.json'), meta);
  }
} else {
  cpSync(join(snap, 'meta.json'), join(to, 'meta.json'));   // the snapshot carries its own export time
}
console.log(`data <- ${from === out ? 'pipeline output' : 'snapshot'}${publishing ? ' (snapshot refreshed)' : ''}`);
