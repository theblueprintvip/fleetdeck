// Stripe Checkout without dashboard setup: prices are created inline per session, and
// payments are confirmed by reading the session back from Stripe (no webhook needed).
import { db, increment, secrets } from '@appdeploy/sdk';

export const PRODUCTS = {
  single: { name: 'Redline full review', amount: 900, credits: 1, interval: null, label: 'Single review' },
  pack3: { name: 'Redline 3-Pack', amount: 1900, credits: 3, interval: null, label: '3-Pack' },
  pack10: { name: 'Redline 10-Pack', amount: 3900, credits: 10, interval: null, label: '10-Pack' },
  pro_month: { name: 'Redline Pro', amount: 1900, credits: 0, interval: 'month', label: 'Pro monthly' },
  pro_year: { name: 'Redline Pro', amount: 14400, credits: 0, interval: 'year', label: 'Pro yearly' },
} as const;

export type ProductKey = keyof typeof PRODUCTS;

export interface Entitlement {
  credits: number;
  sub_id?: string;
  customer?: string;
  pro_until?: number;
  checked_at?: number;
}

type Params = { [key: string]: string | number | boolean | null | undefined | Params | Params[] | string[] };

// Stripe's form encoding: nested objects become a[b][c]=v, arrays become a[0][b]=v.
export function formEncode(params: Params, prefix = ''): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    const key = prefix ? `${prefix}[${k}]` : k;
    if (Array.isArray(v)) {
      v.forEach((item, i) => {
        if (typeof item === 'object') parts.push(formEncode(item, `${key}[${i}]`));
        else parts.push(`${encodeURIComponent(`${key}[${i}]`)}=${encodeURIComponent(String(item))}`);
      });
    } else if (typeof v === 'object') {
      parts.push(formEncode(v, key));
    } else {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(v))}`);
    }
  }
  return parts.filter(Boolean).join('&');
}

export async function stripeKey(): Promise<string | null> {
  try {
    const names = await secrets.listSecretNames();
    if (!names.includes('STRIPE_SECRET_KEY')) return null;
    return (await secrets.readSecret('STRIPE_SECRET_KEY')).trim() || null;
  } catch {
    return null;
  }
}

async function stripe(key: string, method: 'GET' | 'POST', path: string, params?: Params): Promise<any> {
  const body = params ? formEncode(params) : '';
  const url = 'https://api.stripe.com/v1/' + path + (method === 'GET' && body ? '?' + body : '');
  const res = await fetch(url, {
    method,
    headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: method === 'POST' ? body : undefined,
  });
  const data: any = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error?.message || `Stripe error ${res.status}`);
  return data;
}

export async function getEntitlement(uh: string): Promise<Entitlement> {
  const [e] = await db.get<Entitlement>('entitlements', [uh], { consistent: true });
  return { credits: 0, ...(e ?? {}) };
}

const nowS = () => Math.floor(Date.now() / 1000);

function periodEnd(sub: any): number {
  return Number(sub?.current_period_end ?? sub?.items?.data?.[0]?.current_period_end ?? 0);
}

// Pro is active until the paid period ends; re-checked with Stripe at most every 6 hours.
export async function isPro(uh: string, ent: Entitlement, key: string | null): Promise<boolean> {
  if ((ent.pro_until ?? 0) > nowS() && nowS() - (ent.checked_at ?? 0) < 6 * 3600) return true;
  if (!ent.sub_id || !key) return (ent.pro_until ?? 0) > nowS();
  try {
    const sub = await stripe(key, 'GET', `subscriptions/${encodeURIComponent(ent.sub_id)}`);
    const active = ['active', 'trialing', 'past_due'].includes(sub.status);
    const until = active ? periodEnd(sub) : 0;
    await db.tx.update('entitlements', [{ id: uh, record: { pro_until: until, checked_at: nowS() } }], { patch: true });
    return until > nowS();
  } catch (err) {
    console.warn('subscription check failed', String(err));
    return (ent.pro_until ?? 0) > nowS();
  }
}

export async function createCheckout(key: string, uh: string, email: string | undefined, product: ProductKey, origin: string): Promise<string> {
  const p = PRODUCTS[product];
  const session = await stripe(key, 'POST', 'checkout/sessions', {
    mode: p.interval ? 'subscription' : 'payment',
    client_reference_id: uh,
    customer_email: email,
    allow_promotion_codes: true,
    success_url: `${origin}/?checkout=success&session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${origin}/?checkout=cancel#pricing`,
    metadata: { product, uh },
    line_items: [{
      quantity: 1,
      price_data: {
        currency: 'usd',
        unit_amount: p.amount,
        product_data: { name: p.name },
        recurring: p.interval ? { interval: p.interval } : undefined,
      },
    }],
  });
  return String(session.url);
}

// Applies a completed Checkout Session once. Safe to call again with the same session.
export async function confirmCheckout(key: string, uh: string, sessionId: string): Promise<{ applied: boolean; product: ProductKey; ent: Entitlement }> {
  if (!/^cs_[A-Za-z0-9_]+$/.test(sessionId)) throw new Error('Invalid checkout session');
  const s = await stripe(key, 'GET', `checkout/sessions/${sessionId}`);
  if (s.client_reference_id !== uh) throw new Error('This payment belongs to another account');
  const product = s.metadata?.product as ProductKey;
  if (!(product in PRODUCTS)) throw new Error('Unknown product');
  const paid = s.payment_status === 'paid' || (s.mode === 'subscription' && s.status === 'complete' && s.payment_status !== 'unpaid');
  if (!paid) throw new Error('Payment not completed yet');

  await db.tx.add('entitlements', [{ id: uh, credits: 0 }]);
  const [fresh] = await db.tx.add('payments', [{ id: sessionId, uh, product, at: new Date().toISOString() }]);
  if (fresh === null) return { applied: false, product, ent: await getEntitlement(uh) };

  const p = PRODUCTS[product];
  if (p.interval) {
    const sub = await stripe(key, 'GET', `subscriptions/${encodeURIComponent(String(s.subscription))}`);
    await db.tx.update('entitlements', [{ id: uh, record: { sub_id: String(sub.id), customer: String(sub.customer), pro_until: periodEnd(sub), checked_at: nowS() } }], { patch: true });
  } else {
    const record: Record<string, unknown> = { credits: increment(p.credits) };
    if (s.customer) record.customer = String(s.customer);
    await db.tx.update('entitlements', [{ id: uh, record }], { patch: true });
  }
  return { applied: true, product, ent: await getEntitlement(uh) };
}

// Spends one credit, guarded so two tabs cannot spend the same credit twice.
export async function spendCredit(uh: string): Promise<boolean> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const ent = await getEntitlement(uh);
    if (ent.credits < 1) return false;
    const [ok] = await db.tx.update('entitlements', [{ id: uh, record: { credits: ent.credits - 1 }, if: { credits: ent.credits } }], { patch: true });
    if (ok) return true;
  }
  return false;
}

export async function portalUrl(key: string, customer: string, origin: string): Promise<string> {
  const s = await stripe(key, 'POST', 'billing_portal/sessions', { customer, return_url: `${origin}/#pricing` });
  return String(s.url);
}

export function productList() {
  return (Object.keys(PRODUCTS) as ProductKey[]).map(k => ({ key: k, label: PRODUCTS[k].label, amount: PRODUCTS[k].amount, credits: PRODUCTS[k].credits, interval: PRODUCTS[k].interval }));
}
