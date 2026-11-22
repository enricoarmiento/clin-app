"""
Clin - Anonimizzazione in forma di testo, per l'invio a un'IA.

Il motore PDF oscura con riquadri neri; un modello linguistico invece ragiona
meglio se il dato sensibile e' sostituito da un segnaposto tipizzato:

    "Paziente: ROSSI MARIO, nato il 10/12/1985. Rossi riferisce..."
    -> "Paziente: [PAZIENTE_1], nato il [DATA_NASCITA_1]. [PAZIENTE_1] riferisce..."

Lo stesso valore riceve sempre lo stesso segnaposto, e le parti di uno stesso
nominativo ("ROSSI MARIO", "Rossi") vengono ricondotte alla stessa persona.
La tabella segnaposto -> valore originale resta sul computer: non va mai
inviata all'IA.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3; "fitz" e' il nome storico, deprecato
except ImportError:
    try:
        import fitz  # PyMuPDF
    except ImportError:  # app web (Pyodide): si usa solo l'analisi del testo
        fitz = None

import clin_llm
import clin_ocr

from anonymize_engine import (
    _PageIndex, collect_document_names, collect_names, find_matches, protected_spans,
    subtract_protected,
)

__all__ = [
    "DEFAULT_LEVEL", "SUPPORTED_SUFFIXES", "MAX_FILE_MB", "TextResult",
    "anonymize_text", "anonymize_file", "restore_text",
]

DEFAULT_LEVEL = 10         # nessuna scala: tutte le categorie sono sempre attive
MAX_FILE_MB = 50
SUPPORTED_SUFFIXES = ({".pdf", ".txt", ".md", ".csv", ".tsv", ".xlsx", ".xlsm", ".docx"}
                      | clin_ocr.IMAGE_SUFFIXES)

_PLACEHOLDER = {
    "Personalizzati": "DATO",
    "ID ufficiali": "ID",
    "Nome paziente": "PAZIENTE",
    "Ricorrenze nominativo": "PERSONA",
    "Data di nascita": "DATA_NASCITA",
    "Contatti": "CONTATTO",
    "Dati bancari": "IBAN",
    "Identificativi clinici": "ID_CLINICO",
    "Provenienza": "PROVENIENZA",
    "Medici": "MEDICO",
    "Personale sanitario": "OPERATORE",
    "Struttura sanitaria": "STRUTTURA",
    "Date": "DATA",
    "Documenti": "DOCUMENTO",
    "Persone": "PERSONA",
}
_PERSON_KINDS = {"PAZIENTE", "MEDICO", "OPERATORE", "PERSONA"}
_PLACEHOLDER_RX = re.compile(r"\[[A-Z_]+_\d+\]")


@dataclass
class TextResult:
    text: str
    mapping: Dict[str, str] = field(default_factory=dict)   # segnaposto -> originale
    counts: Dict[str, int] = field(default_factory=dict)    # categoria -> occorrenze
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """Riepilogo senza alcun valore originale: si puo' mostrare all'IA."""
        if not self.counts:
            return "Nessun dato sensibile rilevato."
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(self.counts.items(), key=lambda kv: -kv[1]))
        return f"Dati sostituiti con segnaposto - {parts}."


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


class _Registry:
    """Assegna segnaposto stabili; riconduce le parti di un nome alla persona.

    Il tipo della persona (PAZIENTE, MEDICO, PERSONA...) lo decide la prima
    occorrenza nel testo. Due nominativi completi sono la stessa persona solo se
    uno contiene l'altro («Mario Rossi», «ROSSI MARIO», «Rossi Mario Luigi»): un
    nome di battesimo o un cognome in comune non basta («Luca Verdi» non e'
    «Esposito Luca», la figlia «Elena Rossi» non e' il paziente «Mario Rossi»).
    Una parola sola («Rossi») si attribuisce solo se appartiene a una persona sola.
    """

    def __init__(self, names: Sequence[str]):
        self.by_value: Dict[Tuple[str, str], str] = {}
        self.mapping: Dict[str, str] = {}
        self.counters: Dict[str, int] = {}
        self.person_of: Dict[str, str] = {}   # nome completo -> segnaposto
        self.token_of: Dict[str, Optional[str]] = {}  # parola -> segnaposto (None se ambigua)
        self.people: Dict[str, frozenset] = {}  # segnaposto -> parole del nome
        self.group_of: Dict[str, Optional[str]] = {}  # nome o parola -> nominativo completo noto
        self.isolated: Dict[str, str] = {}    # persone citate solo dal contesto
        self.add_names(names)

    def add_names(self, names: Sequence[str]) -> None:
        """I nominativi completi (piu' parole) definiscono le persone."""
        for full in sorted({n.strip() for n in names if " " in n.strip()}, key=len, reverse=True):
            key = _norm(full)
            if key in self.group_of:
                continue
            self.group_of[key] = full
            for tok in key.split():
                prev = self.group_of.get(tok, full)
                # parola comune a due nominativi diversi: non indica nessuno dei due
                self.group_of[tok] = full if prev is not None and _norm(prev) == key else None

    def _register(self, ph: str, names: Sequence[str]) -> None:
        tokens = set(self.people.get(ph, frozenset()))
        for name in names:
            key = _norm(name)
            self.person_of[key] = ph
            tokens.update(key.split())
        self.people[ph] = frozenset(tokens)
        for tok in tokens:
            owner = self.token_of.get(tok, ph)
            self.token_of[tok] = ph if owner == ph else None

    @classmethod
    def from_mapping(cls, mapping: Dict[str, str]) -> "_Registry":
        """Riprende una tabella salvata, continuando la numerazione."""
        reg = cls([])
        for ph, original in mapping.items():
            kind, _, num = ph.strip("[]").rpartition("_")
            if not num.isdigit():
                continue
            reg.mapping[ph] = original
            reg.by_value[(kind, _norm(original))] = ph
            reg.counters[kind] = max(reg.counters.get(kind, 0), int(num))
            if kind in _PERSON_KINDS:
                reg._register(ph, [original])
        return reg

    def _new(self, kind: str, original: str) -> str:
        self.counters[kind] = self.counters.get(kind, 0) + 1
        ph = f"[{kind}_{self.counters[kind]}]"
        self.mapping[ph] = original
        self.by_value[(kind, _norm(original))] = ph
        return ph

    def _person(self, kind: str, original: str) -> str:
        key = _norm(original)
        tokens = frozenset(key.split())
        if key in self.person_of:
            return self.person_of[key]
        if len(tokens) >= 2:
            # stessa persona solo se un nome contiene l'altro (ordine e maiuscole a parte)
            for ph, known in self.people.items():
                if tokens <= known or known <= tokens:
                    if len(tokens) > len(known):  # "Rossi" poi "Mario Rossi": si conserva il nome intero
                        self.mapping[ph] = original
                    self._register(ph, [original])
                    return ph
            full = self.group_of.get(key) or original
        else:
            tok = next(iter(tokens), "")
            owner = self.token_of.get(tok)
            if owner:
                return owner
            full = self.group_of.get(tok) or original
            fkey = _norm(full)
            if fkey in self.person_of:
                return self.person_of[fkey]
        ph = self._new(kind, full)
        self._register(ph, [original, full])
        return ph

    def get(self, kind: str, original: str, link: bool = True) -> str:
        """`link=False`: persona citata dal contesto ("la moglie Anna"), da non
        fondere per singola parola con un'altra persona (la paziente Anna Bianchi)."""
        key = _norm(original)
        if kind in _PERSON_KINDS and not link:
            # solo corrispondenza esatta; registro a parte, invisibile alle ricerche per parola
            if key in self.isolated:
                return self.isolated[key]
            if key in self.person_of:
                return self.person_of[key]
            self.isolated[key] = self._new(kind, original)
            return self.isolated[key]
        if kind in _PERSON_KINDS:
            # "Giulia" dopo "la figlia Giulia Ferri": stesso familiare (mai il contrario)
            if key in self.isolated:
                return self.isolated[key]
            if len(key.split()) == 1 and not self.token_of.get(key):
                for full, ph in self.isolated.items():
                    if key in full.split() and sum(key in f.split() for f in self.isolated) == 1 \
                            and key not in self.token_of:
                        return ph
            return self._person(kind, original)
        if (kind, key) in self.by_value:
            return self.by_value[(kind, key)]
        return self._new(kind, original)


def find_spans(text: str, level: int = DEFAULT_LEVEL, explicit: Optional[Sequence[str]] = None,
               whitelist: Optional[Sequence[str]] = None,
               names: Sequence[str] = ()) -> List[Tuple[int, int, str]]:
    """Intervalli (inizio, fine, categoria) che `anonymize_text` sostituisce."""
    matches = find_matches(text, level, explicit, keep_labels=True, derived_targets=names)
    protected = protected_spans(text, whitelist or [])
    spans: List[Tuple[int, int, str]] = []
    for m in matches:
        for s, e in subtract_protected(m.start, m.end, protected):
            # niente spazi o punteggiatura ai bordi del segnaposto
            while s < e and text[s] in " \t\n,;:.()":
                s += 1
            while e > s and text[e - 1] in " \t\n,;:()":
                e -= 1
            if e > s:
                spans.append((s, e, m.category))
    # sovrapposizioni: vince l'intervallo piu' lungo
    # a parita' d'intervallo vince la categoria piu' specifica (es. Nome paziente su Ricorrenze)
    # il contesto ("la moglie Anna") prevale sulla semplice ricorrenza di un nome
    rank = {"Persone": 1, "Ricorrenze nominativo": 2, "Personalizzati": 2}
    spans.sort(key=lambda t: (t[0], -(t[1] - t[0]), rank.get(t[2], 0)))
    chosen: List[Tuple[int, int, str]] = []
    for s, e, cat in spans:
        if chosen and s < chosen[-1][1]:
            if e > chosen[-1][1]:
                ps, _, pcat = chosen[-1]
                chosen[-1] = (ps, e, pcat)
            continue
        chosen.append((s, e, cat))
    return chosen


def _replace(text: str, registry: _Registry, level: int,
             explicit: Optional[Sequence[str]], whitelist: Optional[Sequence[str]],
             names: Sequence[str], counts: Dict[str, int]) -> str:
    chosen = find_spans(text, level, explicit, whitelist, names)
    out: List[str] = []
    cursor = 0
    for s, e, cat in chosen:
        original = " ".join(text[s:e].split())
        # i titoli ("Dott. Bianchi") restano leggibili: si sostituisce solo il nome
        title = re.match(r"(?i)^(dott\.ssa|dott\.?|dr\.ssa|dr\.?|prof\.ssa|prof\.?|sig\.ra|sig\.?|inf\.)\s+", original)
        prefix = ""
        if title:
            prefix, original = title.group(0), original[title.end():]
        kind = _PLACEHOLDER.get(cat, "DATO")
        out.append(text[cursor:s])
        out.append(prefix + registry.get(kind, original, link=cat != "Persone"))
        counts[cat] = counts.get(cat, 0) + 1
        cursor = e
    out.append(text[cursor:])
    # "MARIO ROSSI" a parti invertite produce due segnaposto uguali in fila
    return re.sub(r"(\[[A-Z_]+_\d+\])(?:[ \t]+\1)+", r"\1", "".join(out))


def _without_relatives(text: str, names: Sequence[str], level: int, explicit, whitelist) -> List[str]:
    """I familiari citati dal contesto («la figlia Elena Rossi») non definiscono persone a cui
    ricondurre le parole sole: «il sig. Rossi» resta il paziente, non la figlia."""
    kin = set()
    for s, e, cat in find_spans(text, level, explicit, whitelist, names):
        if cat == "Persone":
            kin.add(_norm(re.sub(r"(?i)^(sig\.ra|sig\.?|dott\.ssa|dott\.?)\s+", "", text[s:e])))
    return [n for n in names if _norm(n) not in kin]


def anonymize_text(
    text: str,
    level: int = DEFAULT_LEVEL,
    explicit: Optional[Sequence[str]] = None,
    whitelist: Optional[Sequence[str]] = None,
    names: Optional[Sequence[str]] = None,
    _registry: Optional[_Registry] = None,
    previous: Optional[Dict[str, str]] = None,
) -> TextResult:
    """Sostituisce i dati sensibili di un testo con segnaposto stabili."""
    all_names = list(names or []) + collect_names(text)
    registry = _registry or (_Registry.from_mapping(previous) if previous else _Registry([]))
    registry.add_names(_without_relatives(text, all_names, level, explicit, whitelist))
    counts: Dict[str, int] = {}
    new = _replace(text, registry, level, explicit, whitelist, all_names, counts)
    return TextResult(new, dict(registry.mapping), counts)


def restore_text(text: str, mapping: Dict[str, str]) -> str:
    """
    Rimette i valori originali al posto dei segnaposto (solo in locale).

    Tollera le varianti che gli LLM producono: spazi nelle parentesi
    ("[ PAZIENTE_1 ]"), parentesi omesse ("PAZIENTE_1"), grassetto markdown.
    Le parentesi valgono "tutto o niente" e un indice inesistente ("[PAZIENTE_9]")
    resta com'e': meglio un segnaposto visibile che il paziente sbagliato.
    Un'unica sostituzione impedisce di ri-sostituire un valore appena inserito.
    """
    keys = {k.strip("[]"): v for k, v in mapping.items()}
    if not keys:
        return text
    names = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    rx = re.compile(r"\[\s*(" + names + r")\s*\]|(?<![\w\[])(" + names + r")(?![\w\]])")
    return rx.sub(lambda m: keys[m.group(1) or m.group(2)], text)


# --------------------------------------------------------------------------
# Estrazione del testo dai file
# --------------------------------------------------------------------------
def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        parts = [n for n in z.namelist()
                 if re.match(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", n)]
        chunks = []
        for name in sorted(parts, key=lambda n: (not n.endswith("document.xml"), n)):
            xml = z.read(name).decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"<w:tab/>", "\t", xml)
            xml = re.sub(r"<[^>]+>", "", xml)
            chunks.append(_unescape(xml))
    return "\n".join(chunks)


def _unescape(s: str) -> str:
    import html
    return html.unescape(s)


def _read_text(path: Path) -> str:
    """Legge un file di testo provando le codifiche comuni (Windows incluso)."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16").replace("\x00", "")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc).replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def check_path(path: Path, allowed_roots: Optional[Sequence[Path]] = None) -> Path:
    """Controlli prima di aprire un file indicato da un'IA o da un utente."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ValueError(f"File non trovato: {p.name}")
    if p.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Formato non supportato: {p.suffix} "
                         f"(ammessi: {', '.join(sorted(SUPPORTED_SUFFIXES))})")
    if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"File troppo grande (limite {MAX_FILE_MB} MB).")
    if allowed_roots:
        if not any(p.is_relative_to(Path(r).expanduser().resolve()) for r in allowed_roots):
            raise ValueError("Il file e' fuori dalle cartelle autorizzate per Clin.")
    return p


def anonymize_file(
    path: Path,
    level: int = DEFAULT_LEVEL,
    explicit: Optional[Sequence[str]] = None,
    whitelist: Optional[Sequence[str]] = None,
    allowed_roots: Optional[Sequence[Path]] = None,
    previous: Optional[Dict[str, str]] = None,
) -> TextResult:
    """
    Estrae il testo di un documento e lo restituisce anonimizzato con
    segnaposto. Il file originale non viene modificato.
    """
    p = check_path(path, allowed_roots)
    ext = p.suffix.lower()
    notes: List[str] = []
    sections: List[Tuple[str, str]] = []
    names: List[str] = []
    ocr_extra: List[str] = []   # codici "sospetti" nel testo OCR, oscurati per prudenza

    if ext == ".pdf" or ext in clin_ocr.IMAGE_SUFFIXES:
        with clin_ocr.open_as_pdf(p) as doc:
            if doc.is_encrypted and not doc.authenticate(""):
                raise ValueError("PDF protetto da password.")
            use_ocr = clin_ocr.available()
            ocr_pages = {page.number: clin_ocr.ocr_words(page) for page in doc
                         if use_ocr and not page.get_text().strip()}
            with clin_llm.cache_only():  # il modello gira sul testo delle sezioni, sotto
                names = collect_document_names(doc, level, whitelist, ocr_pages)
            for page in doc:
                if page.number in ocr_pages:
                    txt = _PageIndex(page, ocr_pages[page.number]).text
                    ocr_extra.extend(clin_ocr.ocr_suspects(txt))
                    notes.append(f"Pagina {page.number + 1}: testo letto con OCR, puo' contenere errori.")
                else:
                    txt = page.get_text("text", sort=True)
                if not txt.strip():
                    notes.append(f"Pagina {page.number + 1}: nessun testo"
                                 + ("" if use_ocr else " (probabile scansione; OCR non installato)")
                                 + ", esclusa.")
                    continue
                sections.append((f"Pagina {page.number + 1}", txt))
    elif ext == ".docx":
        sections.append(("Documento", _docx_text(p)))
    elif ext in {".xlsx", ".xlsm", ".csv", ".tsv"}:
        return _anonymize_table_file(p, level, explicit, whitelist, previous)
    else:
        sections.append(("Testo", _read_text(p)))

    for _, t in sections:
        names.extend(collect_names(t))

    # `previous`: la numerazione prosegue da letture precedenti, cosi' [PAZIENTE_1]
    # e' la stessa persona in tutti i file della sessione
    registry = _Registry.from_mapping(previous) if previous else _Registry([])
    registry.add_names(names)
    counts: Dict[str, int] = {}
    body: List[str] = []
    for title, t in sections:
        new = _replace(t, registry, level, list(explicit or []) + ocr_extra, whitelist,
                       names, counts)
        body.append(f"--- {title} ---\n{new.strip()}" if len(sections) > 1 else new.strip())
    return TextResult("\n\n".join(body), dict(registry.mapping), counts, notes)


_HEADER_KIND = {"Nome paziente": "PAZIENTE", "ID ufficiali": "ID",
                "Data di nascita": "DATA_NASCITA", "Contatti": "CONTATTO",
                "Identificativi clinici": "ID_CLINICO"}


def _header_category(header: str, level: int) -> Optional[str]:
    from anonymize_engine import CATEGORY_LEVELS
    from tabular_engine import _HEADER_RULES
    norm = re.sub(r"[\s_\-./:#°]+", " ", (header or "").lower()).strip()
    for rx, _mode, cat in _HEADER_RULES:
        if norm and level >= CATEGORY_LEVELS.get(cat, 99) and rx.fullmatch(norm):
            return cat
    return None


def _row_names(row, cats) -> List[str]:
    """Nominativi della riga: con piu' colonne di nome (Cognome, Nome) uno solo, unito."""
    vals = [" ".join(str(row[c]).split()) for c, cat in enumerate(cats)
            if cat == "Nome paziente" and c < len(row) and row[c] not in (None, "")]
    if len(vals) > 1 and all(len(v.split()) == 1 for v in vals):
        return [" ".join(vals)]
    return vals


def _is_90_plus(value: str) -> bool:
    try:
        return float(value.replace(",", ".")) >= 90
    except ValueError:
        return False


def _anonymize_table_file(path: Path, level: int, explicit, whitelist, previous=None) -> TextResult:
    with clin_llm.cache_only():  # come in tabular_engine: celle solo con le regole
        return _anonymize_table_file_rules(path, level, explicit, whitelist, previous)


def _anonymize_table_file_rules(path: Path, level: int, explicit, whitelist, previous=None) -> TextResult:
    """
    Fogli di calcolo: le colonne identificative (riconosciute
    dall'intestazione) vengono sostituite per intero; le altre celle passano
    dal rilevamento automatico, che conosce anche i nominativi delle colonne.
    """
    from tabular_engine import load_table
    sheets = load_table(path).sheets
    names: List[str] = []
    plans = []
    for sheet in sheets:
        header = ["" if h is None else str(h) for h in (sheet.rows[0] if sheet.rows else [])]
        cats = [_header_category(h, level) for h in header]
        plans.append((sheet, header, cats))
        for row in sheet.rows[1:]:
            for v in _row_names(row, cats):
                names.append(v)
                names.extend(t for t in v.split() if len(t) >= 3)
    # `previous`: la numerazione prosegue da letture precedenti, cosi' [PAZIENTE_1]
    # e' la stessa persona in tutti i file della sessione
    registry = _Registry.from_mapping(previous) if previous else _Registry([])
    registry.add_names(names)
    counts: Dict[str, int] = {}
    body: List[str] = []
    from tabular_engine import _AGE_HEADER, _norm_header
    for sheet, header, cats in plans:
        age_cols = {c for c, h in enumerate(header) if _AGE_HEADER.fullmatch(_norm_header(h))}
        lines = ["\t".join(header)]
        for row in sheet.rows[1:]:
            cells = []
            person = _row_names(row, cats)
            for c, v in enumerate(row):
                v = "" if v is None else str(v)
                cat = cats[c] if c < len(cats) else None
                if v.strip() and cat == "Nome paziente" and len(person) == 1:
                    # Cognome e Nome in colonne separate: un solo segnaposto per la persona
                    cells.append(registry.get("PAZIENTE", person[0]))
                    counts[cat] = counts.get(cat, 0) + 1
                elif v.strip() and cat:
                    cells.append(registry.get(_HEADER_KIND.get(cat, "DATO"), v))
                    counts[cat] = counts.get(cat, 0) + 1
                elif c in age_cols and _is_90_plus(v):
                    cells.append("90+")  # eta' molto alta: identifica
                else:
                    cells.append(_replace(v, registry, level, explicit, whitelist, names, counts))
            lines.append("\t".join(cells))
        title = f"--- Foglio {sheet.name} ---\n" if len(sheets) > 1 else ""
        body.append(title + "\n".join(lines))
    return TextResult("\n\n".join(body), dict(registry.mapping), counts)
