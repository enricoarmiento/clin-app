"""
Clin - riconoscimento contestuale con un modello linguistico locale (Ollama).

Le regole del motore trovano i dati con forma riconoscibile (codici, telefoni,
date, etichette). Un nome senza etichetta («riferisce la figlia Giulia»,
«discusso con Oleksandr Kovalenko») richiede invece di capire il testo: qui lo
fa un modello eseguito sul computer tramite Ollama.

* Solo locale: l'indirizzo del server deve essere localhost, altrimenti il
  modulo si disattiva. Il testo non lascia mai la macchina.
* Facoltativo: senza Ollama (o con CLIN_LLM=0) Clin usa le sole regole.
* Verificato: ogni proposta del modello deve comparire nel testo e passare i
  controlli di plausibilita' (eponimi medici, termini clinici, cifre).
* Cache per testo: anteprima, propagazione e redazione non ripetono la chiamata.

Variabili d'ambiente: CLIN_LLM=0 (disattiva), CLIN_LLM_MODEL (modello),
CLIN_OLLAMA_URL (default http://127.0.0.1:11434).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

# Misurati sul benchmark sintetico (tests/benchmark): gemma4:e4b trova tutti i
# nomi e indirizzi senza falsi positivi a ~4,5 s per documento su M1 Pro.
PREFERRED_MODELS = ["gemma4:e4b", "qwen3:4b", "gemma4:12b", "qwen2.5:7b", "qwen3.5:4b",
                    "qwen2.5:14b", "llama3.1:latest"]
MIN_CHARS = 30          # testi piu' brevi (celle, nomi file): solo regole
CHUNK_CHARS = 6000
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

PROMPT = """Dal documento sanitario qui sotto estrai SOLO:
- PERSONA: nomi e/o cognomi di persone reali (paziente, familiari, medici, operatori, firmatari), di qualsiasi lingua o origine;
- INDIRIZZO: indirizzi di abitazione (via, numero civico, CAP, citta').
Non estrarre eponimi medici (es. "Giordano" in "manovra di Giordano", "Parkinson", "Lasègue"), farmaci, esami, reparti, ospedali, titoli (Dott., Sig.).
Copia ogni valore esattamente come scritto. Se non c'e' nulla restituisci una lista vuota.

DOCUMENTO:
"""
SCHEMA = {
    "type": "object",
    "properties": {"entita": {"type": "array", "items": {
        "type": "object",
        "properties": {"testo": {"type": "string"},
                       "tipo": {"type": "string", "enum": ["PERSONA", "INDIRIZZO"]}},
        "required": ["testo", "tipo"]}}},
    "required": ["entita"],
}

# Parole che precedono un eponimo: "manovra di Giordano", "morbo di Parkinson".
_EPONYM_BEFORE = re.compile(
    r"(?i)\b(?:manovra|segno|segni|prova|prove|morbo|malattia|sindrome|scala|score|classificazione|"
    r"criteri|criterio|test|riflesso|punto|triade|legge|metodo|tecnica|intervento|operazione|"
    r"incisione|frattura|ernia|cisti|tumore|linfoma|sarcoma|nodo|fascio|ansa|dotto|canale|"
    r"tetralogia|anomalia|morbus|disease|sign|syndrome)\s+(?:di|de|del|della|of)?\s*$")
# Eponimi e termini che compaiono spesso da soli con la maiuscola.
_CLINICAL_WORDS = {w.casefold() for w in """
    Lasègue Lasegue Romberg Babinski Mingazzini Giordano Murphy Blumberg Rovsing Parkinson Alzheimer
    Crohn Glasgow Valsalva Fontaine Apgar Child Pugh Killip NYHA Wells Gleason Breslow Clark Hodgkin
    Down Turner Marfan Cushing Addison Graves Hashimoto Basedow Raynaud Sjögren Kawasaki Wilson
    Barrett Mallory Weiss Zenker Meckel Hirschsprung Wernicke Korsakoff Guillain Barré Ebstein Fallot
    Tachipirina Augmentin Coumadin Eutirox Lasix Cardioaspirina Brufen Moment Plavix Xarelto Eliquis
    """.split()}
_TITLE = re.compile(r"(?i)^(?:dott\.ssa|dott\.?|dr\.ssa|dr\.?|prof\.ssa|prof\.?|sig\.ra|sig\.na|sig\.?|"
                    r"signor[ae]?|inf\.|avv\.|ing\.)\s+")

_cache: Dict[str, List[Tuple[str, str]]] = {}
_lock = threading.Lock()
_local = threading.local()
_state: Dict[str, object] = {"model": None, "checked": False, "error": ""}


def _base_url() -> Optional[str]:
    url = os.environ.get("CLIN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    host = urllib.parse.urlparse(url).hostname or ""
    return url if host in _LOCAL_HOSTS else None  # mai un server remoto


def _post(path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(_base_url() + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def model() -> Optional[str]:
    """Modello locale in uso, o None se il riconoscimento contestuale e' spento."""
    if os.environ.get("CLIN_LLM", "1") == "0" or _base_url() is None:
        return None
    try:
        import clin_settings
        if not clin_settings.load()["modello_locale"]:
            return None
    except Exception:
        pass
    with _lock:
        if _state["checked"]:
            return _state["model"]  # type: ignore[return-value]
        _state["checked"] = True
        try:
            with urllib.request.urlopen(_base_url() + "/api/tags", timeout=2) as r:
                installed = [m["name"] for m in json.load(r).get("models", [])]
        except Exception as exc:
            _state["error"] = f"Ollama non raggiungibile ({exc.__class__.__name__})"
            return None
        wanted = os.environ.get("CLIN_LLM_MODEL")
        order = ([wanted] if wanted else []) + PREFERRED_MODELS
        _state["model"] = next((m for m in order if m in installed), None)
        if not _state["model"]:
            _state["error"] = "nessun modello adatto installato in Ollama (es. ollama pull gemma4:e4b)"
        return _state["model"]  # type: ignore[return-value]


def engine() -> str:
    """Motore per i nomi senza etichetta: "gliner", "ollama" o "" (solo regole)."""
    if os.environ.get("CLIN_LLM", "1") == "0":
        return ""
    try:
        import clin_settings
        s = clin_settings.load()
        if not s["modello_locale"]:
            return ""
        wanted = str(s.get("motore", "auto"))
    except Exception:
        wanted = "auto"
    import clin_ner
    if wanted in ("auto", "gliner") and clin_ner.installed():
        return "gliner"
    if wanted in ("auto", "ollama") and model():
        return "ollama"
    return ""


def status() -> str:
    e = engine()
    if e == "gliner":
        return "GLiNER (servizio locale)"
    m = model() if e == "ollama" else None
    return f"modello locale {m}" if m else f"solo regole: {_state['error'] or 'disattivato'}"


@contextmanager
def cache_only():
    """Nel blocco si usano solo risultati gia' calcolati (anteprima, verifiche)."""
    prev = getattr(_local, "cache_only", False)
    _local.cache_only = True
    try:
        yield
    finally:
        _local.cache_only = prev


def _chunks(text: str) -> List[str]:
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > CHUNK_CHARS and buf:
            out.append(buf)
            buf = ""
        buf += line
    return out + ([buf] if buf.strip() else [])


def is_eponym_at(text: str, start: int) -> bool:
    return bool(_EPONYM_BEFORE.search(text[max(0, start - 40):start]))


def plausible_person(value: str) -> Optional[str]:
    """Ripulisce un nome proposto; None se non sembra il nome di una persona."""
    value = _TITLE.sub("", " ".join(value.split())).strip(" .,;:()[]\"'«»")
    tokens = value.split()
    if not tokens or len(value) < 2 or len(tokens) > 6 or re.search(r"\d", value):
        return None
    if not all(t[0].isupper() or t.lower() in {"de", "di", "da", "del", "della", "van", "von", "el",
                                                 "al", "bin", "ben", "dos", "das", "le", "la", "lo"}
               for t in tokens):
        return None
    if all(t.casefold() in _CLINICAL_WORDS for t in tokens):
        return None
    return value


def entities(text: str) -> List[Tuple[str, str]]:
    """Coppie (valore, tipo) con tipo PERSONA o INDIRIZZO, gia' verificate."""
    if len(text.strip()) < MIN_CHARS:
        return []
    key = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    if key in _cache:
        return _cache[key]
    if getattr(_local, "cache_only", False):
        return []
    eng = engine()
    if not eng:
        return []
    if eng == "gliner":
        import clin_ner
        items = clin_ner.entities(text)
        if items is not None:
            result = _verified(items, text)
            _cache[key] = result
            return result
        if not model():          # servizio non disponibile: si ripiega su Ollama, se c'e'
            return []
    m = model()
    found: List[Tuple[str, str]] = []
    for chunk in _chunks(text):
        body = {"model": m, "prompt": PROMPT + chunk, "stream": False, "format": SCHEMA,
                "think": False, "keep_alive": "10m",
                "options": {"temperature": 0, "num_ctx": 8192}}
        try:
            data = _post("/api/generate", body, timeout=180)
            items = json.loads(data.get("response") or "{}").get("entita", [])
        except Exception as exc:
            _state["error"] = f"errore del modello locale ({exc.__class__.__name__})"
            return []  # niente cache: si riprova alla prossima occasione
        found += _verified([(str(it.get("testo", "")), it.get("tipo")) for it in items], text)
    result = list(dict.fromkeys(found))
    _cache[key] = result
    return result


def _verified(items, text: str) -> List[Tuple[str, str]]:
    """Le proposte del modello passano gli stessi controlli, qualunque sia il motore."""
    out = []
    for value, kind in items:
        value = value.strip()
        if kind == "PERSONA":
            value = plausible_person(value) or ""
        elif kind == "INDIRIZZO":
            value = value.strip(" .,;:") if re.search(r"\d", value) and len(value) >= 5 else ""
        if value and value in text:
            out.append((value, kind))
    return list(dict.fromkeys(out))
