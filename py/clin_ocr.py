"""
Clin - OCR locale per le pagine scansionate e le immagini.

Motore: Tesseract, gia' compilato dentro PyMuPDF (`page.get_textpage_ocr`).
Nessuna dipendenza in piu' e nessuna rete: serve solo il modello della
lingua italiana (`ita.traineddata`, ~2,6 MB), da scaricare una volta con

    python3 scripts/fetch_ocr_model.py

Senza il modello l'OCR e' semplicemente spento: le pagine senza testo
vengono segnalate come prima.

Cartella del modello, in ordine: variabile CLIN_TESSDATA, `models/tessdata`
accanto a questo file (o dentro il bundle dell'app).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3; "fitz" e' il nome storico, deprecato
except ImportError:
    try:
        import fitz  # PyMuPDF
    except ImportError:  # app web: l'OCR la' e' Tesseract.js, qui serve solo ocr_suspects
        fitz = None

LANGUAGE = "ita"
OCR_DPI = 300          # lettura: sotto i 300 dpi Tesseract perde le cifre piccole
RENDER_DPI = 200       # immagine che sostituisce la pagina redatta
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def tessdata_dir() -> Path:
    env = os.environ.get("CLIN_TESSDATA")
    if env:
        return Path(env).expanduser()
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "models" / "tessdata"


def available() -> bool:
    return fitz is not None and (tessdata_dir() / f"{LANGUAGE}.traineddata").is_file()


# L'OCR sbaglia qualche carattere ("RSSMRA857T10A562S"): un codice deformato
# non supera piu' i controlli. Nel testo letto con l'OCR si oscura per
# prudenza ogni sequenza che somiglia a un identificativo.
_OCR_CODE = re.compile(r"\b[A-Za-z0-9]{10,}\b")
_OCR_DATE = re.compile(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}")
_OCR_NUMBER = re.compile(r"(?<![\d,.])\+?\d[\d \-./]{5,}\d(?![\d,])")


def ocr_suspects(text: str) -> List[str]:
    """Sequenze del testo OCR da trattare come dati sensibili espliciti."""
    out: List[str] = []
    for m in _OCR_CODE.finditer(text):
        tok = m.group(0)
        if sum(c.isdigit() for c in tok) >= 3 and sum(c.isalpha() for c in tok) >= 3:
            out.append(tok)
    for m in _OCR_NUMBER.finditer(text):
        tok = m.group(0)
        # le date hanno gia' le loro regole (data di nascita, livello 10)
        if sum(c.isdigit() for c in tok) >= 7 and not _OCR_DATE.fullmatch(tok.strip()):
            out.append(tok)
    return out


def ocr_words(page: "fitz.Page") -> List[tuple]:
    """Parole lette dall'OCR, nello stesso formato di page.get_text('words')."""
    tp = page.get_textpage_ocr(language=LANGUAGE, dpi=OCR_DPI, full=True,
                               tessdata=str(tessdata_dir()))
    return page.get_text("words", textpage=tp, sort=True)


def open_as_pdf(path: Path) -> "fitz.Document":
    """Apre un PDF, o converte un'immagine in un PDF di una pagina per fotogramma."""
    src = fitz.open(str(path))
    if src.is_pdf:
        return src
    try:
        return fitz.open("pdf", src.convert_to_pdf())
    finally:
        src.close()


def replace_with_redacted_image(
    page: "fitz.Page",
    rects: Sequence["fitz.Rect"],
    fill: Tuple[float, float, float] = (0, 0, 0),
    pad: float = 1.5,
) -> None:
    """
    Sostituisce la pagina con la sua immagine, gia' coperta sui riquadri.
    I pixel sotto i riquadri vengono sovrascritti prima di rientrare nel PDF:
    nell'output non resta ne' l'immagine originale ne' alcun livello di testo.
    """
    zoom = RENDER_DPI / 72
    pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
    color = tuple(int(round(c * 255)) for c in fill)
    for r in rects:
        box = (fitz.Rect(r) + (-pad, -pad, pad, pad)) * fitz.Matrix(zoom, zoom)
        pix.set_rect(box.irect & pix.irect, color)
    page.add_redact_annot(page.rect)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    page.insert_image(page.rect, pixmap=pix)
