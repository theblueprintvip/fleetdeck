import { router, json, error, requireAuth, auth, ai, db, increment } from '@appdeploy/sdk';
import { createHash } from 'crypto';

import { realtimeSubscriptionRoutes } from './realtime-subscribers';
import { CLAUSES, CONTRACT_TYPES } from './kb';
import {
  MAX_CHARS,
  REVIEW_SCHEMA,
  REVIEW_SYSTEM,
  reviewPrompt,
  finalize,
  counterOfferPrompt,
} from './engine';
import type { RawReview, Review, Finding, KeyDate } from './engine';

const ANON_DAILY = 8;
const USER_DAILY = 40;
const BENCHMARK_MIN_CONTRACTS = 25;

const hash = (s: string) => createHash('sha256').update(s).digest('hex').slice(0, 24);
const today = () => new Date().toISOString().slice(0, 10);
const scansTable = (userId: string) => `scans_${hash(userId)}`;

function clientIp(event: any): string {
  const fwd = String(event?.headers?.['x-forwarded-for'] ?? '').split(',')[0].trim();
  return fwd || String(event?.requestContext?.http?.sourceIp ?? 'unknown');
}

// One counter per caller per day. Returns false once the daily limit is used up.
async function takeQuota(caller: string, limit: number): Promise<boolean> {
  const id = `${today()}_${hash(caller)}`;
  await db.tx.add('usage', [{ id, n: 0 }]);
  await db.tx.update('usage', [{ id, record: { n: increment(1) } }], { patch: true });
  const [rec] = await db.get<{ n: number }>('usage', [id], { consistent: true });
  return (rec?.n ?? 1) <= limit;
}

interface Profile {
  who?: string;
  city?: string;
  state?: string;
  share_stats?: boolean;
}

async function getProfile(userId: string): Promise<Profile> {
  const [p] = await db.get<Profile>('profiles', [hash(userId)]);
  return p ?? {};
}

// Anonymous clause statistics: contract type, clause id, risk level, state and month.
// No contract text, quotes, names or user ids are stored.
async function recordStats(review: Review, state: string): Promise<void> {
  const type = review.contract_type;
  const libIds = [...new Set(review.findings.map(f => f.library_id).filter((x): x is string => !!x))];
  const ids = [`t_${type}`, ...libIds.map(id => `c_${type}_${id}`)];
  await db.tx.add('clause_counts', ids.map(id => ({ id, n: 0 })));
  await db.tx.update('clause_counts', ids.map(id => ({ id, record: { n: increment(1) } })), { patch: true });
  const month = today().slice(0, 7);
  const events = review.findings.map(f => ({ type, library_id: f.library_id ?? '', risk: f.risk, state: state.slice(0, 40), month }));
  if (events.length) await db.add('clause_events', events.slice(0, 100));
}

// "Seen in 64% of creator deals" once there are enough contracts of that type.
async function benchmarks(review: Review): Promise<Record<string, number>> {
  const type = review.contract_type;
  const libIds = [...new Set(review.findings.map(f => f.library_id).filter((x): x is string => !!x))];
  if (!libIds.length) return {};
  const rows = await db.get<{ n: number }>('clause_counts', [`t_${type}`, ...libIds.map(id => `c_${type}_${id}`)]);
  const total = rows[0]?.n ?? 0;
  if (total < BENCHMARK_MIN_CONTRACTS) return {};
  const out: Record<string, number> = {};
  libIds.forEach((id, i) => {
    const n = rows[i + 1]?.n ?? 0;
    out[id] = Math.round((n / total) * 100);
  });
  return out;
}

async function runReview(text: string, opts: { side?: string; city?: string; state?: string; who?: string }): Promise<Review> {
  const clean = text.replace(/\u0000/g, '').trim();
  const truncated = clean.length > MAX_CHARS;
  const content = truncated ? clean.slice(0, MAX_CHARS) : clean;
  const result = await ai.extract({
    system: REVIEW_SYSTEM,
    prompt: reviewPrompt(opts),
    content,
    schema: REVIEW_SCHEMA,
    maxTokens: 8192,
    temperature: 0.1,
    maxRetries: 2,
  });
  return finalize((result.data ?? {}) as RawReview, content, truncated);
}

function aiFailure(err: unknown) {
  const e = err as { statusCode?: number; responseText?: string };
  console.error('ai failure', e?.statusCode, e?.responseText ?? String(err));
  if (e?.statusCode === 429) return error('Redline is busy right now. Try again in a minute.', 429);
  return error('Redline could not review this contract. Try again.', 502);
}

const str = (v: unknown, n = 200) => String(v ?? '').slice(0, n).trim();

export const handler = router({
  'GET /api/_healthcheck': [async () => json({ message: 'Success' })],

  // Full review. Works signed out (small daily limit); signed-in users get history and the wallet.
  'POST /api/analyze': [async (ctx) => {
    const body = (ctx.body ?? {}) as Record<string, unknown>;
    const text = String(body.text ?? '');
    if (text.trim().length < 80) return error('Paste or upload the full contract text.', 400);

    const user = await auth.getUser(ctx.event);
    const profile = user ? await getProfile(user.userId) : {};
    const allowed = await takeQuota(user ? `u:${user.userId}` : `ip:${clientIp(ctx.event)}`, user ? USER_DAILY : ANON_DAILY);
    if (!allowed) {
      return error(user
        ? 'You have reached today’s review limit. It resets tomorrow.'
        : 'Free reviews for today are used up. Sign in for more.', 429);
    }

    const opts = {
      side: str(body.side, 10),
      who: str(body.who ?? profile.who, 60),
      city: str(body.city ?? profile.city, 80),
      state: str(body.state ?? profile.state, 40),
    };

    let review: Review;
    try {
      review = await runReview(text, opts);
    } catch (err) {
      return aiFailure(err);
    }

    let bench: Record<string, number> = {};
    try {
      bench = await benchmarks(review);
      const share = body.share_stats !== false && profile.share_stats !== false;
      if (share) await recordStats(review, opts.state);
    } catch (err) {
      console.warn('stats skipped', String(err));
    }

    let scanId: string | null = null;
    if (user) {
      const [id] = await db.add(scansTable(user.userId), [{
        name: str(body.name, 160) || review.title,
        title: review.title,
        contract_type: review.contract_type,
        contract_label: review.contract_label,
        counterparty: review.counterparty,
        risk: review.risk,
        level: review.level,
        findings: review.findings,
        dates: review.dates,
        created_at: new Date().toISOString(),
      }]);
      scanId = id;
    }

    return json({ ...review, benchmarks: bench, scan_id: scanId, signed_in: !!user });
  }],

  'POST /api/counter-offer': [async (ctx) => {
    const body = (ctx.body ?? {}) as Record<string, unknown>;
    const findings = (Array.isArray(body.findings) ? body.findings : []).slice(0, 10).map((f: any) => ({
      clause: str(f?.clause, 120),
      quote: str(f?.quote, 300),
      rewrite: str(f?.rewrite, 900),
    })).filter(f => f.quote && f.rewrite);
    if (!findings.length) return error('Pick at least one clause to push back on.', 400);

    const user = await auth.getUser(ctx.event);
    const allowed = await takeQuota(user ? `u:${user.userId}` : `ip:${clientIp(ctx.event)}`, user ? USER_DAILY : ANON_DAILY);
    if (!allowed) return error(user ? 'Daily limit reached. It resets tomorrow.' : 'Sign in to draft more replies today.', 429);

    try {
      const out = await ai.generate({
        system: 'You write clear, polite negotiation emails for people pushing back on contract terms.',
        prompt: counterOfferPrompt({ title: str(body.title, 160), counterparty: str(body.counterparty, 160), tone: str(body.tone, 10), findings }),
        maxTokens: 900,
        temperature: 0.4,
      });
      return json({ email: out.text.trim() });
    } catch (err) {
      return aiFailure(err);
    }
  }],

  'GET /api/me': [requireAuth(), async (ctx) => {
    const profile = await getProfile(ctx.user!.userId);
    return json({ user: { email: ctx.user!.email ?? null, name: ctx.user!.name ?? null }, profile: { share_stats: true, ...profile } });
  }],

  'PUT /api/me': [requireAuth(), async (ctx) => {
    const body = (ctx.body ?? {}) as Record<string, unknown>;
    const profile: Profile = {
      who: str(body.who, 60),
      city: str(body.city, 80),
      state: str(body.state, 40),
      share_stats: body.share_stats !== false,
    };
    await db.tx.update('profiles', [{ id: hash(ctx.user!.userId), record: { ...profile } }]);
    return json({ profile });
  }],

  'GET /api/scans': [requireAuth(), async (ctx) => {
    const { items } = await db.list<Record<string, unknown>>(scansTable(ctx.user!.userId), { limit: 50 });
    items.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    return json({ scans: items });
  }],

  'DELETE /api/scans/:id': [requireAuth(), async (ctx) => {
    const [ok] = await db.delete(scansTable(ctx.user!.userId), [ctx.params.id]);
    return ok ? json({ deleted: true }) : error('Not found', 404);
  }],

  // Contract wallet: every deadline across the user's saved contracts, soonest first.
  'GET /api/wallet': [requireAuth(), async (ctx) => {
    const { items } = await db.list<{ name?: string; title?: string; dates?: KeyDate[]; findings?: Finding[] }>(scansTable(ctx.user!.userId), { limit: 50 });
    const now = today();
    const entries = items.flatMap(s => (s.dates ?? []).map(d => ({
      scan_id: s.id,
      contract: s.name || s.title || 'Contract',
      label: d.label,
      date: d.date,
      when: d.when,
      kind: d.kind,
      past: !!d.date && d.date < now,
    })));
    entries.sort((a, b) => (a.date ? 0 : 1) - (b.date ? 0 : 1) || String(a.date).localeCompare(String(b.date)));
    return json({ entries });
  }],

  'GET /api/library': [async () => json({
    types: CONTRACT_TYPES,
    clauses: CLAUSES,
  })],

  ...realtimeSubscriptionRoutes,
});
