/*
 * Offline shell (MASTER_PLAN P2B-2).
 *
 * Only ever registered in a secure context (see pwa.py), so on plain HTTP over
 * the LAN this file is simply never fetched and the app behaves exactly as it
 * did before.
 *
 * The caching policy is deliberately asymmetric, because the two kinds of
 * request here fail in opposite directions:
 *
 *   Static assets   cache-first.  Versioned by app_version, so a deploy
 *                                 replaces them wholesale rather than serving
 *                                 last week's CSS against this week's markup.
 *
 *   Pages and API   network-first. A receipt printer's queue depth and a
 *                                 task's due time are worth nothing stale. The
 *                                 cache is a fallback for when the server is
 *                                 unreachable, not a performance layer.
 *
 * Nothing that changes state is cached or replayed. A POST that appeared to
 * succeed offline and then printed a duplicate receipt hours later would be
 * worse than an honest failure.
 */
const VERSION = '{{ app_version }}';
const CACHE = 'taskhome-' + VERSION;

const SHELL = [
    '{{ url_for("main.index") }}',
    '{{ url_for("static", filename="mica.css") }}',
    '{{ url_for("static", filename="ui.js") }}',
    '{{ url_for("static", filename="vendor/mica-tokens.css") }}',
    '{{ url_for("static", filename="vendor/inter.css") }}',
    '{{ url_for("static", filename="vendor/material-icons.css") }}',
    '{{ url_for("static", filename="vendor/fonts/inter-variable.woff2") }}',
    '{{ url_for("static", filename="icons/icon-192.png") }}',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        // addAll is all-or-nothing: one 404 would leave no cache at all, so
        // each asset is added independently and a missing one is survivable.
        caches.open(CACHE).then((cache) => Promise.all(
            SHELL.map((url) => cache.add(url).catch(() => null))
        )).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))
            ))
            // Take over open tabs immediately rather than waiting for every one
            // to close, so a fixed bug does not linger on a phone left open on
            // the counter for a week.
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;

    // Only GET, and only this origin. Anything else goes straight to the
    // network untouched.
    if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) {
        return;
    }

    const isStatic = request.url.includes('/static/');

    if (isStatic) {
        event.respondWith(
            caches.match(request).then((hit) => hit || fetch(request).then((response) => {
                if (response.ok) {
                    const copy = response.clone();
                    caches.open(CACHE).then((cache) => cache.put(request, copy));
                }
                return response;
            }))
        );
        return;
    }

    event.respondWith(
        fetch(request)
            .then((response) => {
                if (response.ok) {
                    const copy = response.clone();
                    caches.open(CACHE).then((cache) => cache.put(request, copy));
                }
                return response;
            })
            .catch(() => caches.match(request).then(
                (hit) => hit || caches.match('{{ url_for("main.index") }}')
            ))
    );
});
