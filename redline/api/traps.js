// Trap knowledge base for the /scan?id= result view (labels and safe rewrites, no patterns).
import { TRAPS } from './_lib/scan.js';

export default function handler(req, res) {
  res.setHeader('Cache-Control', 'public, max-age=3600, s-maxage=86400');
  res.status(200).json(TRAPS.map(({ id, label, why, fix }) => ({ id, label, why, fix })));
}
