// Service Worker: 静的シェルのみキャッシュする。
// API応答・認証情報・状態は絶対にキャッシュしない (指示書21章)。

const CACHE = "neuro-remote-v13";
const SHELL = [
  "/", "/index.html", "/app.js", "/styles.css",
  "/manifest.json", "/icon.svg", "/icon-192.png", "/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API・認証系は常にネットワーク直行 (キャッシュしない・させない)
  if (url.pathname.startsWith("/api/")) {
    return; // 既定のネットワーク処理に委ねる
  }

  // 静的シェルは network-first。オンライン(LAN内)では常に最新のHTML/JS/CSSを取得し、
  // 取得できた時だけキャッシュを更新。オフライン時のみキャッシュへフォールバック。
  // これで「新HTML＋古いapp.js」のようなキャッシュ不整合が起きない。
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
        }
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
