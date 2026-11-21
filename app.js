// Clin - app web. Tutto avviene nel browser: motore Python (Pyodide),
// OCR (Tesseract.js) e lettura PDF (pdf.js) sono ospitati insieme all'app.

const $ = (id) => document.getElementById(id);
const MAP_KEY = "clin.map";
const MAP_TTL_MS = 12 * 60 * 60 * 1000; // la tabella segnaposto scade dopo 12 ore
const MAX_SIDE = 2200;                  // lato massimo delle foto elaborate

let py = null;        // modulo clin_web
let tess = null;      // worker OCR, creato al primo uso
let pdfjs = null;     // pdf.js, caricato al primo uso
let pending = null;   // { kind, file } in attesa del motore
let image = null;     // { bitmap, words, redacted:Set, scale }

// ------------------------------------------------------------------ stato
function status(msg, error = false) {
  $("status").textContent = msg;
  $("status").classList.toggle("error", error);
}

function loadMap() {
  try {
    const raw = JSON.parse(localStorage.getItem(MAP_KEY) || "null");
    if (raw && Date.now() - raw.saved < MAP_TTL_MS) return raw.map || {};
    localStorage.removeItem(MAP_KEY);
  } catch (_) { /* archiviazione non disponibile */ }
  return {};
}

function saveMap(map) {
  try { localStorage.setItem(MAP_KEY, JSON.stringify({ saved: Date.now(), map })); } catch (_) {}
  refreshMapInfo();
}

function refreshMapInfo() {
  const n = Object.keys(loadMap()).length;
  $("map-info").textContent = n
    ? `${n} segnaposto salvati su questo telefono. Vengono cancellati automaticamente dopo 12 ore.`
    : "Nessun segnaposto salvato.";
}

// ------------------------------------------------------------------ motore
async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  refreshMapInfo();
  try {
    const { loadPyodide } = await import("./vendor/pyodide/pyodide.mjs");
    const pyodide = await loadPyodide({ indexURL: new URL("vendor/pyodide/", location.href).href });
    for (const f of ["anonymize_engine.py", "clin_text.py", "tabular_engine.py", "clin_ocr.py", "clin_llm.py", "clin_web.py"]) {
      const src = await (await fetch(`py/${f}`, { cache: "no-cache" })).text();
      pyodide.FS.writeFile(f, src);
    }
    pyodide.runPython("import sys; sys.path.insert(0, '.')");
    py = pyodide.pyimport("clin_web");
    window.__clinPyodide = pyodide;
    status("Pronto. Nessun dato lascia il telefono.");
    $("run").disabled = false;
    await consumeShared();
    if (pending) { const p = pending; pending = null; await handleFile(p.file); }
  } catch (err) {
    console.error(err);
    status("Impossibile avviare il motore: " + err.message, true);
  }
}

function level() { return 10; }  // nessuna scala: si rimuove sempre tutto

function apply(resJson) {
  const res = JSON.parse(resJson);
  saveMap({ ...loadMap(), ...res.mapping });
  return res;
}

function show(res) {
  $("out").hidden = false;
  $("summary").textContent = res.summary;
  $("result").textContent = res.text;
  $("notes").textContent = (res.notes || []).join(" ");
  $("out").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------------ testo
async function runText() {
  const text = $("input").value;
  if (!text.trim()) { status("Incollare un testo o scegliere un file.", true); return; }
  $("image-wrap").hidden = true;
  show(apply(py.run_text(text, level(), JSON.stringify(loadMap()))));
  status("Fatto. Controllare il risultato prima dell'invio.");
}

// ------------------------------------------------------------------ file
async function handleFile(file) {
  if (!file) return;
  if (!py) { pending = { file }; status("Il file verrà elaborato appena il motore è pronto…"); return; }
  const name = (file.name || "").toLowerCase();
  try {
    if (file.type.startsWith("image/") || /\.(jpe?g|png|heic|webp)$/.test(name)) {
      await runImage(file);
    } else if (file.type === "application/pdf" || name.endsWith(".pdf")) {
      await runPdf(file);
    } else if (/\.(docx|txt|md|csv|tsv)$/.test(name)) {
      status("Elaborazione del documento…");
      const ext = name.slice(name.lastIndexOf("."));
      const path = "/tmp/documento" + ext;  // il nome originale non serve e non viene usato
      window.__clinPyodide.FS.writeFile(path, new Uint8Array(await file.arrayBuffer()));
      $("image-wrap").hidden = true;
      show(apply(py.run_file(path, level(), JSON.stringify(loadMap()))));
      window.__clinPyodide.FS.unlink(path);
      status("Fatto. Controllare il risultato prima dell'invio.");
    } else {
      status("Formato non supportato. Excel: salvarlo come CSV.", true);
    }
  } catch (err) {
    console.error(err);
    status("Errore durante l'elaborazione: " + err.message, true);
  } finally {
    $("file").value = "";
  }
}

async function runPdf(file) {
  status("Lettura del PDF…");
  if (!pdfjs) {
    pdfjs = await import("./vendor/pdfjs/pdf.min.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = "vendor/pdfjs/pdf.worker.min.mjs";
  }
  const doc = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()),
                                        isEvalSupported: false }).promise;
  const pages = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const content = await (await doc.getPage(i)).getTextContent();
    pages.push(content.items.map((it) => it.str + (it.hasEOL ? "\n" : " ")).join(""));
  }
  await doc.destroy();
  $("image-wrap").hidden = true;
  show(apply(py.run_pages(JSON.stringify(pages), level(), JSON.stringify(loadMap()))));
  const n = pages.length === 1 ? "1 pagina" : `${pages.length} pagine`;
  status(`PDF elaborato (${n}). Controllare il risultato prima dell'invio.`);
}

// ------------------------------------------------------------------ foto
function loadScript(src) {
  return new Promise((ok, ko) => {
    const s = document.createElement("script");
    s.src = src; s.onload = ok; s.onerror = () => ko(new Error("script " + src));
    document.head.appendChild(s);
  });
}

async function ocrWorker() {
  if (tess) return tess;
  await loadScript("vendor/tess/tesseract.min.js");
  const base = new URL("vendor/tess/", location.href).href;
  tess = await window.Tesseract.createWorker("ita", 1, {
    workerPath: base + "worker.min.js",
    corePath: base,
    langPath: base.replace(/\/$/, ""),
    workerBlobURL: false,
    gzip: true,
    cacheMethod: "none",   // il modello e' gia' nella cache dell'app
  });
  return tess;
}

async function runImage(file) {
  status("Lettura della foto (OCR sul telefono)…");
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = $("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);

  const worker = await ocrWorker();
  const { data } = await worker.recognize(canvas);

  // Testo ricostruito riga per riga, con la posizione di ogni parola.
  const words = [];
  let text = "";
  for (const block of data.blocks || []) {
    for (const para of block.paragraphs || []) {
      for (const line of para.lines || []) {
        line.words.forEach((w, i) => {
          if (i > 0) text += " ";
          words.push({ start: text.length, end: text.length + w.text.length, bbox: w.bbox });
          text += w.text;
        });
        text += "\n";
      }
    }
  }

  const spans = JSON.parse(py.spans(text, level(), JSON.stringify(loadMap())));
  const redacted = new Set();
  words.forEach((w, i) => {
    if (spans.some(([s, e]) => w.start < e && w.end > s)) redacted.add(i);
  });
  image = { bitmap, words, redacted, scale };
  drawImage();

  $("image-wrap").hidden = false;
  const res = apply(py.run_text(text, level(), JSON.stringify(loadMap()), true));
  res.notes = ["Testo ricavato con OCR: verificare l'immagine, toccando le parole da oscurare."];
  show(res);
  status(`Foto elaborata: ${redacted.size} parole oscurate. Controllare prima dell'invio.`);
}

function drawImage() {
  const canvas = $("canvas");
  const ctx = canvas.getContext("2d");
  ctx.drawImage(image.bitmap, 0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000";
  const pad = 3;
  for (const i of image.redacted) {
    const b = image.words[i].bbox;
    ctx.fillRect(b.x0 - pad, b.y0 - pad, b.x1 - b.x0 + 2 * pad, b.y1 - b.y0 + 2 * pad);
  }
}

function toggleWord(ev) {
  if (!image) return;
  const canvas = $("canvas");
  const rect = canvas.getBoundingClientRect();
  const x = (ev.clientX - rect.left) * (canvas.width / rect.width);
  const y = (ev.clientY - rect.top) * (canvas.height / rect.height);
  const i = image.words.findIndex(({ bbox: b }) => x >= b.x0 - 4 && x <= b.x1 + 4 && y >= b.y0 - 4 && y <= b.y1 + 4);
  if (i < 0) return;
  image.redacted.has(i) ? image.redacted.delete(i) : image.redacted.add(i);
  drawImage();
}

function imageBlob() {
  // La ricodifica dal canvas elimina i metadati EXIF (posizione, dispositivo, data).
  return new Promise((ok) => $("canvas").toBlob(ok, "image/jpeg", 0.9));
}

// ------------------------------------------------------------------ condivisione
async function share(data) {
  if (navigator.share && (!data.files || navigator.canShare?.(data))) {
    try { await navigator.share(data); return true; } catch (_) { return true; }
  }
  return false;
}

async function copy(text, okMsg) {
  try { await navigator.clipboard.writeText(text); status(okMsg); }
  catch (_) { status("Copia non riuscita: selezionare il testo e copiarlo a mano.", true); }
}

// Android: file o testo arrivati dal menu Condividi (vedi sw.js).
async function consumeShared() {
  if (!new URLSearchParams(location.search).has("condiviso")) return;
  history.replaceState(null, "", location.pathname);
  const cache = await caches.open("clin-share");
  const fileRes = await cache.match("file");
  const textRes = await cache.match("text");
  await caches.delete("clin-share");
  if (textRes) $("input").value = await textRes.text();
  if (fileRes) {
    const blob = await fileRes.blob();
    const name = fileRes.headers.get("X-Name") || "condiviso";
    await handleFile(new File([blob], name, { type: blob.type }));
  } else if (textRes) {
    await runText();
  }
}

// ------------------------------------------------------------------ eventi
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-selected", x === b));
  $("tab-anon").hidden = b.dataset.tab !== "anon";
  $("tab-restore").hidden = b.dataset.tab !== "restore";
  refreshMapInfo();
}));

$("file").addEventListener("change", (e) => handleFile(e.target.files[0]));
$("run").addEventListener("click", runText);
$("paste").addEventListener("click", async () => {
  try { $("input").value = await navigator.clipboard.readText(); }
  catch (_) { status("Accesso agli appunti negato: incollare a mano nel riquadro.", true); }
});
$("copy").addEventListener("click", () => copy($("result").textContent, "Testo copiato: incollarlo nella chat dell'IA."));
$("share").addEventListener("click", async () => {
  if (!(await share({ text: $("result").textContent }))) copy($("result").textContent, "Testo copiato.");
});
$("canvas").addEventListener("click", toggleWord);
$("share-img").addEventListener("click", async () => {
  const file = new File([await imageBlob()], "clin-anonimizzata.jpg", { type: "image/jpeg" });
  if (!(await share({ files: [file] }))) status("Condivisione non disponibile: usare «Salva immagine».", true);
});
$("save-img").addEventListener("click", async () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await imageBlob());
  a.download = "clin-anonimizzata.jpg";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

$("paste-reply").addEventListener("click", async () => {
  try { $("reply").value = await navigator.clipboard.readText(); } catch (_) {}
});
$("do-restore").addEventListener("click", () => {
  if (!py) return;
  const out = py.restore($("reply").value, JSON.stringify(loadMap()));
  $("restored").textContent = out;
  $("restored").hidden = false;
  $("copy-restored").hidden = false;
});
$("copy-restored").addEventListener("click", () => copy($("restored").textContent, "Risposta copiata."));
$("forget").addEventListener("click", () => {
  try { localStorage.removeItem(MAP_KEY); } catch (_) {}
  refreshMapInfo();
});

boot();
