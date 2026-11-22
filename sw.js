// Clin - service worker: funzionamento offline e ricezione dal menu Condividi (Android).
// edd017e8dc1c e ["./", "app.js", "apple-touch-icon.png", "fonts/AlbertSans.woff2", "fonts/OFL-AlbertSans.txt", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "index.html", "logo.svg", "manifest.webmanifest", "py/anonymize_engine.py", "py/clin_llm.py", "py/clin_ocr.py", "py/clin_text.py", "py/clin_web.py", "py/tabular_engine.py", "style.css", "vendor/pdfjs/pdf.min.mjs", "vendor/pdfjs/pdf.worker.min.mjs", "vendor/pyodide/pyodide-lock.json", "vendor/pyodide/pyodide.asm.js", "vendor/pyodide/pyodide.asm.wasm", "vendor/pyodide/pyodide.mjs", "vendor/pyodide/python_stdlib.zip", "vendor/tess/ita.traineddata.gz", "vendor/tess/tesseract-core-lstm.wasm.js", "vendor/tess/tesseract-core-simd-lstm.wasm.js", "vendor/tess/tesseract.min.js", "vendor/tess/worker.min.js", "vendor/wheels/et_xmlfile-2.0.0-py3-none-any.whl", "vendor/wheels/openpyxl-3.1.5-py2.py3-none-any.whl"] vengono compilati da build.py.
const CACHE = "clin-edd017e8dc1c";
const FILES = ["./", "app.js", "apple-touch-icon.png", "fonts/AlbertSans.woff2", "fonts/OFL-AlbertSans.txt", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "index.html", "logo.svg", "manifest.webmanifest", "py/anonymize_engine.py", "py/clin_llm.py", "py/clin_ocr.py", "py/clin_text.py", "py/clin_web.py", "py/tabular_engine.py", "style.css", "vendor/pdfjs/pdf.min.mjs", "vendor/pdfjs/pdf.worker.min.mjs", "vendor/pyodide/pyodide-lock.json", "vendor/pyodide/pyodide.asm.js", "vendor/pyodide/pyodide.asm.wasm", "vendor/pyodide/pyodide.mjs", "vendor/pyodide/python_stdlib.zip", "vendor/tess/ita.traineddata.gz", "vendor/tess/tesseract-core-lstm.wasm.js", "vendor/tess/tesseract-core-simd-lstm.wasm.js", "vendor/tess/tesseract.min.js", "vendor/tess/worker.min.js", "vendor/wheels/et_xmlfile-2.0.0-py3-none-any.whl", "vendor/wheels/openpyxl-3.1.5-py2.py3-none-any.whl"];

self.addEventListener("install", (event) => {
  // cache: "reload" scavalca la cache HTTP: una nuova versione non riceve file vecchi
  event.waitUntil(caches.open(CACHE)
    .then((c) => c.addAll(FILES.map((f) => new Request(f, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("clin-") && k !== CACHE && k !== "clin-share")
                                       .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== location.origin) return;

  // Share Target: il file resta sul telefono, in una cache locale letta subito da app.js.
  if (event.request.method === "POST" && url.pathname.endsWith("/condividi")) {
    event.respondWith((async () => {
      const form = await event.request.formData();
      const cache = await caches.open("clin-share");
      const file = form.get("file");
      const text = [form.get("title"), form.get("text"), form.get("url")].filter(Boolean).join("\n");
      if (file && file.size) {
        await cache.put("file", new Response(file, { headers: { "X-Name": file.name || "condiviso" } }));
      }
      if (text) await cache.put("text", new Response(text));
      return Response.redirect("./?condiviso=1", 303);
    })());
    return;
  }

  if (event.request.method !== "GET") return;
  event.respondWith(
    caches.match(event.request, { ignoreSearch: true }).then((hit) => hit || fetch(event.request))
  );
});
