// Copy dm.py's exported JSON into src/data so the build reads a local, gitignored copy.
import { cpSync, mkdirSync, existsSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const from = join(here, '..', '..', 'TDM-jev-ClaudeProposalCurrent', 'out');
const to = join(here, '..', 'src', 'data');

if (!existsSync(join(from, 'items.json'))) {
  console.error(`No dm.py output at ${from}. Run: python3 dm.py run  (in TDM-jev-ClaudeProposalCurrent)`);
  process.exit(1);
}
rmSync(to, { recursive: true, force: true });
mkdirSync(to, { recursive: true });
for (const f of ['items.json', 'categories.json', 'sources.json']) cpSync(join(from, f), join(to, f));
cpSync(join(from, 'categories'), join(to, 'categories'), { recursive: true });
console.log(`copied data from ${from}`);
