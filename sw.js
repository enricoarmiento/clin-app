// Clin si è spostato su https://clin-app.github.io/clin/app/
// Questo service worker sostituisce quello vecchio: svuota la cache e si rimuove,
// così i telefoni non restano bloccati sulla versione salvata offline.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const k of await caches.keys()) await caches.delete(k);
    await self.registration.unregister();
    for (const c of await self.clients.matchAll({ type: "window" })) c.navigate("https://clin-app.github.io/clin/app/");
  })());
});
