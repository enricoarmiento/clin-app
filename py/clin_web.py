"""
Clin - ponte fra l'app web (JavaScript) e il motore Python, eseguito nel
browser con Pyodide. Tutte le funzioni accettano e restituiscono JSON.
Nessuna funzione esegue I/O di rete: il motore lavora solo sul testo ricevuto.
"""

import json
import os
from pathlib import Path

# nel browser non ci sono modelli locali (GLiNER/Ollama): solo regole e contesto
os.environ["CLIN_LLM"] = "0"

from anonymize_engine import collect_names, find_matches
from clin_ocr import ocr_suspects
from clin_text import (
    _Registry, _replace, anonymize_file, anonymize_text, restore_text,
)


def _prev(previous_json):
    prev = json.loads(previous_json or "{}") or {}
    return prev, [v for k, v in prev.items() if k.startswith("[PAZIENTE_")]


def _out(res):
    return json.dumps({"text": res.text, "mapping": res.mapping, "counts": res.counts,
                       "notes": getattr(res, "notes", []), "summary": res.summary()},
                      ensure_ascii=False)


def run_text(text, level, previous_json, ocr=False):
    """Testo libero (incollato, trascrizione audio, OCR)."""
    prev, names = _prev(previous_json)
    explicit = ocr_suspects(text) if ocr else None
    return _out(anonymize_text(text, level=int(level), previous=prev or None, names=names,
                               explicit=explicit))


def run_pages(pages_json, level, previous_json):
    """Pagine di un PDF: i nominativi trovati in una pagina valgono per tutte."""
    from clin_text import TextResult
    pages = json.loads(pages_json)
    prev, names = _prev(previous_json)
    for t in pages:
        names.extend(collect_names(t))
    registry = _Registry.from_mapping(prev) if prev else _Registry([])
    registry.add_names(names)
    counts, body, notes = {}, [], []
    for i, t in enumerate(pages, 1):
        if not t.strip():
            notes.append(f"Pagina {i}: nessun testo (scansione). Caricarla come foto per l'OCR.")
            continue
        new = _replace(t, registry, int(level), None, None, names, counts)
        body.append(f"--- Pagina {i} ---\n{new.strip()}" if len(pages) > 1 else new.strip())
    return _out(TextResult("\n\n".join(body), dict(registry.mapping), counts, notes))


def run_file(path, level, previous_json):
    """DOCX, CSV, TSV, TXT, MD scritti nel file system virtuale di Pyodide."""
    prev, _ = _prev(previous_json)
    return _out(anonymize_file(Path(path), level=int(level), previous=prev or None))


def spans(text, level, previous_json):
    """Intervalli sensibili del testo di una foto, per oscurarne le parole."""
    _, names = _prev(previous_json)
    names = names + collect_names(text)
    out = []
    for m in find_matches(text, int(level), ocr_suspects(text), keep_labels=True,
                          derived_targets=names):
        out.append([m.start, m.end, m.category])
    return json.dumps(out)


def restore(text, mapping_json):
    return restore_text(text, json.loads(mapping_json or "{}"))
