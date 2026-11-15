import { test } from 'node:test';
import assert from 'node:assert/strict';
import { scan, token, TRAPS, extractText } from '../api/_lib/scan.js';

const risky = `This license is perpetual and irrevocable. The Agreement shall automatically renew for successive
one-year terms. Contractor shall indemnify and hold harmless Company from any and all claims, losses and damages.
Company may modify these terms at any time without notice. Contractor assigns all right, title and interest.`;

test('knowledge base has 14 traps with unique ids', () => {
  assert.equal(TRAPS.length, 14);
  assert.equal(new Set(TRAPS.map(t => t.id)).size, 14);
});

test('risky contract scores high and names the traps', () => {
  const r = scan(risky);
  assert.ok(r.risk >= 70, `risk ${r.risk}`);
  for (const id of ['perp', 'auto', 'indem']) assert.ok(r.traps.some(t => t.id === id), id);
  assert.match(token(r), /^\d+-[a-z.]+$/);
});

test('clean contract scores low', () => {
  const r = scan('Either party may terminate on 30 days written notice. Each party owns its own pre-existing work. Payment is due within 30 days.');
  assert.ok(r.risk <= 12, `risk ${r.risk}`);
  assert.equal(r.traps.length, 0);
});

// Minimal one-page PDF with the given text.
function makePdf(text) {
  const stream = `BT /F1 9 Tf 20 700 Td (${text}) Tj ET`;
  const objs = [
    '<</Type/Catalog/Pages 2 0 R>>',
    '<</Type/Pages/Kids[3 0 R]/Count 1>>',
    '<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>',
    `<</Length ${stream.length}>>stream\n${stream}\nendstream`,
    '<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>',
  ];
  let out = '%PDF-1.4\n';
  const offs = objs.map((o, i) => { const at = out.length; out += `${i + 1} 0 obj\n${o}\nendobj\n`; return at; });
  const xref = out.length;
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n` + offs.map(o => `${String(o).padStart(10, '0')} 00000 n \n`).join('');
  out += `trailer<</Size ${objs.length + 1}/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF`;
  return Buffer.from(out, 'latin1');
}

test('extracts PDF text from a Buffer that does not start at offset 0 of its memory', async () => {
  // Node pools small Buffers, so a file often sits partway into a shared
  // ArrayBuffer. pdf.js 1.10 builds sub-streams from bytes.buffer and ignores
  // byteOffset, so it must be given a buffer of its own.
  const target = makePdf('This license is perpetual and irrevocable.');
  const pad = 4096;
  const backing = new ArrayBuffer(pad + target.length);
  new Uint8Array(backing).fill(0x78, 0, pad);
  new Uint8Array(backing).set(target, pad);
  const view = Buffer.from(backing, pad, target.length);
  const text = await extractText(view, 'application/pdf');
  assert.match(text, /perpetual and irrevocable/);
});
