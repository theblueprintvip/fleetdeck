// Twilio Messaging webhook. Text a PDF (MMS) or paste clause text to the Redline number.
import twilio from 'twilio';
import { scan, scanUrl, extractText } from './_lib/scan.js';

const xml = s => String(s).replace(/[<>&'"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&apos;', '"': '&quot;' }[c]));

function reply(res, message) {
  res.setHeader('Content-Type', 'text/xml');
  res.status(200).send(`<?xml version="1.0" encoding="UTF-8"?><Response><Message>${xml(message)}</Message></Response>`);
}

function publicUrl(req) {
  const proto = req.headers['x-forwarded-proto'] || 'https';
  const host = req.headers['x-forwarded-host'] || req.headers.host;
  return `${proto}://${host}${req.url}`;
}

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).setHeader('Allow', 'POST').end();

  const { TWILIO_ACCOUNT_SID: sid, TWILIO_AUTH_TOKEN: authToken } = process.env;
  if (!sid || !authToken) return res.status(500).end();

  const params = req.body || {};
  const signature = req.headers['x-twilio-signature'];
  if (!signature || !twilio.validateRequest(authToken, signature, publicUrl(req), params)) {
    return res.status(403).end();
  }

  try {
    let text = '';
    const media = params.MediaUrl0;
    if (media) {
      // Twilio media URLs require Basic auth when HTTP auth on media is enabled.
      const r = await fetch(media, {
        headers: { Authorization: 'Basic ' + Buffer.from(`${sid}:${authToken}`).toString('base64') },
      });
      if (!r.ok) throw new Error('download');
      const buf = Buffer.from(await r.arrayBuffer());
      text = await extractText(buf, params.MediaContentType0 || r.headers.get('content-type') || '');
    } else {
      text = params.Body || '';
    }

    if (text.trim().length < 40) {
      return reply(res, 'Redline: send a contract as a PDF, or paste the clause text, and we’ll scan it in seconds.');
    }

    const result = scan(text);
    const n = result.traps.length;
    const names = result.traps.slice(0, 3).map(t => t.label.toLowerCase()).join(', ');
    const summary = n
      ? `${n} trap${n > 1 ? 's' : ''} (${names}${n > 3 ? ', +' + (n - 3) : ''})`
      : 'no traps found';
    return reply(res, `Redline: Risk ${result.risk}/100 — ${summary}. View fix: ${scanUrl(result)}`);
  } catch (err) {
    const msg = err.message === 'unsupported'
      ? 'Redline reads PDFs and plain text. Please send the contract as a PDF.'
      : err.message === 'too_large'
        ? 'Redline: that file is over 10 MB. Try a smaller PDF.'
        : 'Redline couldn’t read that file. Please try again.';
    return reply(res, msg);
  }
}
