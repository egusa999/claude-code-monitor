// Claude Code Monitor (接続先設定シェル): インストール可否判定のためだけの最小Service Worker。
// オフラインキャッシュは行わない(常に接続先サーバーへ直接fetchする)。
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
