// Shared Redline scan engine — the same 14-trap knowledge base the frontend runs.
// Files under api/_lib are not routed by Vercel.

export const TRAPS = [
  { id: 'perp', label: 'Perpetuity', weight: 18,
    re: [/\bin perpetuity\b/i, /\bperpetual(ly)?\b/i, /\birrevocabl[ey]\b/i],
    why: 'Grants or obligations that never end.',
    fix: 'The rights granted under this Agreement last for the Term and end automatically on expiry or termination.' },
  { id: 'auto', label: 'Auto-renewal', weight: 14,
    re: [/automatic(ally)?\s+renew/i, /\bauto[- ]?renew/i, /renew(s|ed)?\s+for\s+(successive|additional)/i],
    why: 'The contract rolls over unless you catch a short cancellation window.',
    fix: 'This Agreement renews only by written agreement of both parties. Either party may decline renewal at any time before expiry.' },
  { id: 'indem', label: 'Unlimited indemnity', weight: 18,
    re: [/indemnif(y|ies|ication)[^.]{0,160}\b(any and all|all claims|without limit)/i, /hold\s+harmless[^.]{0,120}\b(any|all)\b/i],
    why: 'You pay for their losses with no ceiling, even ones they cause.',
    fix: 'Each party indemnifies the other only for third-party claims caused by its own breach or negligence, capped at the fees paid in the prior 12 months.' },
  { id: 'amend', label: 'One-sided changes', weight: 10,
    re: [/may\s+(modify|amend|change|update)[^.]{0,80}\b(at any time|sole discretion|without notice)/i],
    why: 'They can rewrite the deal after you sign.',
    fix: 'Any amendment must be in writing and signed by both parties.' },
  { id: 'noncomp', label: 'Broad non-compete', weight: 12,
    re: [/non[- ]?compet/i, /shall not[^.]{0,80}\b(compete|competing business|similar business)/i],
    why: 'Restricts where and for whom you can work.',
    fix: 'For 6 months after termination, neither party will solicit the other’s clients it worked with directly under this Agreement. No other restriction applies.' },
  { id: 'ip', label: 'IP grab', weight: 14,
    re: [/all\s+(inventions|intellectual property|work product)[^.]{0,120}\b(whether or not|prior|during or after|conceived)/i, /assigns?\s+(all|any)\s+(right|rights),?\s+title/i],
    why: 'Takes ownership of work beyond the project, sometimes your prior work.',
    fix: 'Client owns the deliverables specified in the Statement of Work once paid in full. Contractor keeps all pre-existing IP and general know-how.' },
  { id: 'ld', label: 'Penalty damages', weight: 10,
    re: [/liquidated\s+damages/i, /\bpenalt(y|ies)\s+of\b/i],
    why: 'Fixed payouts that can exceed any real loss.',
    fix: 'Each party is liable only for actual, proven direct damages.' },
  { id: 'term', label: 'Lopsided termination', weight: 8,
    re: [/(company|client|licensor|employer|we)\s+may\s+terminate[^.]{0,80}\b(at any time|for any reason|for convenience|without cause)/i],
    why: 'Only one side can walk away freely.',
    fix: 'Either party may terminate on 30 days’ written notice. Work completed up to termination is paid.' },
  { id: 'liab', label: 'Liability shield', weight: 10,
    re: [/in no event shall (company|client|licensor|provider|we)[^.]{0,40}be liable/i, /(company|provider|licensor)\s+shall\s+(have no|not be)\s+liab/i],
    why: 'Caps or removes their liability but not yours.',
    fix: 'Each party’s total liability is capped at the fees paid in the prior 12 months, except for fraud, gross negligence or wilful misconduct.' },
  { id: 'arb', label: 'Forced arbitration', weight: 8,
    re: [/binding\s+arbitration/i, /class[- ]action\s+waiver/i, /waive[^.]{0,40}\bjury\b/i],
    why: 'Removes your right to court and to join a class action.',
    fix: 'Disputes go first to good-faith negotiation for 30 days, then to the courts of the jurisdiction where the services are performed.' },
  { id: 'venue', label: 'Remote venue', weight: 5,
    re: [/exclusive\s+jurisdiction/i, /governed by the laws of[^.]{0,60}\bwithout regard/i],
    why: 'A dispute means litigating far from home under unfamiliar law.',
    fix: 'This Agreement is governed by the law of the place where the services are performed, and its courts have jurisdiction.' },
  { id: 'conf', label: 'Endless confidentiality', weight: 6,
    re: [/confidential[^.]{0,120}\b(indefinitely|in perpetuity|survive[s]? indefinitely|no expiration)/i],
    why: 'Secrecy obligations that never expire and bind only you.',
    fix: 'Confidentiality obligations are mutual and last 3 years after termination, or as long as the information is a trade secret.' },
  { id: 'fees', label: 'Punitive late fees', weight: 6,
    re: [/late\s+(fee|charge)/i, /interest\s+(at|of)\s+(\d{2,}|[2-9](\.\d+)?)\s*%/i],
    why: 'Fees or interest that compound fast.',
    fix: 'Late payments accrue simple interest at the statutory rate, starting 30 days after a written reminder.' },
  { id: 'assign', label: 'Free assignment', weight: 6,
    re: [/may\s+assign[^.]{0,60}\bwithout\s+(your|the other party’s|the other party's|prior)?\s*consent/i],
    why: 'You can end up contracted to a stranger.',
    fix: 'Neither party may assign this Agreement without the other’s written consent, not unreasonably withheld.' },
];

// Every contract carries a floor of residual risk; a clean draft scores 12.
const BASE = 12;

export function scan(text) {
  const src = String(text || '').replace(/\s+/g, ' ');
  const found = [];
  for (const t of TRAPS) {
    for (const re of t.re) {
      const m = src.match(re);
      if (m) {
        // Quote the sentence the match sits in, capped for SMS and email.
        const start = Math.max(src.lastIndexOf('. ', m.index) + 2, m.index - 160, 0);
        const stop = src.indexOf('. ', m.index + m[0].length);
        const quote = src.slice(start, stop < 0 ? undefined : Math.min(stop + 1, m.index + m[0].length + 160)).trim();
        found.push({ id: t.id, label: t.label, weight: t.weight, why: t.why, fix: t.fix, quote });
        break;
      }
    }
  }
  found.sort((a, b) => b.weight - a.weight);
  const risk = Math.min(99, BASE + found.reduce((s, t) => s + t.weight, 0));
  return { risk, traps: found, chars: src.length };
}

// Stateless share token: "87-perp.auto.indem". Decoded by index.html at /scan?id=…
export function token(result) {
  return `${result.risk}-${result.traps.map(t => t.id).join('.')}`;
}

export function scanUrl(result) {
  const base = (process.env.PUBLIC_URL || 'https://redline.app').replace(/\/$/, '');
  return `${base}/scan?id=${encodeURIComponent(token(result))}`;
}

const MAX_BYTES = 10 * 1024 * 1024;

// Turns a downloaded file into plain text. PDF and text only.
export async function extractText(buf, contentType = '', filename = '') {
  if (buf.length > MAX_BYTES) throw new Error('too_large');
  const type = contentType.toLowerCase();
  const isPdf = type.includes('pdf') || /\.pdf$/i.test(filename) || buf.subarray(0, 5).toString() === '%PDF-';
  if (isPdf) {
    // Import the lib file directly: pdf-parse's index runs a debug harness on load.
    const { default: pdf } = await import('pdf-parse/lib/pdf-parse.js');
    // Copy to a buffer of its own: the bundled pdf.js builds sub-streams from
    // bytes.buffer and ignores byteOffset, and small Node Buffers sit partway
    // into a shared pool.
    const out = await pdf(new Uint8Array(buf));
    return out.text || '';
  }
  if (type.startsWith('text/') || /\.(txt|md)$/i.test(filename)) return buf.toString('utf8');
  throw new Error('unsupported');
}
