// Claude Code Monitor (接続先設定シェル): インストール可否判定と通知のための最小Service Worker。
// オフラインキャッシュは行わない(常に接続先サーバーへ直接fetchする)。
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
// 作業完了などの通知をタップしたら、開いているアプリを前面に出す(無ければ開く)
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) { if ('focus' in c) return c.focus(); }
    return self.clients.openWindow('./');
  })());
});
