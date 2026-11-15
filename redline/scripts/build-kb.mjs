// Writes mcp-server/kb.json from the server engine and the page's country list,
// so the self-contained MCP server scores contracts exactly like the web app.
import { readFile, writeFile } from 'node:fs/promises';
import { TRAPS, BASE } from '../api/_lib/scan.js';

const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
const list = html.match(/const COUNTRIES = \('([^']+)'\)/)[1];
const countries = list.split('|').map(s => {
  const [name, family] = s.split(':');
  return { name: JSON.parse(`"${name}"`), family };
});
const en = html.slice(html.indexOf('    en: {'), html.indexOf('    es: {'));
const family = Object.fromEntries(['K', 'C', 'M', 'I'].map(k => [k, JSON.parse(`"${en.match(new RegExp(`\\n      ${k}: '([^']+)'`))[1].replace(/"/g, '\\"')}"`)]));

const kb = {
  base: BASE,
  traps: TRAPS.map(t => ({ id: t.id, label: t.label, weight: t.weight, patterns: t.re.map(r => r.source), why: t.why, fix: t.fix })),
  countries,
  family_notes: family,
};
await writeFile(new URL('../mcp-server/kb.json', import.meta.url), JSON.stringify(kb, null, 2) + '\n');
console.log(`kb.json: ${kb.traps.length} traps, ${kb.countries.length} countries`);
