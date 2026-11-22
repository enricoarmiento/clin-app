"""
Clin - Motore di anonimizzazione per fogli di calcolo (CSV, TSV, XLSX).

Condivide con il motore PDF il riconoscimento dei dati sensibili
(`anonymize_engine.find_matches`) e vi aggiunge il controllo tipico dei dati
tabellari: una regola per colonna, la scelta delle righe da trattare e la
possibilita' di forzare singole celle.

Modalita' disponibili per colonna o cella:
  * `auto`   rileva e oscura solo le porzioni sensibili (comportamento PDF)
  * `all`    oscura l'intero contenuto della cella
  * `pseudo` sostituisce il valore con uno pseudonimo stabile (PZ-0001)
  * `keep`   lascia la cella intatta

Gli pseudonimi sono coerenti all'interno dello stesso file: lo stesso valore
riceve sempre lo stesso codice, cosi' le analisi statistiche restano possibili
senza esporre l'identita'.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import clin_llm
from anonymize_engine import (
    CATEGORY_LEVELS, FileResult, excluded_categories, find_matches, protected_spans, subtract_protected,
)

__all__ = [
    "MODE_AUTO", "MODE_ALL", "MODE_PSEUDO", "MODE_KEEP", "MODE_LABELS",
    "CSV_SUFFIXES", "EXCEL_SUFFIXES", "TABLE_SUFFIXES",
    "Sheet", "TableDoc", "TableSettings", "CellPlan",
    "is_table_file", "load_table", "plan_sheet", "anonymize_table",
]

MODE_AUTO = "auto"
MODE_ALL = "all"
MODE_PSEUDO = "pseudo"
MODE_KEEP = "keep"

MODE_LABELS: Dict[str, str] = {
    MODE_AUTO: "Automatico",
    MODE_ALL: "Oscura tutto",
    MODE_PSEUDO: "Pseudonimo",
    MODE_KEEP: "Non toccare",
}

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
TABLE_SUFFIXES = CSV_SUFFIXES | EXCEL_SUFFIXES

DEFAULT_MASK = "███"          # ███
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def is_table_file(path: Path) -> bool:
    return Path(path).suffix.lower() in TABLE_SUFFIXES


# --------------------------------------------------------------------------
# Strutture dati
# --------------------------------------------------------------------------
@dataclass
class Sheet:
    """Un foglio: intestazione opzionale piu' righe di valori testuali."""
    name: str
    rows: List[List[str]] = field(default_factory=list)
    truncated: bool = False       # vero se la lettura si e' fermata a max_rows
    total_rows: int = 0

    @property
    def column_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def header(self, has_header: bool = True) -> List[str]:
        if has_header and self.rows:
            return [str(v) for v in self.rows[0]]
        return [f"Colonna {i + 1}" for i in range(self.column_count)]


@dataclass
class TableDoc:
    path: Path
    kind: str                      # "csv" oppure "excel"
    sheets: List[Sheet] = field(default_factory=list)
    delimiter: str = ","
    encoding: str = "utf-8"

    def sheet(self, name: str) -> Optional[Sheet]:
        for s in self.sheets:
            if s.name == name:
                return s
        return self.sheets[0] if self.sheets else None


@dataclass
class TableSettings:
    """Configurazione dell'anonimizzazione tabellare."""
    level: int = 10
    explicit: List[str] = field(default_factory=list)
    whitelist: List[str] = field(default_factory=list)
    keep_labels: bool = False
    has_header: bool = True
    mask: str = DEFAULT_MASK
    default_mode: str = MODE_AUTO
    # per nome foglio
    column_modes: Dict[str, Dict[int, str]] = field(default_factory=dict)
    cell_modes: Dict[str, Dict[Tuple[int, int], str]] = field(default_factory=dict)
    row_selection: Dict[str, Optional[Set[int]]] = field(default_factory=dict)
    # registro pseudonimi condiviso tra file (None: uno nuovo per file)
    pseudonyms: Optional["_PseudonymBook"] = None

    # ---- accessi comodi -------------------------------------------------
    def column_mode(self, sheet: str, col: int) -> str:
        return self.column_modes.get(sheet, {}).get(col, self.default_mode)

    def set_column_mode(self, sheet: str, col: int, mode: str) -> None:
        self.column_modes.setdefault(sheet, {})[col] = mode

    def cell_mode(self, sheet: str, row: int, col: int) -> Optional[str]:
        return self.cell_modes.get(sheet, {}).get((row, col))

    def set_cell_mode(self, sheet: str, row: int, col: int, mode: Optional[str]) -> None:
        table = self.cell_modes.setdefault(sheet, {})
        if mode is None:
            table.pop((row, col), None)
        else:
            table[(row, col)] = mode

    def rows_for(self, sheet: str) -> Optional[Set[int]]:
        return self.row_selection.get(sheet)

    def set_rows(self, sheet: str, rows: Optional[Set[int]]) -> None:
        self.row_selection[sheet] = rows

    def reset_sheet(self, sheet: str) -> None:
        self.column_modes.pop(sheet, None)
        self.cell_modes.pop(sheet, None)
        self.row_selection.pop(sheet, None)


@dataclass
class CellPlan:
    """Cosa accadra' ad una cella."""
    new_value: str
    category: str
    mode: str

    @property
    def changed(self) -> bool:
        return True


# --------------------------------------------------------------------------
# Lettura
# --------------------------------------------------------------------------
def _read_text(path: Path) -> Tuple[str, str]:
    """Legge un file di testo provando le codifiche piu' comuni."""
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _sniff_delimiter(sample: str, suffix: str) -> str:
    if suffix == ".tsv":
        return "\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def load_table(path: Path, max_rows: Optional[int] = None) -> TableDoc:
    """
    Carica un CSV/TSV o una cartella di lavoro Excel.

    `max_rows` limita le righe lette (utile per l'anteprima); l'anonimizzazione
    finale rilegge sempre il file per intero.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in EXCEL_SUFFIXES:
        return _load_excel(path, max_rows)
    if suffix in CSV_SUFFIXES:
        return _load_csv(path, max_rows)
    raise ValueError(f"Formato non supportato: {suffix}")


def _load_csv(path: Path, max_rows: Optional[int]) -> TableDoc:
    text, encoding = _read_text(path)
    delimiter = _sniff_delimiter(text[:8192], path.suffix.lower())
    rows: List[List[str]] = []
    total = 0
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    for row in reader:
        total += 1
        if max_rows is None or len(rows) < max_rows:
            rows.append([("" if v is None else str(v)) for v in row])
    sheet = Sheet(name=path.stem, rows=rows,
                  truncated=max_rows is not None and total > len(rows), total_rows=total)
    return TableDoc(path=path, kind="csv", sheets=[sheet],
                    delimiter=delimiter, encoding=encoding)


def _load_excel(path: Path, max_rows: Optional[int]) -> TableDoc:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dipende dall'ambiente
        raise RuntimeError(
            "Il supporto Excel richiede openpyxl: installalo con "
            "'python3 -m pip install openpyxl'."
        ) from exc

    wb = load_workbook(str(path), data_only=True, read_only=True)
    sheets: List[Sheet] = []
    try:
        for ws in wb.worksheets:
            rows: List[List[str]] = []
            total = 0
            for values in ws.iter_rows(values_only=True):
                total += 1
                if max_rows is None or len(rows) < max_rows:
                    rows.append([_cell_to_text(v) for v in values])
            sheets.append(Sheet(
                name=ws.title, rows=rows,
                truncated=max_rows is not None and total > len(rows), total_rows=total,
            ))
    finally:
        wb.close()
    return TableDoc(path=path, kind="excel", sheets=sheets)


def _cell_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# --------------------------------------------------------------------------
# Pianificazione (anteprima)
# --------------------------------------------------------------------------
class _PseudonymBook:
    """
    Assegna pseudonimi stabili, uno spazio di numerazione per prefisso (tipo di
    dato). Condiviso tra i file di un lotto e salvabile su disco: lo stesso
    paziente riceve lo stesso codice in ogni foglio, cosi' i file restano
    collegabili per la ricerca.
    """

    def __init__(self, maps: Optional[Dict[str, Dict[str, str]]] = None,
                 originals: Optional[Dict[str, str]] = None):
        self._maps: Dict[str, Dict[str, str]] = maps or {}
        self.originals: Dict[str, str] = originals or {}  # codice -> valore com'era scritto

    def get(self, sheet: str, col: int, prefix: str, value: str) -> str:
        table = self._maps.setdefault(prefix, {})
        key = " ".join(value.split()).casefold()
        if prefix == "PAZ":
            # "ROSSI MARIO", "Mario Rossi" e Cognome|Nome separati: stessa persona
            key = " ".join(sorted(key.split()))
        if key not in table:
            table[key] = f"{prefix}-{len(table) + 1:04d}"
        self.originals.setdefault(table[key], " ".join(value.split()))
        return table[key]

    def count(self) -> int:
        return sum(len(t) for t in self._maps.values())

    @classmethod
    def load(cls, path: Path) -> "_PseudonymBook":
        import json
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if "codici" in data:
            return cls(data["codici"], data.get("originali", {}))
        return cls(data)  # formato precedente: solo i codici

    def save(self, path: Path) -> None:
        """Scrive la tabella (contiene i valori originali): permessi 600."""
        import json
        import os
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"codici": self._maps, "originali": self.originals}, fh, ensure_ascii=False)
        os.replace(tmp, path)


# prefisso per tipo di dato: "Paziente", "Cognome e nome", "Nominativo" -> PAZ
_CATEGORY_PREFIX = {"Nome paziente": "PAZ", "ID ufficiali": "ID", "Identificativi clinici": "IDC",
                    "Medici": "MED", "Personale sanitario": "OPE", "Persone": "PER",
                    "Documenti": "DOC", "Struttura sanitaria": "STR"}


def _prefix_from_header(header: str, col: int) -> str:
    letters = re.sub(r"[^A-Za-z]", "", header).upper()
    if len(letters) >= 2:
        return letters[:3]
    return f"C{col + 1}"


def _mask_for(text: str, mask: str) -> str:
    """Maschera proporzionale alla lunghezza, entro limiti ragionevoli."""
    if not mask:
        return ""
    if len(mask) == 1:
        return mask * max(3, min(len(text), 12))
    return mask


# Intestazioni di colonna che identificano il paziente: in modalita' automatica
# la colonna intera viene trattata senza attendere un'etichetta nel valore.
# (regex sull'intestazione normalizzata, modalita', categoria che ne fissa il livello)
_HEADER_RULES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"(?:cognome|nome|nominativo|paziente|assistito|patient|surname|first name|last name|full name|name)(?: (?:e|and|del|of|paziente|patient|completo))*( nome| cognome| name)?"),
     MODE_PSEUDO, "Nome paziente"),
    (re.compile(r"(?:codice fiscale|cod fiscale|c ?f|cf paziente|tessera sanitaria|n tessera|ssn|passaporto|passport|stp|eni|team)"),
     MODE_PSEUDO, "ID ufficiali"),
    (re.compile(r"(?:data (?:di )?nascita|data nasc|nascita|dob|date of birth|birth ?date|luogo (?:e data )?(?:di )?nascita|luogo nascita|comune (?:di )?nascita)"),
     MODE_ALL, "Data di nascita"),
    (re.compile(r"(?:tel(?:efono)?|cell(?:ulare)?|phone|mobile|e ?mail|pec|indirizzo|residenza|domicilio|via|address|cap|iban)(?: \w+)?"
                r"|(?:comune|citt[aà]|city|localit[aà]|provincia|prov|regione di residenza|frazione|luogo)"
                r"(?: (?:di |del |della )?(?:residenza|domicilio|abitazione|provenienza))?"),
     MODE_ALL, "Contatti"),
    (re.compile(r"(?:id paziente|cod(?:ice)? paziente|patient id|mrn|nosologico|n nosologico|cartella(?: clinica)?|n cartella|accettazione|n accettazione"
                r"|(?:n |numero |cod(?:ice)? |id )?(?:ricetta|nre|impegnativa|prenotazione|campione|provetta|accession(?: number)?|episodio|ricovero|sdo|pratica|referto|esame|letto|stanza|barcode|protocollo)(?: n)?)"),
     MODE_PSEUDO, "Identificativi clinici"),
    # altre persone: medici, personale, familiari (pseudonimo per ruolo, collegabile tra fogli)
    (re.compile(r"(?:(?:nome |cognome )*(?:medico|dott(?:ore|oressa)?|refertante|firmatario|firma(?: medico)?|mmg|pls|medico curante|curante|chirurgo|operatore 1|primo operatore|anestesista|radiologo|cardiologo|specialista|physician|doctor)(?: \w+){0,3})"),
     MODE_PSEUDO, "Medici"),
    (re.compile(r"(?:(?:nome |cognome )*(?:infermier[ea]|operatore|tecnico|ostetrica|compilatore|compilato da|inserito da|autore|nurse|operator)(?: \w+){0,3})"),
     MODE_PSEUDO, "Personale sanitario"),
    (re.compile(r"(?:(?:nome |cognome )*(?:familiare|caregiver|tutore|amministratore di sostegno|referente|contatto(?: di)?(?: emergenza)?|parente|coniuge|madre|padre|figli[oa]|next of kin|emergency contact)(?: \w+){0,3})"),
     MODE_PSEUDO, "Persone"),
    (re.compile(r"(?:carta (?:d )?identit[aà]|n documento|numero documento|documento|patente|permesso di soggiorno)"),
     MODE_PSEUDO, "Documenti"),
    (re.compile(r"(?:ospedale|struttura|presidio|asl|ausl|asst|azienda sanitaria|centro|centro arruolante|hospital)"),
     MODE_PSEUDO, "Struttura sanitaria"),
]


_AGE_HEADER = re.compile(r"(?:eta|età|age|anni|eta anni|età anni|eta all[' ]?\w+|età all[' ]?\w+)")


def _norm_header(header: str) -> str:
    return re.sub(r"[\s_\-\.\/:#°()%]+", " ", (header or "").lower()).strip()


def header_category(header: str, level: int = 10) -> Optional[Tuple[str, str]]:
    """(modalita', categoria) suggerite dall'intestazione di colonna, o None."""
    norm = re.sub(r"[\s_\-\.\/:#°]+", " ", (header or "").lower()).strip()
    if not norm:
        return None
    excl = excluded_categories()
    for rx, mode, category in _HEADER_RULES:
        if level >= CATEGORY_LEVELS.get(category, 99) and rx.fullmatch(norm):
            return None if category in excl else (mode, category)
    return None


def _person_column(values: Sequence[str]) -> bool:
    """
    Colonna di nominativi riconosciuta dal contenuto (intestazione non nota, es. «Referente
    studio»): la maggior parte delle celle e' per intero il nome di una persona secondo il
    modello locale. Senza modello locale non si decide nulla (troppi falsi positivi).
    """
    sample = [" ".join(v.split()) for v in values if v and v.strip()][:40]
    sample = [v for v in sample if 3 <= len(v) <= 60 and not re.search(r"\d", v)]
    if len(sample) < 3 or not clin_llm.engine():
        return False
    found = {" ".join(t.split()).casefold() for t, kind in clin_llm.entities(".\n".join(sample) + ".")
             if kind == "PERSONA"}
    hits = sum(1 for v in sample if v.casefold() in found)
    return hits >= max(3, int(len(sample) * 0.6))


def header_rule(header: str, level: int) -> Optional[str]:
    """Modalita' suggerita per una colonna in base all'intestazione, o None."""
    found = header_category(header, level)
    return found[0] if found else None


def _apply_auto(
    value: str, settings: TableSettings, derived: Optional[Sequence[str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Applica il rilevamento automatico ad una cella.
    Restituisce (nuovo_valore, categoria) oppure (None, None) se nulla cambia.
    """
    # shortcut: celle senza modello locale (una chiamata per cella sarebbe lenta);
    # i nominativi arrivano dalle colonne riconosciute. Da rivedere per note lunghe.
    with clin_llm.cache_only():
        matches = find_matches(value, settings.level, settings.explicit, settings.keep_labels,
                               derived_targets=derived)
    if not matches:
        return None, None
    protected = protected_spans(value, settings.whitelist)

    spans: List[Tuple[int, int]] = []
    categories: List[str] = []
    for m in matches:
        for s, e in subtract_protected(m.start, m.end, protected):
            spans.append((s, e))
            categories.append(m.category)
    if not spans:
        return None, None

    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    merged: List[Tuple[int, int]] = []
    used_categories: List[str] = []
    for i in order:
        s, e = spans[i]
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
            used_categories.append(categories[i])

    out: List[str] = []
    cursor = 0
    for s, e in merged:
        out.append(value[cursor:s])
        out.append(_mask_for(value[s:e], settings.mask))
        cursor = e
    out.append(value[cursor:])
    new_value = "".join(out)
    if new_value == value:
        return None, None
    return new_value, used_categories[0]


def plan_sheet(
    sheet: Sheet,
    settings: TableSettings,
    pseudonyms: Optional[_PseudonymBook] = None,
    max_rows: Optional[int] = None,
) -> Dict[Tuple[int, int], CellPlan]:
    """
    Calcola, senza modificare nulla, cosa accadra' ad ogni cella del foglio.
    Usato dall'anteprima e dall'esecuzione vera e propria.
    """
    plans: Dict[Tuple[int, int], CellPlan] = {}
    book = pseudonyms if pseudonyms is not None else _PseudonymBook()
    header = sheet.header(settings.has_header)
    allowed_rows = settings.rows_for(sheet.name)
    first_data_row = 1 if settings.has_header else 0

    # Regole dedotte dall'intestazione, solo per colonne senza regola esplicita.
    explicit_cols = settings.column_modes.get(sheet.name, {})
    header_modes: Dict[int, str] = {}
    header_prefix: Dict[int, str] = {}
    if settings.has_header and settings.default_mode == MODE_AUTO:
        for c, h in enumerate(header):
            if c not in explicit_cols:
                found = header_category(h, settings.level)
                if found:
                    header_modes[c] = found[0]
                    if found[1] in _CATEGORY_PREFIX:
                        header_prefix[c] = _CATEGORY_PREFIX[found[1]]

    # I nominativi delle colonne identificative vengono cercati anche nelle
    # celle di testo libero (note, diagnosi, anamnesi).
    derived: List[str] = []
    name_rx = _HEADER_RULES[0][0]  # solo le colonne di nominativo
    name_cols = [c for c, h in enumerate(header)
                 if name_rx.fullmatch(re.sub(r"[\s_\-\.\/:#°]+", " ", (h or "").lower()).strip())]
    if settings.level >= CATEGORY_LEVELS["Ricorrenze nominativo"]:
        for row in sheet.rows[first_data_row:]:
            for c in name_cols:
                if c < len(row) and row[c] is not None:
                    full = " ".join(str(row[c]).split())
                    if len(full) >= 3:
                        derived.append(full)
                    derived.extend(t for t in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’]{3,}", full))
        derived = sorted(set(derived), key=len, reverse=True)

    # colonne di nominativi senza intestazione nota: decise dal contenuto
    try:
        import clin_settings
        by_content = clin_settings.load().get("colonne_dal_contenuto", True)
    except ImportError:  # app web: nessun modello locale, nessuna impostazione
        by_content = False
    if (settings.has_header and settings.default_mode == MODE_AUTO and by_content
            and "Persone" not in excluded_categories()):
        for c in range(sheet.column_count):
            if c in explicit_cols or c in header_modes:
                continue
            col = [str(row[c]) for row in sheet.rows[first_data_row:first_data_row + 60]
                   if c < len(row) and row[c] is not None]
            if _person_column(col):
                header_modes[c], header_prefix[c] = MODE_PSEUDO, "PER"
                for v in col:
                    derived.append(" ".join(v.split()))
        derived = sorted(set(derived), key=len, reverse=True)
    sheet.detected = list(derived)  # per ripulire anche il nome del file

    person_cols = [c for c, p in header_prefix.items() if p == "PAZ"]
    age_cols = {c for c, h in enumerate(header)
                if c not in explicit_cols and _AGE_HEADER.fullmatch(_norm_header(h))}

    for r, row in enumerate(sheet.rows):
        if max_rows is not None and r >= max_rows:
            break
        if r < first_data_row:
            continue  # l'intestazione non viene mai alterata
        if allowed_rows is not None and r not in allowed_rows:
            continue
        for c, raw in enumerate(row):
            value = "" if raw is None else str(raw)
            if not value.strip():
                continue
            mode = (settings.cell_mode(sheet.name, r, c) or header_modes.get(c)
                    or settings.column_mode(sheet.name, c))
            if mode == MODE_KEEP:
                continue
            if mode == MODE_ALL:
                plans[(r, c)] = CellPlan(_mask_for(value, settings.mask),
                                         "Colonna oscurata", mode)
                continue
            if mode == MODE_PSEUDO:
                prefix = header_prefix.get(c) or _prefix_from_header(header[c] if c < len(header) else "", c)
                if prefix == "PAZ" and len(person_cols) > 1 and c in person_cols:
                    # Cognome e Nome in colonne separate: un solo codice per la persona
                    value = " ".join(str(row[k]) for k in person_cols if k < len(row) and row[k] is not None)
                plans[(r, c)] = CellPlan(book.get(sheet.name, c, prefix, value),
                                         "Pseudonimo", mode)
                continue
            if c in age_cols:
                try:
                    if float(value.replace(",", ".")) >= 90:
                        plans[(r, c)] = CellPlan("90+", "Data di nascita", mode)  # eta' molto alta: identifica
                except ValueError:
                    pass
                continue
            new_value, category = _apply_auto(value, settings, derived)
            if new_value is not None:
                plans[(r, c)] = CellPlan(new_value, category or "Personalizzati", mode)

    return plans


# --------------------------------------------------------------------------
# Scrittura
# --------------------------------------------------------------------------
def anonymize_table(
    input_path: Path,
    output_path: Path,
    settings: TableSettings,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> FileResult:
    """
    Anonimizza un CSV/TSV o un file Excel secondo le regole indicate.
    Non solleva eccezioni: gli errori finiscono in `result.error`.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    result = FileResult(input_path=input_path, output_path=output_path)

    try:
        doc = load_table(input_path)  # lettura completa
        result.pages = len(doc.sheets)
        book = settings.pseudonyms if settings.pseudonyms is not None else _PseudonymBook()
        all_plans: Dict[str, Dict[Tuple[int, int], CellPlan]] = {}

        for sheet in doc.sheets:
            if should_cancel and should_cancel():
                result.error = "Operazione annullata."
                return result
            plans = plan_sheet(sheet, settings, book)
            all_plans[sheet.name] = plans
            result.detected_names.extend(getattr(sheet, "detected", [])[:200])
            for plan in plans.values():
                result.redactions += 1
                result.per_category[plan.category] = (
                    result.per_category.get(plan.category, 0) + 1
                )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if doc.kind == "csv":
            _write_csv(doc, all_plans, output_path)
        else:
            _write_excel(input_path, all_plans, output_path, settings)

        if result.redactions == 0:
            result.warnings.append(
                "Nessuna cella modificata: verifica il livello di sensibilita' "
                "o imposta una regola sulle colonne."
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def _write_csv(
    doc: TableDoc,
    all_plans: Dict[str, Dict[Tuple[int, int], CellPlan]],
    output_path: Path,
) -> None:
    sheet = doc.sheets[0]
    plans = all_plans.get(sheet.name, {})
    with open(output_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=doc.delimiter)
        for r, row in enumerate(sheet.rows):
            writer.writerow([
                plans[(r, c)].new_value if (r, c) in plans else value
                for c, value in enumerate(row)
            ])


def _write_excel(
    input_path: Path,
    all_plans: Dict[str, Dict[Tuple[int, int], CellPlan]],
    output_path: Path,
    settings: TableSettings,
) -> None:
    from openpyxl import load_workbook

    # Si riapre senza `data_only` per non perdere formule e formattazione.
    wb = load_workbook(str(input_path), data_only=False)
    try:
        for ws in wb.worksheets:
            plans = all_plans.get(ws.title, {})
            for (r, c), plan in plans.items():
                cell = ws.cell(row=r + 1, column=c + 1)
                cell.value = plan.new_value
            # Note di cella e intestazioni di stampa sfuggono all'analisi e
            # contengono spesso nominativi: vengono eliminate.
            for row in ws.iter_rows():
                for cell in row:
                    if cell.comment is not None:
                        cell.comment = None
            for hf in (ws.oddHeader, ws.oddFooter, ws.evenHeader, ws.evenFooter,
                       ws.firstHeader, ws.firstFooter):
                for part in (hf.left, hf.center, hf.right):
                    part.text = None
        # Le proprieta' del documento contengono spesso autore e azienda.
        try:
            props = wb.properties
            # Va usata la stringa vuota: assegnando None openpyxl reinserisce
            # i propri valori predefiniti (per esempio creator="openpyxl").
            for attr in ("creator", "lastModifiedBy", "title", "subject",
                         "description", "keywords", "category", "identifier"):
                if hasattr(props, attr):
                    setattr(props, attr, "")
        except Exception:
            pass
        wb.save(str(output_path))
    finally:
        wb.close()
