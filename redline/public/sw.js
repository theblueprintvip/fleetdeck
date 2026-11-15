// Redline service worker: offline shell, Share sheet target, file launch.
const VERSION = 'redline-v27-2';
const SHELL = [
  '/',
  '/manifest.json',
  '/tila_final_sparkle_brown_eyes.png',
  '/redline_logo_transparent.png',
];
const SHARE_CACHE = 'redline-share';

self.addEventListener('install', e => {
  // Cache each file on its own so one missing asset doesn't block install.
  e.waitUntil(caches.open(VERSION)
    .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION && k !== SHARE_CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Share sheet: stash the shared file, then hand off to the page.
async function handleShare(request) {
  const data = await request.formData();
  const file = data.get('file');
  const cache = await caches.open(SHARE_CACHE);
  await cache.delete('/shared-file');
  if (file && typeof file !== 'string') {
    await cache.put('/shared-file', new Response(file, {
      headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name || 'contract.pdf') },
    }));
  } else {
    const text = [data.get('title'), data.get('text'), data.get('url')].filter(Boolean).join('\n');
    if (text) await cache.put('/shared-file', new Response(text, { headers: { 'Content-Type': 'text/plain', 'X-Filename': 'shared.txt' } }));
  }
  return Response.redirect('/?source=share', 303);
}

self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  if (request.method === 'POST' && url.pathname === '/share-target') {
    e.respondWith(handleShare(request));
    return;
  }
  if (request.method !== 'GET' || url.pathname.startsWith('/api/')) return;

  // Pages: network first so a deploy shows up, cached shell when offline.
  if (request.mode === 'navigate') {
    e.respondWith(
      fetch(request)
        .then(res => { const copy = res.clone(); caches.open(VERSION).then(c => c.put('/', copy)); return res; })
        .catch(() => caches.match('/'))
    );
    return;
  }

  // Static assets and fonts: cache first, fill on miss.
  const cacheable = url.origin === location.origin || /fonts\.(googleapis|gstatic)\.com$/.test(url.hostname);
  if (!cacheable) return;
  e.respondWith(
    caches.match(request).then(hit => hit || fetch(request).then(res => {
      if (res.ok || res.type === 'opaque') { const copy = res.clone(); caches.open(VERSION).then(c => c.put(request, copy)); }
      return res;
    }))
  );
});
