// Redline engine: asks the model for a grounded review, then checks it. Scores are
// computed here from the clause library, never taken from the model.
import { CLAUSES, CLAUSE_BY_ID, CONTRACT_TYPES, RISK_WEIGHT, BASE_RISK } from './kb';
import type { ContractType, Risk } from './kb';

export const MAX_CHARS = 60000;

export interface RawFinding {
  library_id?: string;
  quote?: string;
  risk?: string;
  explanation?: string;
  rewrite?: string;
}

export interface RawDate {
  label?: string;
  date?: string;
  when?: string;
  kind?: string;
}

export interface RawReview {
  contract_type?: string;
  title?: string;
  counterparty?: string;
  findings?: RawFinding[];
  dates?: RawDate[];
}

export interface Finding {
  library_id: string | null;
  clause: string;
  risk: Risk;
  confidence: 'library' | 'review_recommended';
  source: string;
  quote: string;
  quote_verified: boolean;
  explanation: string;
  rewrite: string;
  weight: number;
}

export interface KeyDate {
  label: string;
  date: string | null;
  when: string;
  kind: string;
}

export interface Review {
  contract_type: ContractType;
  contract_label: string;
  title: string;
  counterparty: string;
  risk: number;
  level: 'low' | 'medium' | 'high';
  findings: Finding[];
  dates: KeyDate[];
  truncated: boolean;
}

const TYPES = Object.keys(CONTRACT_TYPES) as ContractType[];
const RISKS: Risk[] = ['high', 'medium', 'low'];
const DATE_KINDS = ['renewal', 'notice_deadline', 'payment', 'exclusivity_end', 'term_end', 'other'];

export const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    contract_type: { type: 'string', enum: TYPES },
    title: { type: 'string' },
    counterparty: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          library_id: { type: 'string' },
          quote: { type: 'string' },
          risk: { type: 'string', enum: RISKS },
          explanation: { type: 'string' },
          rewrite: { type: 'string' },
        },
        required: ['library_id', 'quote', 'risk', 'explanation', 'rewrite'],
      },
    },
    dates: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          label: { type: 'string' },
          date: { type: 'string' },
          when: { type: 'string' },
          kind: { type: 'string', enum: DATE_KINDS },
        },
        required: ['label', 'date', 'when', 'kind'],
      },
    },
  },
  required: ['contract_type', 'title', 'counterparty', 'findings', 'dates'],
};

const LIBRARY_TEXT = CLAUSES.map(c => `${c.id} | ${c.library} | ${c.name} | ${c.risk} | ${c.exploit}`).join('\n');

export const REVIEW_SYSTEM = [
  'You are Redline, a contract-risk reviewer for people without a lawyer on retainer. You explain risk like a sharp friend: direct, a little cheeky, never condescending, never alarmist. You are not a lawyer and never give legal advice.',
  'Work only from the contract text you are given. Never invent clauses, numbers, dates or laws.',
].join(' ');

export function reviewPrompt(opts: { side?: string; city?: string; state?: string; who?: string }): string {
  const side = opts.side === 'buyer'
    ? 'the paying customer'
    : opts.side === 'seller'
      ? 'the provider being paid'
      : 'the individual signer (the weaker party, usually the athlete, creator, performer, employee, tenant, borrower or user)';
  return [
    `Review the contract for risks to ${side}.`,
    opts.who ? `The signer describes themselves as: ${opts.who}.` : '',
    opts.state ? `They are signing from ${[opts.city, opts.state].filter(Boolean).join(', ')}.` : '',
    '',
    `1. contract_type: one of ${TYPES.join(', ')}.`,
    '2. title: a short name for the contract, e.g. "NIL endorsement agreement". counterparty: the other party\'s name, or "".',
    '3. findings: every clause that is risky for the signer. For each:',
    '   - library_id: the id of the best-matching entry in the CLAUSE LIBRARY below. Use "" only if no entry fits.',
    '   - quote: the risky sentence copied exactly from the contract, at most 300 characters.',
    '   - risk: high, medium or low.',
    '   - explanation: one or two plain-English sentences on what could go wrong for the signer.',
    '   - rewrite: replacement clause text the signer could propose instead. Concrete and fair, ready to paste.',
    '   Order findings from most to least risky. Do not flag standard, balanced clauses.',
    '4. dates: every date, deadline or time window in the contract (renewals, notice deadlines, payment due dates, exclusivity end, term end).',
    '   label: what it is. date: YYYY-MM-DD if the contract states an exact date, else "". when: the deadline as written, e.g. "30 days before renewal". kind: one of ' + DATE_KINDS.join(', ') + '.',
    '',
    'CLAUSE LIBRARY (id | library | clause | risk | common exploit):',
    LIBRARY_TEXT,
  ].filter(Boolean).join('\n');
}

const norm = (s: string) => s.replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/\s+/g, ' ').trim().toLowerCase();

function clip(s: unknown, n: number): string {
  const t = String(s ?? '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

export function finalize(raw: RawReview, text: string, truncated: boolean): Review {
  const type: ContractType = TYPES.includes(raw.contract_type as ContractType) ? raw.contract_type as ContractType : 'other';
  const haystack = norm(text);
  const seen = new Set<string>();
  const findings: Finding[] = [];

  for (const f of raw.findings ?? []) {
    const quote = clip(f.quote, 300);
    if (!quote) continue;
    const entry = f.library_id ? CLAUSE_BY_ID.get(f.library_id) : undefined;
    // Integrity: a library match sets the risk level; otherwise it is a general review.
    const risk: Risk = entry ? entry.risk : (RISKS.includes(f.risk as Risk) ? f.risk as Risk : 'medium');
    const key = entry ? entry.id : norm(quote).slice(0, 80);
    if (seen.has(key)) continue;
    seen.add(key);
    const verified = haystack.includes(norm(quote).replace(/…$/, ''));
    const weight = entry && verified ? RISK_WEIGHT[risk] : Math.round(RISK_WEIGHT[risk] / 2);
    findings.push({
      library_id: entry ? entry.id : null,
      clause: entry ? entry.name : 'Other risk',
      risk,
      confidence: entry && verified ? 'library' : 'review_recommended',
      source: entry ? `Redline clause library: ${entry.name}` : 'General AI review',
      quote,
      quote_verified: verified,
      explanation: clip(f.explanation, 400),
      rewrite: clip(f.rewrite, 900),
      weight,
    });
  }

  const order: Record<Risk, number> = { high: 0, medium: 1, low: 2 };
  findings.sort((a, b) => order[a.risk] - order[b.risk] || b.weight - a.weight);
  const risk = Math.min(99, BASE_RISK + findings.reduce((s, f) => s + f.weight, 0));

  const dates: KeyDate[] = (raw.dates ?? [])
    .map(d => ({
      label: clip(d.label, 120),
      date: /^\d{4}-\d{2}-\d{2}$/.test(String(d.date ?? '')) ? String(d.date) : null,
      when: clip(d.when, 160),
      kind: DATE_KINDS.includes(String(d.kind)) ? String(d.kind) : 'other',
    }))
    .filter(d => d.label && (d.date || d.when))
    .slice(0, 30);

  return {
    contract_type: type,
    contract_label: CONTRACT_TYPES[type].label,
    title: clip(raw.title, 120) || CONTRACT_TYPES[type].label,
    counterparty: clip(raw.counterparty, 120),
    risk,
    level: risk >= 60 ? 'high' : risk >= 30 ? 'medium' : 'low',
    findings: findings.slice(0, 25),
    dates,
    truncated,
  };
}

export function counterOfferPrompt(opts: {
  title: string;
  counterparty: string;
  tone: string;
  findings: Array<{ clause: string; quote: string; rewrite: string }>;
}): string {
  const tone = opts.tone === 'firm' ? 'firm and confident' : opts.tone === 'soft' ? 'warm and collaborative' : 'friendly but clear';
  return [
    `Write a short email the signer can send to ${opts.counterparty || 'the other party'} about the ${opts.title || 'contract'}.`,
    `Tone: ${tone}. Thank them, say they are keen to move forward, then propose these changes as a numbered list.`,
    'For each change, quote the original wording briefly and give the proposed replacement. Keep it under 250 words.',
    'Do not mention Redline, AI, or legal advice. Start with "Subject:" on the first line. Use [Your name] as the signature.',
    '',
    ...opts.findings.map((f, i) => `${i + 1}. ${f.clause}\nOriginal: ${f.quote}\nProposed: ${f.rewrite}`),
  ].join('\n');
}
