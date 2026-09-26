// Copy dm.py's exported JSON into src/data (a tracked snapshot: that is what Vercel builds from).
import { cpSync, mkdirSync, existsSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const from = join(here, '..', '..', 'TDM-jev-ClaudeProposalCurrent', 'out');
const to = join(here, '..', 'src', 'data');

if (!existsSync(join(from, 'items.json'))) {
  // Not on the machine that runs the pipeline (e.g. Vercel): build from the committed snapshot in src/data.
  if (existsSync(join(to, 'items.json'))) {
    console.log('No pipeline output here; building from the committed snapshot in src/data');
    process.exit(0);
  }
  console.error(`No dm.py output at ${from} and no committed snapshot. Run: python3 dm.py run  (in TDM-jev-ClaudeProposalCurrent)`);
  process.exit(1);
}
rmSync(to, { recursive: true, force: true });
mkdirSync(to, { recursive: true });
for (const f of ['items.json', 'categories.json', 'sources.json', 'forms.json']) cpSync(join(from, f), join(to, f));
cpSync(join(from, 'categories'), join(to, 'categories'), { recursive: true });
cpSync(join(from, 'forms'), join(to, 'forms'), { recursive: true });
// When the pipeline last exported: the site shows "Updated 2h ago" from this, not from build time.
writeFileSync(join(to, 'meta.json'), JSON.stringify({ updated: statSync(join(from, 'items.json')).mtime.toISOString() }) + '\n');
console.log(`copied data from ${from}`);
