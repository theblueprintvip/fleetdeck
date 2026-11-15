// SendGrid Inbound Parse webhook. Forward a contract to the Redline inbox and get
// the Risk score plus safe rewrites back. Point Inbound Parse at
// https://<host>/api/email?key=<INBOUND_PARSE_KEY> with "POST the raw, full MIME" OFF.
import Busboy from 'busboy';
import sgMail from '@sendgrid/mail';
import { scan, scanUrl, extractText } from './_lib/scan.js';

export const config = { api: { bodyParser: false } };

const MAX_FILE = 10 * 1024 * 1024;

function parseMultipart(req) {
  return new Promise((resolve, reject) => {
    const fields = {};
    const files = [];
    const bb = Busboy({ headers: req.headers, limits: { fileSize: MAX_FILE, files: 10 } });
    bb.on('field', (name, val) => { fields[name] = val; });
    bb.on('file', (name, stream, info) => {
      const chunks = [];
      stream.on('data', c => chunks.push(c));
      stream.on('limit', () => { stream.resume(); });
      stream.on('end', () => {
        if (!stream.truncated) files.push({ field: name, filename: info.filename, mimeType: info.mimeType, buf: Buffer.concat(chunks) });
      });
    });
    bb.on('error', reject);
    bb.on('close', () => resolve({ fields, files }));
    req.pipe(bb);
  });
}

const esc = s => String(s).replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));

function senderAddress(from = '') {
  const m = from.match(/<([^>]+)>/);
  const addr = (m ? m[1] : from).trim();
  return /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(addr) ? addr : null;
}

function messageId(headers = '') {
  const m = headers.match(/^Message-ID:\s*(<[^>]+>)/im);
  return m ? m[1] : null;
}

function authenticated(fields) {
  const spf = (fields.SPF || '').toLowerCase();
  let dkim = {};
  try { dkim = JSON.parse(fields.dkim || '{}'); } catch { /* SendGrid sends "{@domain : pass}" */ }
  const dkimPass = /pass/i.test(fields.dkim || '') || Object.values(dkim).includes('pass');
  return spf === 'pass' || dkimPass;
}

function renderHtml(result, url, filename) {
  const tone = result.risk >= 60 ? '#D6402A' : result.risk >= 30 ? '#C9A86A' : '#14171B';
  const rows = result.traps.map(t => `
    <tr><td style="padding:20px 0;border-top:1px solid #E4E0D6">
      <div style="font:600 15px Inter,Helvetica,Arial,sans-serif;color:#14171B">${esc(t.label)}</div>
      <div style="font:400 14px/1.5 Inter,Helvetica,Arial,sans-serif;color:#5A5D61;margin-top:4px">${esc(t.why)}</div>
      <div style="font:400 12px/1.5 'IBM Plex Mono',Menlo,monospace;color:#D6402A;margin-top:10px;text-decoration:line-through">“…${esc(t.quote)}…”</div>
      <div style="font:400 14px/1.6 Inter,Helvetica,Arial,sans-serif;color:#14171B;margin-top:10px;padding:12px 14px;background:#FFFFFF;border-left:3px solid #C9A86A;border-radius:8px">${esc(t.fix)}</div>
    </td></tr>`).join('');
  return `<!doctype html><html><body style="margin:0;background:#F3F1EC">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F1EC"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">
  <tr><td style="font:600 22px Fraunces,Georgia,serif;color:#14171B">Redline</td></tr>
  <tr><td style="padding-top:24px;font:400 13px 'IBM Plex Mono',Menlo,monospace;color:#5A5D61">${esc(filename || 'Your contract')}</td></tr>
  <tr><td style="padding-top:8px;font:600 56px/1 Fraunces,Georgia,serif;color:${tone}">${result.risk}<span style="font-size:20px;color:#5A5D61">/100</span></td></tr>
  <tr><td style="padding-top:12px"><div style="height:12px;background:#E4E0D6;border-radius:6px"><div style="height:12px;width:${result.risk}%;background:${tone};border-radius:6px"></div></div></td></tr>
  <tr><td style="padding:16px 0 8px;font:400 15px/1.5 Inter,Helvetica,Arial,sans-serif;color:#14171B">${result.traps.length ? `${result.traps.length} trap${result.traps.length > 1 ? 's' : ''} found. Safe rewrites below.` : 'No known traps found.'}</td></tr>
  <tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">${rows}</table></td></tr>
  <tr><td style="padding-top:28px"><a href="${esc(url)}" style="display:inline-block;background:#14171B;color:#F3F1EC;font:600 15px Inter,Helvetica,Arial,sans-serif;text-decoration:none;padding:14px 24px;border-radius:999px">Open full scan</a></td></tr>
  <tr><td style="padding-top:28px;font:400 12px/1.5 Inter,Helvetica,Arial,sans-serif;color:#8A8C8F">Redline flags common contract risks. It is not legal advice.</td></tr>
</table></td></tr></table></body></html>`;
}

function renderText(result, url) {
  const lines = [`Redline: Risk ${result.risk}/100`, ''];
  for (const t of result.traps) lines.push(`${t.label}: ${t.why}`, `Safe rewrite: ${t.fix}`, '');
  if (!result.traps.length) lines.push('No known traps found.', '');
  lines.push(`Full scan: ${url}`, '', 'Redline flags common contract risks. It is not legal advice.');
  return lines.join('\n');
}

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).setHeader('Allow', 'POST').end();

  const { SENDGRID_API_KEY, SENDGRID_FROM, INBOUND_PARSE_KEY } = process.env;
  if (!SENDGRID_API_KEY || !SENDGRID_FROM || !INBOUND_PARSE_KEY) return res.status(500).end();
  if (new URL(req.url, 'http://x').searchParams.get('key') !== INBOUND_PARSE_KEY) return res.status(403).end();

  let fields, files;
  try {
    ({ fields, files } = await parseMultipart(req));
  } catch {
    return res.status(400).end();
  }

  // Always 200 from here: SendGrid retries non-2xx for up to 3 days.
  const to = senderAddress(fields.from);
  if (!to || !authenticated(fields)) return res.status(200).end();

  const doc = files.find(f => /pdf/i.test(f.mimeType) || /\.pdf$/i.test(f.filename))
    || files.find(f => /^text\//i.test(f.mimeType));

  let text = '';
  try {
    text = doc ? await extractText(doc.buf, doc.mimeType, doc.filename) : (fields.text || '');
  } catch { /* falls through to the "couldn't read" reply */ }

  sgMail.setApiKey(SENDGRID_API_KEY);
  const subject = (fields.subject || 'your contract').replace(/^\s*(re|fwd?):\s*/i, '');
  const inReplyTo = messageId(fields.headers);
  const threadHeaders = inReplyTo ? { 'In-Reply-To': inReplyTo, References: inReplyTo } : undefined;

  try {
    if (text.trim().length < 40) {
      await sgMail.send({
        to, from: SENDGRID_FROM, subject: `Re: ${subject}`, headers: threadHeaders,
        text: 'Redline couldn’t find contract text. Attach the contract as a PDF, or paste the clauses into the email body.',
      });
    } else {
      const result = scan(text);
      const url = scanUrl(result);
      await sgMail.send({
        to, from: SENDGRID_FROM, headers: threadHeaders,
        subject: `Re: ${subject} — Risk ${result.risk}/100`,
        text: renderText(result, url),
        html: renderHtml(result, url, doc && doc.filename),
      });
    }
  } catch (err) {
    console.error('sendgrid', err.code || err.message);
  }
  return res.status(200).end();
}
