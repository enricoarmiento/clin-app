"""
Clin - Motore di anonimizzazione PDF.

Il motore individua dati sensibili in documenti sanitari tramite pattern
multilingua organizzati su una scala di sensibilita' 1-10 e applica
redazioni irreversibili (il testo viene rimosso dal contenuto, non solo
coperto).

Punti chiave dell'implementazione:

* Le corrispondenze vengono cercate su una ricostruzione del testo di pagina
  costruita a partire dalle singole parole. Ogni carattere e' quindi mappato
  su un rettangolo reale, il che consente di redigere anche corrispondenze
  distribuite su piu' righe (cosa impossibile con `Page.search_for`).
* La whitelist non annulla piu' l'intera corrispondenza: le parole protette
  vengono sottratte dalla corrispondenza, il resto viene comunque oscurato.
* Il documento in uscita viene sanificato (metadati, annotazioni, allegati,
  JavaScript, segnalibri, link) e salvato compattato.
* Ogni file produce un `FileResult` con statistiche, avvisi ed errori: un file
  problematico non interrompe piu' l'elaborazione del lotto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24.3; "fitz" e' il nome storico, deprecato
except ImportError:
    try:
        import fitz  # PyMuPDF
    except ImportError:  # app web (Pyodide): si usa solo l'analisi del testo
        fitz = None

import clin_ocr

import clin_llm

__all__ = [
    "Detection",
    "FileResult",
    "BatchResult",
    "CATEGORY_LEVELS",
    "LEVEL_DESCRIPTIONS",
    "MIN_LEVEL",
    "MAX_LEVEL",
    "parse_text_lines",
    "parse_lines_exact",
    "find_matches",
    "protected_spans",
    "subtract_protected",
    "TextMatch",
    "collect_names",
    "collect_document_names",
    "analyze_page",
    "analyze_pdf_page",
    "anonymize_single_pdf",
    "anonymize_document",
    "process_batch_files",
]

MIN_LEVEL = 1
MAX_LEVEL = 10

# --------------------------------------------------------------------------
# Pattern di riconoscimento
# --------------------------------------------------------------------------
# Nei pattern etichetta-valore il gruppo denominato `v` isola il valore
# sensibile: se l'utente sceglie di mantenere visibili le etichette viene
# oscurato soltanto quel gruppo.

# Da 1 a 4 parole con iniziale maiuscola (nomi propri), anche con particelle
# tipo "De", "Di", "D'". `_CAP_NAME_CS` forza la sensibilita' alle maiuscole
# anche dentro un pattern con (?i).
_UP = "A-ZÀ-ÖØ-ÞĀĂĄĆĈĊČĎĐĒĔĖĘĚĜĞĠĢĤĦĨĪĬĮİĴĶĹĻĽĿŁŃŅŇŊŌŎŐŒŔŖŘŚŜŞŠŢŤŦŨŪŬŮŰŲŴŶŸŹŻŽȘȚ"
_LETTER = "A-Za-zÀ-ÖØ-öø-ɏȘșȚțḀ-ỿ"
_CAP_WORD = r"[" + _UP + r"][" + _LETTER + r"'’]+"
# particelle dei cognomi ("Lo Iacono", "De Luca", "Dela Cruz", "El Amrani", "van der Berg")
_PARTICLE = r"(?:[Dd][aeiu]|[Dd]e[il]|[Dd]ell[ao']|[Dd]ela|[Ll][aeio]|[Vv][ao]n|[Dd]er|[Ee]l|[Aa]l|[Bb]in|[Bb]en|[Dd]os|[Dd]as)"
_CAP_NAME = _CAP_WORD + r"(?:[ \t]+(?:" + _PARTICLE + r"[ \t]+)?" + _CAP_WORD + r"){0,3}"
_CAP_NAME_CS = r"(?-i:" + _CAP_NAME + r")"

# Nome di via: parole maiuscole, particelle minuscole ("dei", "della"), numeri
# romani o date ("XX Settembre", "4 Novembre"). Deve iniziare con maiuscola o cifra.
_STREET_TYPES = (r"(?:Via|Viale|V\.le|V\.|Piazza|Piazzale|P\.zza|P\.za|P\.le|Corso|C\.so|Largo|Vicolo|"
                 r"Vico|Strada(?:\s+(?:Statale|Provinciale|Comunale|Vicinale))?|S\.S\.|SS|S\.P\.|Str\.|"
                 r"Contrada|C\.da|Loc\.|Localit[àa]|Frazione|Fraz\.|Borgo|Lungomare|Lungotevere|Lungarno|"
                 r"Circonvallazione|Traversa|Salita|Discesa|Calle|Campo|Rione|Galleria|Vicoletto|Stradone)")
_STREET_WORD = r"(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.]*|[IVXLC]{1,6}|\d{1,2})"
_STREET_PART = r"(?:d[ei]|de[il]|dell[ao']?|degli|delle|d'|da|la|le|lo|il|san|santa|sant')"
_STREET_NAME = (_STREET_WORD + r"(?:[ \t]+(?:" + _STREET_PART + r"[ \t]+|" + _STREET_PART + r"(?=[A-ZÀ-Ö]))?"
                + _STREET_WORD + r"){0,5}")
_NOT_STREET = (r"(?!(?:orale|os|endovenosa|e\.?v\.?|intramuscolare|i\.?m\.?|sottocutanea|s\.?c\.?|"
               r"rettale|sublinguale|inalatoria|topica|transdermica|aeree?|biliari?|urinari[ae]|"
               r"telefonica|telefono|e-?mail|mail|pec|fax|web|posta|internet|libera|di|del|della)\b)")
_CIVICO = r"(?:,?[ \t]*(?:n\.?[ \t]*|n°[ \t]*)?\d{1,4}(?:[ \t]*/?[ \t]*[A-Za-z](?![A-Za-z]))?(?:/\d{1,3})?)"

_MONTHS_IT = r"(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre|genn?\.?|febb?r?\.?|mar\.?|apr\.?|magg?\.?|giu\.?|lug\.?|ago\.?|sett?\.?|ott\.?|nov\.?|dic\.?)"
_MONTHS_EN = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"

# 1. Identificativi ufficiali (CF italiano, passaporti, SSN, STP, ENI)
OFFICIAL_ID_PATTERNS = [
    # Codice Fiscale, incluse le varianti per omocodia (cifre sostituite da LMNPQRSTUV)
    r'(?i)\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b',
    r'\b80380\d{15}\b',  # numero tessera sanitaria TEAM (20 cifre)
    r'(?i)\b(?:STP|ENI)[\s\-]?(?P<v>\d{3}[\s\-]?\d{3}[\s\-]?\d{7,10})\b',
    r'(?i)\b(?:Passaporto|Passport|STP|ENI|NIE|DNI|SSN)[:\s]+(?P<v>[A-Za-z0-9\-\/]{5,})\b',
    r'(?i)\b(?:Tessera\s*Sanitaria|Tessera\s*San\.?|Health\s*Card|TEAM)[:\s]*(?:n\.?|nr\.?|n°)?[:\s]*(?P<v>[0-9A-Za-z\-\/]{5,})',
]

# 2. Etichette con nominativo del paziente (IT, EN, FR, ES, DE)
PATIENT_LABEL_PATTERNS = [
    r'(?i)\b(?:Paziente|Patient|Nominativo|Assistito|Cognome\s*e?\s*Nome|Nome\s*e?\s*Cognome|Patient\s*Name|Nom\s*et\s*prénom|Nombre\s*y\s*apellido)\s*[:]\s*(?P<v>[A-Za-zÀ-ÖØ-öø-ɏȘșȚțḀ-ỿ\s\'\-]{2,})',
    r'(?i)\b(?:Paziente|Patient|Nominativo|Assistito|Cognome\s*e?\s*Nome|Nome\s*e?\s*Cognome|Patient\s*Name|Nom\s*et\s*prénom|Nombre\s*y\s*apellido)\s+(?P<v>' + _CAP_NAME_CS + r')',
    r'(?i)\b(?:Cognome|Surname|Nom|Apellido|Nachname)\s*[:]\s*(?P<v>[A-Za-zÀ-ÖØ-öø-ɏȘșȚțḀ-ỿ\s\'\-]{2,})',
    r'(?i)\b(?:Cognome|Surname|Nom|Apellido|Nachname)\s+(?P<v>' + _CAP_NAME_CS + r')',
    r'(?i)\b(?:Nome|First\s*name|Prénom|Nombre|Vorname)\s*[:]\s*(?P<v>[A-Za-zÀ-ÖØ-öø-ɏȘșȚțḀ-ỿ\s\'\-]{2,})',
    r'(?i)\b(?:Nome|First\s*name|Prénom|Nombre|Vorname)\s+(?P<v>' + _CAP_NAME_CS + r')',
    # Titoli di cortesia: il valore comprende solo parole con iniziale maiuscola,
    # cosi' "Sig. Rossi ha riferito" oscura "Rossi" e non l'intera frase.
    r'\b(?i:Sig(?:\.ra|\.na|\.re|\.)?|Signor[ae]?)\s+(?P<v>' + _CAP_NAME + r')',
    r'\b(?i:Mr\.?|Mrs\.?|Ms\.?|Mme|Mlle|Sr\.?|Sra\.?|Herr|Frau)\s+(?P<v>' + _CAP_NAME + r')',
    r'(?i)\b(?:Il|La)\s+(?:piccol[oa]|bambin[oa]|neonat[oa]|minore)\s+(?P<v>' + _CAP_NAME_CS + r')',
]

# 2-bis. Persone citate senza etichetta, riconosciute dal contesto (parentela,
# ruolo): "riferisce la figlia Giulia", "accompagnato dal marito Paolo Neri".
_KIN = (r"(?:moglie|marito|coniuge|figli[oa]|fratello|sorella|madre|padre|mamma|pap[àa]|nonn[oa]|nipote|"
        r"zi[oa]|cugin[oa]|genero|nuora|suocer[oa]|cognat[oa]|compagn[oa]|convivente|fidanzat[oa]|"
        r"badante|caregiver|tutore|tutrice|amministratore\s+di\s+sostegno|curatore|vicin[oa](?:\s+di\s+casa)?|"
        r"amic[oa]|collega|interprete|mediatore\s+culturale|mediatrice\s+culturale)")
_KIN_BEFORE = re.compile(r'(?i)\b' + _KIN + r'[ \t]*,?[ \t]*$')
PERSON_CONTEXT_PATTERNS = [
    r'\b(?i:' + _KIN + r')[ \t]*,?[ \t]+(?:(?i:sig\.?(?:ra|na)?|signor[ae]?)[ \t]+)?(?P<v>' + _CAP_NAME_CS + r')',
    r'\b(?i:discuss[oa]|concordat[oa]|condivis[oa]|parlato|sentit[oa]|contattat[oa]|informat[oa]|'
    r'visitat[oa]|vist[oa])[ \t]+(?i:con|da|dal|dalla|col|il|la)[ \t]+'
    r'(?:(?i:dott\.ssa|dott\.?|dr\.?|prof\.?|sig\.?(?:ra)?)[ \t]+)?(?P<v>' + _CAP_NAME_CS + r'(?=[ \t]+[A-ZÀ-Ö]|[ \t]*[(,]))',
]

# 3. Date di nascita e dati anagrafici
DOB_PATTERNS = [
    r'(?i)\b(?:Data\s*(?:di)?\s*Nascita|Data\s*Nasc\.?|D\.?N\.?|Nascita|Luogo\s*e\s*Data\s*di\s*Nascita|Date\s*of\s*Birth|DOB|Birth\s*Date|Date\s*de\s*naissance|Fecha\s*de\s*nacimiento|Geburtsdatum)[:\s]+(?P<v>[^\n\r,;\t]{2,})',
    r'(?i)\b(?:Nato\/a|Nato|Nata|Né\(e\)|Né|Née|Nacido\/a|Geboren)(?:\s*il|\s*le|\s*el|\s*am)?[:\s]+(?P<v>[^\n\r,;\t]{2,})',
    r'(?i)\b(?:Et[àa]|Age|Anni)[:\s]+(?P<v>\d{1,3}(?![.,]\d)\s*(?:anni|aa|mesi|y|yrs)?)\b',
    # eta' in prosa ("di 45 anni", "di 5 mesi"); le durate ("da 3 giorni") restano visibili
    r'(?i)\bdi\s+(?P<v>\d{1,3}\s+(?:anni|mesi))\b(?!\s+(?:di|da|fa|prima|dopo|dal|dalla|dall)\b|\s+(?:terapia|trattamento|follow|distanza|durata))',
    r'(?i)\b(?P<v>\d{1,3})[\s\-]?(?:enne|year[\s\-]old|anni\s+compiuti)\b',
]

# 4. Recapiti (telefono, email, indirizzo)
CONTACT_PATTERNS = [
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',  # Email
    r'(?i)\b(?:Tel\.?|Telefono|Phone|Cell\.?|Cellulare|Mobile|Fax|Recapito)[:\s]+(?P<v>[0-9\+\s\-\(\)\/]{5,})',
    # "Via", "Piazza" non sono etichette: "per via orale" non e' un indirizzo
    r'(?i)\b(?:Residenza|Residente|Indirizzo|Domicilio|Domiciliat[oa]|Address|Adresse)\s*[:]\s*(?P<v>[^\n\r;\t]{2,})',
    r'(?i)\b(?:Residenza|Indirizzo|Domicilio|Address)[ \t]+(?=[A-ZÀ-Ö0-9])(?-i:(?P<v>[A-ZÀ-Ö0-9][^\n\r;\t]{2,}))',
    r'(?i)\b(?:Comune|Citt[àa]|City|Localit[àa])(?:\s+di\s+residenza)?\s*:\s*(?P<v>[^\n\r;\t,]{2,})',
    r'(?i)\b(?:CAP|Codice\s*Postale|Postal\s*Code|ZIP)[:\s]+(?P<v>\d{4,6})\b',
    r'(?i)\b(?:PEC|E-?mail|Posta\s*elettronica)[:\s]+(?P<v>\S+@\S+)',
    # indirizzo senza etichetta: "Via Roma 12", "P.zza Garibaldi, 3", "Via dei Mille 5"
    # con l'iniziale maiuscola ("Via Roma", "P.zza Garibaldi") il civico e' facoltativo
    r'\b' + _STREET_TYPES + r'[ \t]+' + _STREET_NAME + _CIVICO + r'?',
    # "via"/"piazza" minuscolo ("abita in via Roma 12"): solo con numero civico,
    # e mai le vie di somministrazione o i mezzi ("per via orale", "via email")
    r'(?i)\b(?:via|viale|piazza|corso|largo|vicolo|strada|contrada|v\.le|p\.zza)[ \t]+' + _NOT_STREET
    + r'[a-zà-ùA-ZÀ-Ö\'][a-zà-ùA-ZÀ-Ö\']*(?:[ \t]+[a-zà-ùA-ZÀ-Ö\']+){0,3}[ \t]+\d{1,4}[a-z]?\b'
    + r'(?![ \t]*(?:cp|cpr|compress|mg|g|ml|gtt|gocce|fl|fiale|volte|x|ui|mcg|/))',
    # CAP seguito dal comune: "00185 Roma", "20121 Milano (MI)"
    r'(?<![\d.,/])\b\d{5}[ \t]+(?-i:[A-ZÀ-Ö][a-zà-öø-ÿ\']+(?:[ \t]+(?:[A-ZÀ-Ö][a-zà-öø-ÿ\']+|di|del|sul|in|d\'))*)(?:[ \t]*\(?[A-Z]{2}\)?(?![A-Za-z]))?',
    # "residente a Roma", "domiciliata in Via ..." (la via e' coperta dal pattern sopra)
    r'(?i)\b(?:residente|domiciliat[oa]|abitante|vive|nat[oa])[ \t]+(?:a|ad|in)[ \t]+(?P<v>(?-i:[A-ZÀ-Ö][a-zà-öø-ÿ\']+(?:[ \t]+[A-ZÀ-Ö][a-zà-öø-ÿ\']+){0,2}))',
    # telefoni senza etichetta (cellulari, fissi, internazionali)
    r'(?<![\d,./])(?:(?:\+|00)39[ \t.\-]?)?3\d{2}(?:[ \t.\-/]?\d){6,7}(?!\d|[.,]\d)',
    r'(?<![\d,./])(?:(?:\+|00)39[ \t.\-]?)?0\d{1,3}[ \t.\-/]\d{2,4}(?:[ \t.\-]?\d{2,5}){0,2}(?!\d|[.,]\d)',
    r'(?<![\d,./])(?:(?:\+|00)39[ \t.\-]?)?0\d{8,9}(?!\d|[.,]\d)',  # compatto: 9-10 cifre (P.IVA ne ha 11)
    r'(?<![\w+])\+(?!39)\d{1,3}[ \t.\-]?\(?\d{1,4}\)?(?:[ \t.\-]?\d{2,4}){2,4}\b',
]

# 4-bis. Dati bancari / finanziari
FINANCIAL_PATTERNS = [
    r'\b[A-Z]{2}\d{2}[ ]?[A-Z0-9]{4}(?:[ ]?[A-Z0-9]{4}){2,6}\b',  # IBAN
    r'(?i)\b(?:IBAN|C\/C|Conto\s*Corrente)[:\s]+(?P<v>[A-Z0-9\s]{10,34})',
    r'(?i)\b(?:P\.?\s?IVA|Partita\s*I\.?V\.?A\.?|VAT)[:\s]*(?:n\.?\s*)?(?P<v>(?:IT)?\d{11})\b',
    r'\b\d{11}\b',                                      # P.IVA isolata: solo con checksum valido
    r'\b(?:\d{4}[ \-.]?){3}\d{1,4}\b|\b\d{4}[ \-]?\d{6}[ \-]?\d{5}\b',  # carte: solo con Luhn
]

# 5. Numeri cartella clinica / identificativi ospedalieri
RECORD_PATTERNS = [
    r'(?i)\bCartella(?:\s*Clinica)?(?:\s*(?:N\.?|n\.?|N°|n°|nº))?[:\s]+(?P<v>[0-9A-Za-z\/\-]+)',
    r'(?i)\b(?:Nosologico|Accettazione|Episode|MRN|Medical\s*Record\s*N°?|Case\s*N°?|File\s*N°?)[:\s]+(?P<v>[0-9A-Za-z\/\-]+)',
    r'(?i)\b(?:ID\s*Paziente|Cod\.?\s*Paziente|Patient\s*ID|Code\s*patient)[:\s]+(?P<v>[0-9A-Za-z\/\-]+)',
    r'(?i)\bCodice\s*Fiscale[:\s]+(?P<v>[A-Za-z0-9]+)',
    r'(?i)\b(?:Referto|Report)\s*(?:N\.?|n\.?|N°|nº)[:\s]*(?P<v>[0-9A-Za-z\/\-]{3,})',
    # "Verbale di Pronto Soccorso n. 0912345", "Scheda di dimissione n. 12345"
    r'(?i)\b(?:verbale|pratica|richiesta|scheda|cartella|referto|certificato|lettera|documento|modulo|'
    r'consenso|impegnativa|ricetta|denuncia|segnalazione)\b[^\n\d:]{0,40}?\b(?:n\.|nr\.?|n°|nº|numero)\s*[:.]?\s*'
    r'(?P<v>\d[A-Za-z0-9\/\-.]{2,}[A-Za-z0-9])',
    # nosologico/nosografico, ricovero, accesso, richiesta, prenotazione, impegnativa...
    # il valore deve contenere almeno una cifra
    r'(?i)\b(?:(?:N(?:um(?:ero)?)?\.?|Nr\.?|N°|Cod(?:ice)?\.?|ID)\s*)?(?:nosologico|nosografico|nosol\.?|nos\.|ricovero|'
    r'episodio|accesso(?:\s*PS)?|verbale(?:\s*PS)?|pratica|richiesta|prenotazione|impegnativa|ricetta|NRE|SDO|'
    r'campione|provetta|prelievo|accettazione|cartella|fascicolo|registro|posizione|barcode|accession|'
    r'protocollo|prot\.|documento|doc\.|rif\.|riferimento|ns\.?\s*rif\.?|referto|esame|ordine|'
    r'prestazione\s*n|lettera|certificato)'
    r'\s*(?P<mk>(?:n(?:um(?:ero)?)?\.?|nr\.?|n°|nº|#|id|cod(?:ice)?\.?)\s*[:.]?|[:])?\s*'
    # senza "n." o ":" il valore deve iniziare con una cifra ("protocollo FOLFOX-4" resta)
    r'(?P<v>(?=[A-Za-z0-9\/\-.]*\d)(?![0-9]{1,2}[\/.][0-9]{1,2}[\/.][0-9]{2,4}\b)(?(mk)[A-Za-z0-9]|\d)[A-Za-z0-9\/\-.]{2,}[A-Za-z0-9])',
]

# 5-bis. Documenti di identita' (carta d'identita', patente, passaporto, permesso)
DOCUMENT_PATTERNS = [
    r"(?i)\b(?:carta\s*d['’]?\s*identit[àa]|C\.\s?I\.|CIE|documento(?:\s*di\s*riconoscimento|\s*d['’]?identit[àa])?|"
    r"doc\.\s*identit[àa]|patente(?:\s*di\s*guida)?|passaporto|passport|permesso\s*di\s*soggiorno|"
    r"p\.d\.s\.|carta\s*di\s*soggiorno|titolo\s*di\s*soggiorno)"
    r"[^\n\d]{0,25}?(?:n(?:um(?:ero)?)?\.?|nr\.?|n°|nº)?\s*[:.]?\s*"
    r"(?P<v>(?=[A-Za-z0-9 \-]*\d)[A-Za-z0-9]{1,3}[ \-]?[A-Za-z0-9]{3,10}(?:[ \-]?[A-Za-z0-9]{1,4})?)\b",
    r'\b[A-Z]{2}\d{5}[A-Z]{2}\b',          # CIE: CA12345AB
    r'\b(?:[A-Z]{2}\d{7}|[A-Z]\d{8})\b(?=[^\n]{0,40}(?i:passaport|patente|document|rilasciat))',
    r'\b[A-Z]{1,2}\d{7}[A-Z]\b',           # patente: U1A2345678? (formati vari)
]

# 6. Provenienza / nazionalita' / reparto di origine
ORIGIN_PATTERNS = [
    r'(?i)\b(?:Provenienza|Proveniente\s*da|Reparto\s*di\s*provenienza|Prov\.?|Origin|Department\s*of\s*origin)[:\s]+(?P<v>[^\n\r,;\t]{2,})',
    r'(?i)\b(?:Nazionalit[àa]|Nationality|Nationalité|Cittadinanza|Citizenship)[:\s]+(?P<v>[^\n\r,;\t]{2,})',
]

# 7. Medici refertatori e firme
DOCTOR_PATTERNS = [
    r'\b(?i:Dott\.ssa|Dott\.?|Dr\.ssa|Dr\.?|Prof\.ssa|Prof\.?|Dott\.re|Inf\.)\s+(?P<v>' + _CAP_NAME + r')',
    r'(?i)\bFirmato\s*(?:digitalmente)?\s*da[:\s]+(?P<v>[^\n\r,;\t]{3,})',
    # "Documento firmato digitalmente ai sensi del D.Lgs. 82/2005 da: Mario Rossi"
    r'(?i)\bfirmat[oa]\s+(?:digitalmente|elettronicamente|con\s+firma\s+digitale)[^\n]{0,90}?\b(?:da|dal|dalla|dal\s+dott\.?|dalla\s+dott\.ssa)[:\s]+(?P<v>[^\n\r,;\t(]{3,})',
    # "Firmato digitalmente" e il nome sulla riga seguente
    r'(?i)\bfirmat[oa]\s+digitalmente\s*[:\-]?\s*\n\s*(?:(?:dott\.ssa|dott\.?|dr\.?|prof\.?)\s+)?(?P<v>' + _CAP_NAME_CS + r')',
    r'(?i)\b(?:Firma(?:\s*digitale)?|Firmatario|Sottoscritto\s*da|Certificato\s*(?:rilasciato\s*)?a|Titolare(?:\s*del\s*certificato)?)\s*(?:di|del|della)?\s*[:]\s*(?P<v>[^\n\r,;\t]{3,})',
    r'\b(?:CN|cn|GN|SN|givenName|surname|commonName)\s*=\s*(?P<v>[^,;/\n=]{2,60})',
    r'\b(?:SERIALNUMBER|serialNumber|2\.5\.4\.5)\s*=\s*(?P<v>[A-Z:\-]*[A-Z0-9]{6,})',
    r'(?i)\bSigned\s*by[:\s]+(?P<v>[^\n\r,;\t]{3,})',
    r'(?i)\b(?:Medico|Physician|Doctor|Esaminatore|Refertatore|Dirigente\s*Medico|Médecin|Médico)[:\s]+(?P<v>[^\n\r,;\t]{3,})',
]

# 8. Personale sanitario e destinatari
PERSONNEL_PATTERNS = [
    r'(?i)\b(?:Richiedente|Richiesto\s*da|Validato\s*da|Compilato\s*da|Eseguito\s*da|Operatore|Utente|Infermier[aeio]|Ostetrica|Fisioterapista|Logopedista|Tecnico(?:\s*di\s*radiologia)?|TSRM|OSS|Coordinat(?:ore|rice)|Caposala|Assistente\s*sociale|Psicolog[oa]|Dietista|Supervisore|Medico\s*Curante|Spett\.le|Spettabile|Alla\s*c\.a\.|C\.a\.|Operator|Requested\s*by|Validated\s*by)[:\s]+(?P<v>[^\n\r,;\t]{2,})',
]

# 9. Strutture sanitarie e branding istituzionale
HOSPITAL_KEYWORDS = [
    "Ospedale Pediatrico Bambino Gesù",
    "Bambino Gesù",
    "BAMBINO GESU",
    "OPBG",
    "IRCCS Ospedale Pediatrico Bambino Gesù",
    "Ospedale Pediatrico Bambino Gesu",
    "Bambino Gesu",
    "Azienda Ospedaliera",
    "Azienda Sanitaria",
    "Policlinico",
    "Ospedale",
    "Hospital",
    "Hopital",
    "Krankenhaus",
    "Medical Center",
    "Centre Hospitalier",
]
HOSPITAL_PATTERNS = [
    r'(?i)\b(?:Ospedale|Hospital|Hopital|Krankenhaus|Policlinico|IRCCS|Azienda\s*Ospedaliera|Azienda\s*Sanitaria|ASL|AUSL|USL|Centre\s*Hospitalier|Medical\s*Center|Fondazione)\s+[A-Za-zÀ-ÖØ-öø-ÿ\']+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ\']+)*',
]

# 10. Date (tutte: di ricovero, esame, controllo)
DATE_PATTERNS = [
    r'(?<![\d/.,])\d{1,2}[\/\-\.]\d{1,2}[\/\-\.](?:19|20)?\d{2}(?![\d/]|\.\d)',  # 12/03/2024, 12.03.24
    r'(?i)\b\d{1,2}°?\s+' + _MONTHS_IT + r'\s+\d{2,4}\b',          # 12 marzo 1985
    r'(?i)\b' + _MONTHS_IT + r'\s+(?:19|20)\d{2}\b',                 # marzo 2024
    r'(?i)\b' + _MONTHS_EN + r'\s+\d{1,2},?\s+\d{4}\b',            # March 12, 1985
    r'\b(?:19|20)\d{2}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b',  # data ISO
]

# Categoria -> livello minimo di sensibilita' che la attiva.
# Nessuna scala di sensibilita': ogni categoria e' sempre attiva. Il dizionario
# resta per compatibilita' con il codice che lo consulta.
CATEGORY_LEVELS: Dict[str, int] = {
    "Personalizzati": 1,
    "ID ufficiali": 1,
    "Nome paziente": 1,
    "Ricorrenze nominativo": 1,
    "Data di nascita": 1,
    "Contatti": 1,
    "Dati bancari": 1,
    "Identificativi clinici": 1,
    "Provenienza": 1,
    "Medici": 1,
    "Personale sanitario": 1,
    "Struttura sanitaria": 1,
    "Date": 1,
    "Documenti": 1,
    "Persone": 1,
}

_PATTERN_GROUPS: List[Tuple[str, List[str], int]] = [
    ("ID ufficiali", OFFICIAL_ID_PATTERNS, 2),
    ("Nome paziente", PATIENT_LABEL_PATTERNS, 2),
    ("Persone", PERSON_CONTEXT_PATTERNS, 2),
    ("Data di nascita", DOB_PATTERNS, 2),
    ("Contatti", CONTACT_PATTERNS, 2),
    ("Dati bancari", FINANCIAL_PATTERNS, 8),
    ("Identificativi clinici", RECORD_PATTERNS, 2),
    ("Provenienza", ORIGIN_PATTERNS, 2),
    ("Medici", DOCTOR_PATTERNS, 3),
    ("Personale sanitario", PERSONNEL_PATTERNS, 2),
    ("Struttura sanitaria", HOSPITAL_PATTERNS, 3),
    ("Date", DATE_PATTERNS, 4),
    ("Documenti", DOCUMENT_PATTERNS, 5),
]

LEVEL_DESCRIPTIONS: Dict[int, str] = {
    1: "Solo liste personalizzate e ID ufficiali (Codice Fiscale, passaporto, tessera sanitaria).",
    2: "Aggiunge nome e cognome del paziente (tutte le lingue) e ogni loro ricorrenza nel documento.",
    3: "Aggiunge date di nascita, eta' e dati anagrafici.",
    4: "Aggiunge telefoni, email, indirizzi di residenza e coordinate bancarie.",
    5: "Aggiunge numeri di cartella clinica, nosologici e ID di accettazione.",
    6: "Aggiunge provenienza, nazionalita' e reparti di origine.",
    7: "Aggiunge medici refertatori, esaminatori e dirigenti medici.",
    8: "Aggiunge operatori, infermieri e personale sanitario.",
    9: "Aggiunge nomi di ospedali, IRCCS, ASL e strutture sanitarie.",
    10: "Scansione massima: intestazioni, pie' di pagina, date e recapiti isolati.",
}

# Colore evidenziazione usato dall'anteprima per ogni categoria (RGB 0-255).
CATEGORY_COLORS: Dict[str, Tuple[int, int, int]] = {
    "Personalizzati": (220, 38, 38),
    "ID ufficiali": (217, 70, 239),
    "Nome paziente": (239, 68, 68),
    "Ricorrenze nominativo": (225, 29, 72),
    "Data di nascita": (249, 115, 22),
    "Contatti": (14, 165, 233),
    "Dati bancari": (6, 182, 212),
    "Identificativi clinici": (168, 85, 247),
    "Provenienza": (234, 179, 8),
    "Medici": (34, 197, 94),
    "Personale sanitario": (16, 185, 129),
    "Struttura sanitaria": (100, 116, 139),
    "Date": (120, 113, 108),
    "Documenti": (192, 38, 211),
    "Persone": (244, 63, 94),
    "Immagini e loghi": (148, 163, 184),
}

# Compilazione anticipata: evita di ricompilare i pattern per ogni pagina.
_COMPILED_GROUPS: List[Tuple[str, List[re.Pattern], int]] = [
    (name, [re.compile(p) for p in pats], min_len)
    for name, pats, min_len in _PATTERN_GROUPS
]

_STRIP_CHARS = " .,;:()[]{}\"'«»<>!/?* "

# Riga composta solo da 2-4 nomi propri (usata dalla scansione profonda).
_PROPER_NAME_LINE = re.compile(
    r'^(?:[A-ZÀ-ÖØ-ß\'][A-Za-zÀ-ÖØ-öø-ÿ\']+\s*){2,4}$'
)


# --------------------------------------------------------------------------
# Strutture dati
# --------------------------------------------------------------------------
@dataclass
class Detection:
    """Una singola corrispondenza individuata su una pagina."""
    text: str
    category: str
    page_index: int
    rects: List["fitz.Rect"] = field(default_factory=list)

    @property
    def color(self) -> Tuple[int, int, int]:
        return CATEGORY_COLORS.get(self.category, (239, 68, 68))


@dataclass
class FileResult:
    """Esito dell'elaborazione di un singolo PDF."""
    input_path: Path
    output_path: Optional[Path] = None
    redactions: int = 0
    pages: int = 0
    pages_without_text: List[int] = field(default_factory=list)
    per_category: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    skipped: bool = False
    # nominativi individuati nel documento: restano in memoria, servono a
    # ripulire il nome del file di output
    detected_names: List[str] = field(default_factory=list, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


@dataclass
class BatchResult:
    """Esito complessivo di un lotto."""
    results: List[FileResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def processed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> List[FileResult]:
        return [r for r in self.results if r.error]

    @property
    def total_redactions(self) -> int:
        return sum(r.redactions for r in self.results)

    @property
    def warnings(self) -> List[Tuple[Path, str]]:
        out: List[Tuple[Path, str]] = []
        for r in self.results:
            for w in r.warnings:
                out.append((r.input_path, w))
        return out


# --------------------------------------------------------------------------
# Utility di parsing degli elenchi utente
# --------------------------------------------------------------------------
def parse_text_lines(raw_text: str) -> List[str]:
    """
    Espande l'input utente in bersagli di ricerca distinti.

    Ogni riga viene mantenuta intera e, se composta da piu' parole, viene
    scomposta anche nelle singole parole e nelle coppie contigue: cosi'
    "MARIO ROSSI" oscura anche "MARIO" e "ROSSI" presi singolarmente.
    Usato per la lista "Rimuovi sempre".
    """
    targets = set()
    for line in raw_text.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        targets.add(line_str)
        words = line_str.split()
        if len(words) > 1:
            for w in words:
                w_clean = w.strip()
                if len(w_clean) >= 3 and not w_clean.isdigit():
                    targets.add(w_clean)
            for i in range(len(words) - 1):
                targets.add(f"{words[i]} {words[i+1]}")
    # I bersagli piu' lunghi vanno provati per primi.
    return sorted(targets, key=len, reverse=True)


def parse_lines_exact(raw_text: str) -> List[str]:
    """
    Restituisce le righe non vuote senza scomporle.

    Usato per la whitelist: scomporre "Ospedale Pediatrico" in singole parole
    proteggerebbe anche occorrenze non volute di "Ospedale".
    """
    seen = []
    for line in raw_text.splitlines():
        s = line.strip()
        if s and s not in seen:
            seen.append(s)
    return sorted(seen, key=len, reverse=True)


# --------------------------------------------------------------------------
# Indicizzazione del testo di pagina
# --------------------------------------------------------------------------
@dataclass
class _WordSpan:
    start: int
    end: int
    rect: "fitz.Rect"
    line_key: Tuple[int, int]


class _PageIndex:
    """
    Ricostruisce il testo della pagina a partire dalle singole parole e
    mantiene la corrispondenza carattere -> rettangolo.
    """

    def __init__(self, page: "fitz.Page", words: Optional[list] = None):
        self.spans: List[_WordSpan] = []
        parts: List[str] = []
        pos = 0
        if words is None:  # parole gia' lette dall'OCR, altrimenti il testo nativo
            try:
                words = page.get_text("words", sort=True)
            except Exception:
                words = page.get_text("words")

        lines: Dict[Tuple[int, int], List[tuple]] = {}
        order: List[Tuple[int, int]] = []
        for w in words:
            key = (w[5], w[6])
            if key not in lines:
                lines[key] = []
                order.append(key)
            lines[key].append(w)

        for i, key in enumerate(order):
            if i > 0:
                parts.append("\n")
                pos += 1
            for j, w in enumerate(lines[key]):
                if j > 0:
                    parts.append(" ")
                    pos += 1
                text = w[4]
                self.spans.append(
                    _WordSpan(pos, pos + len(text), fitz.Rect(w[0], w[1], w[2], w[3]), key)
                )
                parts.append(text)
                pos += len(text)

        self.text: str = "".join(parts)

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    def words_in(self, start: int, end: int) -> List[_WordSpan]:
        """Parole che si sovrappongono all'intervallo di caratteri indicato."""
        return [s for s in self.spans if s.start < end and s.end > start]

    def rects_for(self, start: int, end: int, protected: Sequence[Tuple[int, int]] = ()) -> List["fitz.Rect"]:
        """
        Rettangoli che coprono l'intervallo, raggruppati per riga.
        Le parole che ricadono in un intervallo protetto vengono escluse.
        """
        selected = []
        for s in self.words_in(start, end):
            if any(s.start < pe and s.end > ps for ps, pe in protected):
                continue
            selected.append(s)
        if not selected:
            return []

        rects: List[fitz.Rect] = []
        current: Optional[fitz.Rect] = None
        current_key: Optional[Tuple[int, int]] = None
        prev_end: Optional[int] = None
        for s in selected:
            contiguous = (
                current is not None
                and s.line_key == current_key
                and prev_end is not None
                and s.start - prev_end <= 1
            )
            if contiguous:
                current |= s.rect  # type: ignore[operator]
            else:
                if current is not None:
                    rects.append(current)
                current = fitz.Rect(s.rect)
                current_key = s.line_key
            prev_end = s.end
        if current is not None:
            rects.append(current)
        return rects

    def line_bounds(self) -> List[Tuple[int, int, "fitz.Rect", str]]:
        """Elenco di (start, end, rect, testo) per ogni riga della pagina."""
        out: List[Tuple[int, int, fitz.Rect, str]] = []
        current_key = None
        buf: List[_WordSpan] = []
        for s in self.spans:
            if current_key is None or s.line_key == current_key:
                buf.append(s)
                current_key = s.line_key
                continue
            out.append(self._pack_line(buf))
            buf = [s]
            current_key = s.line_key
        if buf:
            out.append(self._pack_line(buf))
        return out

    def _pack_line(self, buf: List[_WordSpan]) -> Tuple[int, int, "fitz.Rect", str]:
        rect = fitz.Rect(buf[0].rect)
        for s in buf[1:]:
            rect |= s.rect
        return buf[0].start, buf[-1].end, rect, self.text[buf[0].start:buf[-1].end]


def _protected_spans(text: str, whitelist: Sequence[str]) -> List[Tuple[int, int]]:
    """Intervalli di caratteri da non oscurare mai (whitelist)."""
    spans: List[Tuple[int, int]] = []
    for term in whitelist:
        term = term.strip()
        if not term:
            continue
        pattern = re.escape(term)
        if term[0].isalnum():
            pattern = r'\b' + pattern
        if term[-1].isalnum():
            pattern = pattern + r'\b'
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _value_bounds(text: str, start: int, end: int, max_len: int = 90) -> Tuple[int, int]:
    """
    Normalizza l'intervallo del valore di una coppia etichetta-valore.

    Se l'etichetta chiude la riga ed il valore prosegue su quella successiva
    (impaginazione tipica dei referti a colonne) l'intervallo viene spostato
    sulla riga sotto; in ogni caso il valore si ferma a fine riga e non supera
    `max_len` caratteri, per evitare che un pattern goloso inghiotta l'intero
    paragrafo.
    """
    seg = text[start:end]
    lead = len(seg) - len(seg.lstrip())
    if "\n" in seg[:lead]:
        start = start + seg.index("\n") + 1
        while start < end and text[start] in " \t":
            start += 1
    nl = text.find("\n", start, end)
    if nl != -1:
        end = nl
    if end - start > max_len:
        end = start + max_len
    return start, max(start, end)


def _iban_ok(raw: str) -> bool:
    """Controllo mod-97 (ISO 13616) dell'IBAN."""
    iban = re.sub(r'\s', '', raw).upper()
    if not (15 <= len(iban) <= 34) or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    moved = iban[4:] + iban[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


_CF_ODD = dict(zip("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                   [1, 0, 5, 7, 9, 13, 15, 17, 19, 21] +
                   [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16,
                    10, 22, 25, 24, 23]))


def cf_ok(cf: str) -> bool:
    """Carattere di controllo del codice fiscale (valido anche per l'omocodia)."""
    cf = cf.upper()
    if len(cf) != 16 or not cf.isalnum():
        return False
    total = 0
    for i, ch in enumerate(cf[:15]):
        if i % 2 == 0:
            total += _CF_ODD[ch]
        else:
            total += int(ch) if ch.isdigit() else ord(ch) - 65
    return chr(65 + total % 26) == cf[15]


def piva_ok(piva: str) -> bool:
    """Controllo della partita IVA italiana (11 cifre)."""
    if len(piva) != 11 or not piva.isdigit() or piva == "0" * 11:
        return False
    total = 0
    for i, ch in enumerate(piva[:10]):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(piva[10])


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        if not ch.isdigit():
            return False
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# --------------------------------------------------------------------------
# Analisi del testo (indipendente dal formato)
# --------------------------------------------------------------------------
@dataclass
class TextMatch:
    """Corrispondenza individuata su una porzione di testo."""
    start: int
    end: int
    category: str
    text: str = ""


def find_matches(
    text: str,
    sensitivity_level: int = 5,
    explicit_targets: Optional[Sequence[str]] = None,
    keep_labels: bool = False,
    derived_targets: Optional[Sequence[str]] = None,
) -> List[TextMatch]:
    """
    Individua i dati sensibili in una stringa qualsiasi.

    E' il cuore condiviso del motore: la analisi dei PDF vi aggiunge la
    mappatura su rettangoli di pagina, quella dei fogli di calcolo la applica
    cella per cella. La whitelist non viene applicata qui: spetta al chiamante,
    che conosce la geometria dei propri dati.
    """
    if not text:
        return []
    level = max(MIN_LEVEL, min(int(sensitivity_level), MAX_LEVEL))
    matches: List[TextMatch] = []
    seen: set = set()

    def add(start: int, end: int, category: str, min_len: int = 2):
        # per eccesso: uno span che taglia una parola si allarga alla parola intera
        while start > 0 and text[start - 1].isalnum() and text[start].isalnum():
            start -= 1
        while end < len(text) and text[end].isalnum() and text[end - 1].isalnum():
            end += 1
        cleaned = text[start:end].strip()
        if len(cleaned.strip(_STRIP_CHARS)) < min_len:
            return
        key = (category, start, end)
        if key in seen:
            return
        seen.add(key)
        matches.append(TextMatch(start, end, category, " ".join(cleaned.split())))

    # --- Bersagli espliciti dell'utente (sempre attivi) -------------------
    for target in (explicit_targets or []):
        target = target.strip()
        if len(target) < 2:
            continue
        pattern = re.escape(target)
        if target[0].isalnum():
            pattern = r'\b' + pattern
        if target[-1].isalnum():
            pattern = pattern + r'\b'
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        for m in rx.finditer(text):
            add(m.start(), m.end(), "Personalizzati", min_len=1)

    # --- Nominativi gia' individuati altrove nel documento ---------------
    if derived_targets and level >= CATEGORY_LEVELS["Ricorrenze nominativo"]:
        for rx in _target_regexes(derived_targets, case_sensitive_short=True):
            for m in rx.finditer(text):
                add(m.start(), m.end(), "Ricorrenze nominativo", min_len=2)

    # --- Pattern per categoria -------------------------------------------
    for category, patterns, min_len in _COMPILED_GROUPS:
        if level < CATEGORY_LEVELS.get(category, MAX_LEVEL):
            continue
        for rx in patterns:
            for m in rx.finditer(text):
                has_value = "v" in (rx.groupindex or {}) and m.group("v") is not None
                if has_value:
                    v_start, v_end = _value_bounds(text, *m.span("v"))
                    if v_end <= v_start:
                        continue
                    # Senza `keep_labels` viene oscurata anche l'etichetta: lo
                    # span parte dall'inizio della corrispondenza e puo' quindi
                    # attraversare l'a capo, coprendo entrambe le righe.
                    start, end = (v_start if keep_labels else m.start()), v_end
                else:
                    start, end = m.span()
                    nl = text.find("\n", start, end)
                    if nl != -1:
                        end = nl
                if category in ("Nome paziente", "Persone", "Medici", "Personale sanitario") and has_value:
                    end = _trim_name_end(text, v_start, end)
                    if end <= v_start:
                        continue
                elif has_value:
                    # "Nato il 10/12/1985  CF: RSS..." -> il valore si ferma
                    # all'etichetta successiva, che viene trattata a parte
                    nxt = (_NEXT_LABEL.search(text, v_start + 1, end)
                           or _SENTENCE_END.search(text, v_start + 1, end))
                    if nxt:
                        end = len(text[:nxt.start()].rstrip(" \t,;-"))
                        if end <= v_start:
                            continue
                raw = m.group(0)
                if has_value and category in ("Data di nascita", "Identificativi clinici", "ID ufficiali"):
                    # date e codici contengono cifre: "Codice fiscale  Data di nascita" in
                    # un'intestazione di tabella non e' un valore (il luogo di nascita si')
                    label = text[m.start():v_start].lower()
                    if not any(ch.isdigit() for ch in text[v_start:end]) and not (
                            category == "Data di nascita" and re.search(r"luogo|nat[oa]|comune|born", label)):
                        continue
                if category == "ID ufficiali" and not has_value and len(raw) == 16:
                    # CF canonico: oscurato comunque; forma omocodica: solo se il
                    # carattere di controllo torna (evita falsi positivi)
                    if not raw[6:8].isdigit() or not raw[9:11].isdigit() or not raw[12:15].isdigit():
                        if not cf_ok(raw):
                            continue
                if category == "Dati bancari" and not has_value:
                    compact = re.sub(r"[\s\-.]", "", raw)
                    if compact.isdigit():
                        ok = piva_ok(compact) if len(compact) == 11 else (
                            13 <= len(compact) <= 19 and _luhn_ok(compact))
                    else:
                        ok = _iban_ok(raw)
                    if not ok:
                        continue  # sequenza plausibile ma con controllo non valido
                if category == "Nome paziente" and _KIN_BEFORE.search(text, max(0, m.start() - 40), m.start()):
                    add(start, end, "Persone", min_len)  # "la moglie, sig.ra Bianchi": familiare
                    continue
                add(start, end, category, min_len)

    # --- Modello linguistico locale: nomi e indirizzi senza etichetta -----
    for value, kind in clin_llm.entities(text):
        category = "Persone" if kind == "PERSONA" else "Contatti"
        for rx in _target_regexes([value], case_sensitive_short=True):
            for m in rx.finditer(text):
                if category == "Persone" and clin_llm.is_eponym_at(text, m.start()):
                    continue  # "manovra di Giordano": termine clinico, non persona
                add(m.start(), m.end(), category, 2)

    # --- Parole chiave letterali delle strutture sanitarie ---------------
    if level >= CATEGORY_LEVELS["Struttura sanitaria"]:
        for kw in HOSPITAL_KEYWORDS:
            for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
                add(m.start(), m.end(), "Struttura sanitaria", 3)

    return matches


# --------------------------------------------------------------------------
# Propagazione dei nominativi
# --------------------------------------------------------------------------
# Parole che compaiono spesso dopo un'etichetta di nome ma non sono nomi:
# interrompono il valore ("ROSSI MARIO Data di nascita ...").
_NAME_STOPWORDS = {
    w.lower() for w in """
    data nascita nato nata nati sesso eta età anni anno mesi codice fiscale cf
    paziente pz nome cognome nominativo assistito via viale piazza residenza
    residente domicilio indirizzo tel telefono cell cellulare email mail reparto
    referto esame diagnosi ricovero dimissione dimesso dimessa letto stanza
    ospedale clinica policlinico medico dott dottore dottoressa prof sig sigra
    maschio femmina male female sex dob birth date age patient name surname
    first last id cartella nosologico accettazione tessera sanitaria provenienza
    nazionalita nazionalità cittadinanza comune citta città provincia prov cap
    dei degli delle il lo gli un una per con su
    and the of y e o ed ha ho è sono
    richiesta richiedente prestazione esito note anamnesi terapia oggetto
    numero n nr pag pagina firma firmato timbro uo u.o reparto ambulatorio
    """.split()
}

_NAME_SOURCES = [re.compile(p) for p in PATIENT_LABEL_PATTERNS]
# fonti in cui il contesto basta a dire che il valore e' un nome di persona
_STRONG_NAME_SOURCES = [re.compile(p) for p in PERSON_CONTEXT_PATTERNS + DOCTOR_PATTERNS]
# ruoli che seguono spesso il nome nelle firme ("Ivan Petrov - Dirigente Medico")
_ROLE_WORDS = {"dirigente", "direttore", "direttrice", "responsabile", "primario", "specialista",
               "specializzando", "specializzanda", "uoc", "uos", "uosd", "u.o.c", "coordinatore",
               "coordinatrice", "infermiere", "infermiera", "tecnico", "medico", "chirurgo", "in", "presso",
               # reparti e servizi: "Dirigente Medico UOC Geriatria", "Richiedente: Reparto di ..."
               "reparto", "servizio", "dipartimento", "distretto", "unita", "unità", "ambulatorio", "pronto",
               "soccorso", "laboratorio", "struttura", "divisione", "sezione", "centro", "day", "hospital",
               "cardiologia", "geriatria", "medicina", "chirurgia", "pediatria", "neurologia", "ortopedia",
               "oncologia", "ematologia", "nefrologia", "pneumologia", "urologia", "ginecologia", "ostetricia",
               "radiologia", "anestesia", "rianimazione", "psichiatria", "dermatologia", "oculistica",
               "otorinolaringoiatria", "gastroenterologia", "endocrinologia", "reumatologia", "neonatologia",
               "infettivologia", "diabetologia", "fisiatria", "riabilitazione", "endoscopia", "dialisi"}
_TITLE_TOKENS = {"dott", "ssa", "dr", "prof", "sig", "ra", "na", "signor", "signora", "inf", "dottor", "dottoressa"}
_NAME_TOKEN = re.compile(r"[" + _LETTER + r"'’]+")
_NAME_PARTICLES = {"lo", "la", "li", "le", "de", "di", "da", "du", "del", "dei", "della", "dello", "dela",
                   "van", "von", "der", "el", "al", "bin", "ben", "dos", "das"}


def collect_names(text: str) -> List[str]:
    """
    Estrae da un testo i nominativi del paziente che compaiono dopo
    un'etichetta esplicita ("Paziente: ROSSI MARIO", "Cognome: Rossi",
    "Sig.ra Bianchi"). Restituisce il nominativo completo e le singole parole,
    cosi' che ogni ricorrenza successiva nel documento venga oscurata anche
    senza etichetta.
    """
    found: List[str] = []
    for value, kind in clin_llm.entities(text):
        if kind == "PERSONA":
            found.append(value)
            found.extend(t for t in value.split() if len(t) >= 3)
    sources = [(rx, False) for rx in _NAME_SOURCES] + [(rx, True) for rx in _STRONG_NAME_SOURCES]
    for rx, strong in sources:
        for m in rx.finditer(text):
            if "v" not in rx.groupindex or m.group("v") is None:
                continue
            label = text[m.start():m.start("v")]
            v_start, v_end = _value_bounds(text, *m.span("v"), max_len=60)
            value = text[v_start:v_end]
            # Etichette "deboli" (Nome, Cognome) in prosa: si accettano solo se
            # seguite da due punti o se il valore e' gia' formato da maiuscole.
            tokens: List[str] = []
            for tok in _NAME_TOKEN.findall(value):
                if not tokens and tok.lower() in _TITLE_TOKENS:
                    continue  # "Dott. Paolo Neri": il titolo non fa parte del nome
                if tok.lower() in _NAME_PARTICLES and tok[0].isupper() and tokens:
                    tokens.append(tok)  # "LO IACONO", "Dela Cruz"
                    continue
                if tok.lower().strip("'’") in _NAME_STOPWORDS or tok.lower() in _ROLE_WORDS:
                    break
                if not tok[0].isupper():
                    break
                tokens.append(tok)
                if len(tokens) == 4:
                    break
            if not tokens:
                continue
            if not strong and ":" not in label and not label.strip().lower().startswith(
                ("sig", "mr", "mrs", "ms", "mme", "herr", "frau", "il ", "la ")
            ) and not all(t.isupper() for t in tokens):
                continue
            full = " ".join(tokens)
            if len(full) >= 3:
                found.append(full)
            for t in tokens:
                if len(t) >= 3:
                    found.append(t)
    # deduplica preservando l'ordine, piu' lunghi prima
    uniq = list(dict.fromkeys(found))
    return sorted(uniq, key=len, reverse=True)


def collect_document_names(
    doc: "fitz.Document",
    sensitivity_level: int = 5,
    whitelist_words: Optional[Sequence[str]] = None,
    ocr_pages: Optional[Dict[int, list]] = None,
) -> List[str]:
    """Raccoglie i nominativi del paziente da tutte le pagine del documento."""
    if int(sensitivity_level) < CATEGORY_LEVELS["Ricorrenze nominativo"]:
        return []
    protected = {w.strip().lower() for w in (whitelist_words or []) if w.strip()}
    names: List[str] = []
    for page in doc:
        try:
            words = (ocr_pages or {}).get(page.number)
            names.extend(collect_names(_PageIndex(page, words).text))
        except Exception:
            continue
    uniq = [n for n in dict.fromkeys(names) if n.lower() not in protected]
    return sorted(uniq, key=len, reverse=True)


_NEXT_LABEL = re.compile(
    r"(?i)\s(?:C\.?\s?F\.?|Cod(?:ice)?\.?\s*Fiscale|Sesso|Tel(?:efono)?\.?|Cell(?:ulare)?\.?|"
    r"E-?mail|Residen\w*|Indirizzo|Domicilio|Et[àa]|Cartella|Nosologico|Reparto|"
    r"Data\s+(?:di\s+)?(?:ricovero|dimissione|esame|nascita)|Tessera|Paziente)\b\s*[:.]?"
)


# fine frase: "01/02/1990. Verdi riferisce..." -> il valore si ferma alla data
_SENTENCE_END = re.compile(r"(?<=[^\s.])\.(?=\s+[A-ZÀ-ÖØ-Þ])")


def _trim_name_end(text: str, start: int, end: int) -> int:
    """Chiude il valore di un nominativo prima della prima parola-etichetta."""
    named = False
    for m in _NAME_TOKEN.finditer(text, start, end):
        tok = m.group(0)
        low = tok.lower().strip("'’")
        if not named and low in _TITLE_TOKENS:
            continue  # "Dr. Ivan Petrov": il titolo precede il nome
        if tok.lower() in _NAME_PARTICLES and tok[:1].isupper():
            continue
        if low in _NAME_STOPWORDS or low in _ROLE_WORDS:
            return len(text[:m.start()].rstrip(" \t,;-–")) if named else start
        named = True
    return end


def _target_regexes(targets: Sequence[str], case_sensitive_short: bool = False) -> List[re.Pattern]:
    """
    Compila i bersagli in espressioni a confine di parola. Con
    `case_sensitive_short` le parole brevi (<= 4 lettere, es. "Re", "Lia")
    devono comparire con l'iniziale maiuscola, per evitare falsi positivi.
    """
    out: List[re.Pattern] = []
    for target in targets:
        target = target.strip()
        if len(target) < 2:
            continue
        pattern = re.escape(target)
        if target[0].isalnum():
            pattern = r'\b' + pattern
        if target[-1].isalnum():
            pattern = pattern + r'\b'
        flags = re.IGNORECASE
        if case_sensitive_short and (len(target) <= 4 or " " not in target):
            # "Neri" (cognome) si', "capelli neri" no
            forms = dict.fromkeys([target, target[:1].upper() + target[1:].lower(), target.upper()])
            pattern = r"(?<![\w'’])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![\w'’])"
            flags = 0
        try:
            out.append(re.compile(pattern, flags))
        except re.error:
            continue
    return out


def protected_spans(text: str, whitelist: Sequence[str]) -> List[Tuple[int, int]]:
    """Intervalli di caratteri protetti dalla whitelist."""
    return _protected_spans(text, whitelist)


def subtract_protected(
    start: int, end: int, protected: Sequence[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    """
    Rimuove dall'intervallo le porzioni protette, restituendo i segmenti
    residui da oscurare.
    """
    segments = [(start, end)]
    for p_start, p_end in protected:
        updated: List[Tuple[int, int]] = []
        for s, e in segments:
            if p_end <= s or p_start >= e:
                updated.append((s, e))
                continue
            if p_start > s:
                updated.append((s, p_start))
            if p_end < e:
                updated.append((p_end, e))
        segments = updated
        if not segments:
            break
    return [(s, e) for s, e in segments if e > s]


# --------------------------------------------------------------------------
# Analisi di una pagina PDF
# --------------------------------------------------------------------------
def analyze_page(
    page: "fitz.Page",
    sensitivity_level: int = 5,
    explicit_targets: Optional[Sequence[str]] = None,
    whitelist_words: Optional[Sequence[str]] = None,
    keep_labels: bool = False,
    derived_targets: Optional[Sequence[str]] = None,
    words: Optional[list] = None,
) -> List[Detection]:
    """
    Individua (senza modificare nulla) tutti gli elementi sensibili di una
    pagina. Usata sia dal motore di redazione sia dall'anteprima grafica.
    `words` sostituisce il testo nativo (parole lette dall'OCR).
    """
    level = max(MIN_LEVEL, min(int(sensitivity_level), MAX_LEVEL))
    index = _PageIndex(page, words)
    detections: List[Detection] = []
    if not index.text:
        return detections

    protected = _protected_spans(index.text, whitelist_words or [])
    seen: set = set()
    page_no = page.number if page.number is not None else 0

    def add(start: int, end: int, category: str, min_len: int = 2):
        cleaned = index.text[start:end].strip()
        if len(cleaned.strip(_STRIP_CHARS)) < min_len:
            return
        key = (category, start, end)
        if key in seen:
            return
        rects = index.rects_for(start, end, protected)
        if not rects:
            return
        seen.add(key)
        detections.append(
            Detection(text=" ".join(cleaned.split()), category=category,
                      page_index=page_no, rects=rects)
        )

    for match in find_matches(index.text, level, explicit_targets, keep_labels,
                              derived_targets=derived_targets):
        add(match.start, match.end, match.category, 1)

    # --- Righe di sole parole maiuscole in intestazioni e pie' di pagina --
    rect = page.rect
    header_limit = max(240.0, rect.height * 0.22)
    footer_limit = rect.height * 0.88
    for start, end, line_rect, line_text in index.line_bounds():
        in_margin = line_rect.y1 <= header_limit or line_rect.y0 >= footer_limit
        if not in_margin:
            continue
        stripped = line_text.strip()
        if len(stripped) < 4:
            continue
        if _PROPER_NAME_LINE.match(stripped) and stripped not in HOSPITAL_KEYWORDS:
            add(start, end, "Persone", 4)

    return detections


def analyze_pdf_page(
    doc: "fitz.Document",
    page_index: int,
    **kwargs,
) -> List[Detection]:
    """Comodita': analizza una pagina di un documento gia' aperto."""
    if not doc or page_index < 0 or page_index >= len(doc):
        return []
    return analyze_page(doc.load_page(page_index), **kwargs)


# --------------------------------------------------------------------------
# Sanificazione del documento
# --------------------------------------------------------------------------
_SAFE_METADATA_KEYS = (
    "title", "author", "subject", "keywords", "creator", "producer",
    "creationDate", "modDate", "trapped", "format", "encryption",
)


def _sanitize_document(doc: "fitz.Document", warnings: List[str]) -> None:
    """
    Rimuove le informazioni identificative che non fanno parte del contenuto
    visibile: metadati, metadati XMP, segnalibri, allegati, JavaScript,
    annotazioni residue e link esterni.
    """
    # `scrub` copre in un colpo: JavaScript, campi modulo (valori azzerati),
    # testo invisibile (es. strati OCR non redatti), miniature, allegati,
    # metadati XMP. I passaggi successivi restano come rete di sicurezza.
    try:
        doc.scrub(redact_images=0)
    except Exception as exc:  # pragma: no cover - dipende dal PDF
        warnings.append(f"Pulizia profonda non completata: {exc}")

    try:
        doc.set_metadata({k: "" for k in _SAFE_METADATA_KEYS if k not in ("format", "encryption")})
    except Exception as exc:  # pragma: no cover - dipende dal PDF
        warnings.append(f"Metadati non ripuliti: {exc}")

    try:
        doc.del_xml_metadata()
    except Exception:
        pass

    try:
        doc.set_toc([])
    except Exception:
        pass

    try:
        for i in range(doc.embfile_count() - 1, -1, -1):
            doc.embfile_del(i)
    except Exception:
        pass

    for page in doc:
        try:
            for widget in list(page.widgets() or []):
                page.delete_widget(widget)
        except Exception:
            pass
        try:
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
        except Exception:
            pass
        try:
            for link in list(page.get_links() or []):
                page.delete_link(link)
        except Exception:
            pass


def _redact_offpage_text(page: "fitz.Page") -> bool:
    """
    Il testo posizionato fuori dall'area visibile non compare a video ne'
    nell'estrazione standard, ma resta nel file e si recupera con un
    qualunque estrattore. Viene eliminato con quattro fasce di redazione
    attorno alla pagina.
    """
    r = page.rect
    try:
        outside = [w for w in page.get_text("words", clip=fitz.INFINITE_RECT())
                   if not fitz.Rect(w[:4]).intersects(r)]
    except Exception:
        return False
    if not outside:
        return False
    big = 1e5
    for band in (fitz.Rect(-big, -big, big, r.y0), fitz.Rect(-big, r.y1, big, big),
                 fitz.Rect(-big, r.y0, r.x0, r.y1), fitz.Rect(r.x1, r.y0, big, r.y1)):
        page.add_redact_annot(band, fill=None)
    return True


def _apply_redactions(page: "fitz.Page", remove_images: bool) -> None:
    """Applica le redazioni gestendo le differenze fra versioni di PyMuPDF."""
    image_mode = getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", None) if remove_images else None
    try:
        if image_mode is not None:
            page.apply_redactions(images=image_mode)
        else:
            page.apply_redactions()
    except TypeError:
        page.apply_redactions()


# --------------------------------------------------------------------------
# Anonimizzazione di un documento
# --------------------------------------------------------------------------
def anonymize_document(
    input_path: Path,
    output_path: Path,
    sensitivity_level: int = 5,
    remove_images: bool = True,
    explicit_targets: Optional[Sequence[str]] = None,
    whitelist_words: Optional[Sequence[str]] = None,
    fill_color: Tuple[float, float, float] = (0, 0, 0),
    keep_labels: bool = False,
    sanitize_metadata: bool = True,
    verify_output: bool = True,
    password: str = "",
    should_cancel: Optional[Callable[[], bool]] = None,
    propagate_names: bool = True,
    ocr: bool = True,
) -> FileResult:
    """
    Anonimizza un PDF (o un'immagine JPG/PNG, salvata come PDF) restituendo
    un `FileResult` con statistiche ed avvisi. Le pagine senza testo vengono
    lette con l'OCR, se il modello e' installato, e redatte sui pixel.
    Non solleva eccezioni: gli errori vengono riportati in `result.error`.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    result = FileResult(input_path=input_path, output_path=output_path)

    doc = None
    try:
        doc = clin_ocr.open_as_pdf(input_path)

        if doc.is_encrypted:
            if not doc.authenticate(password):
                result.error = "PDF protetto da password: impossibile elaborarlo."
                return result

        result.pages = len(doc)
        if result.pages == 0:
            result.error = "Il documento non contiene pagine."
            return result

        ocr_pages: Dict[int, list] = {}
        use_ocr = ocr and clin_ocr.available()
        for page in doc:
            if not page.get_text().strip():
                if not use_ocr:
                    result.pages_without_text.append(page.number + 1)
                    continue
                page.remove_rotation()  # coordinate OCR e immagine finale allineate
                ocr_pages[page.number] = clin_ocr.ocr_words(page)

        derived = (
            collect_document_names(doc, sensitivity_level, whitelist_words, ocr_pages)
            if propagate_names else []
        )

        for page in doc:
            if should_cancel and should_cancel():
                result.error = "Operazione annullata."
                return result

            if page.number in ocr_pages:
                # scansione: la pagina diventa un'immagine gia' coperta
                ocr_text = " ".join(w[4] for w in ocr_pages[page.number])
                detections = analyze_page(
                    page, sensitivity_level=sensitivity_level,
                    explicit_targets=list(explicit_targets or []) + clin_ocr.ocr_suspects(ocr_text),
                    whitelist_words=whitelist_words,
                    keep_labels=keep_labels, derived_targets=derived,
                    words=ocr_pages[page.number],
                )
                rects = [r for d in detections for r in d.rects
                         if not (r.is_empty or r.is_infinite)]
                for det in detections:
                    result.per_category[det.category] = result.per_category.get(det.category, 0) + 1
                result.redactions += len(rects)
                clin_ocr.replace_with_redacted_image(page, rects, fill_color)
                continue

            if remove_images:
                for info in page.get_image_info():
                    try:
                        page.add_redact_annot(fitz.Rect(info["bbox"]), fill=(1, 1, 1))
                        result.redactions += 1
                        result.per_category["Immagini e loghi"] = (
                            result.per_category.get("Immagini e loghi", 0) + 1
                        )
                    except Exception:
                        pass

            detections = analyze_page(
                page,
                sensitivity_level=sensitivity_level,
                explicit_targets=explicit_targets,
                whitelist_words=whitelist_words,
                keep_labels=keep_labels,
                derived_targets=derived,
            )

            for det in detections:
                for rect in det.rects:
                    if rect.is_empty or rect.is_infinite:
                        continue
                    page.add_redact_annot(rect, fill=fill_color)
                    result.redactions += 1
                result.per_category[det.category] = result.per_category.get(det.category, 0) + 1

            if _redact_offpage_text(page):
                result.warnings.append(
                    f"Pagina {page.number + 1}: rimosso testo nascosto fuori dall'area visibile."
                )

            _apply_redactions(page, remove_images)

        if sanitize_metadata:
            _sanitize_document(doc, result.warnings)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_path), garbage=4, deflate=True, clean=True)
        doc.close()
        doc = None

        if result.redactions == 0 and not result.pages_without_text:
            result.warnings.append(
                "Nessun dato rilevato: verificare il documento o aumentare la sensibilita'."
            )

        if ocr_pages:
            result.warnings.append(
                "Pagine lette con OCR e redatte sull'immagine: "
                f"{', '.join(str(p + 1) for p in sorted(ocr_pages))}. "
                "L'OCR puo' sbagliare: controllare visivamente."
            )

        if result.pages_without_text:
            pages_str = ", ".join(str(p) for p in result.pages_without_text[:8])
            more = "..." if len(result.pages_without_text) > 8 else ""
            result.warnings.append(
                f"Pagine senza testo selezionabile (probabile scansione), "
                f"non analizzabili automaticamente: {pages_str}{more}"
                + ("" if clin_ocr.available() or not ocr else
                   " (per leggerle installare l'OCR: python3 scripts/fetch_ocr_model.py)")
            )

        result.detected_names = list(derived)
        residual = _residual_check(output_path, sensitivity_level, explicit_targets,
                                   whitelist_words, derived)
        if residual:
            result.warnings.append(
                "Verifica finale: possibili dati sensibili ancora presenti "
                f"({'; '.join(residual[:5])}). Controllare manualmente il documento."
            )

        check_terms = list(explicit_targets or []) + [d for d in derived if len(d) >= 4]
        if verify_output and check_terms:
            leftovers = _verify_output(output_path, check_terms)
            if leftovers:
                result.warnings.append(
                    "Verifica finale: alcuni termini risultano ancora presenti nel testo "
                    f"({', '.join(leftovers[:5])}). Controllare manualmente il documento."
                )

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    return result


# Categorie ad alta affidabilita': se ricompaiono nell'output e' quasi certo
# che un dato sia sfuggito (le altre generano falsi allarmi sulle etichette).
_RESIDUAL_CATEGORIES = {"ID ufficiali", "Dati bancari", "Ricorrenze nominativo", "Personalizzati"}


def _residual_check(
    output_path: Path,
    level: int,
    explicit_targets: Optional[Sequence[str]],
    whitelist_words: Optional[Sequence[str]],
    derived: Sequence[str],
) -> List[str]:
    """
    Rianalizza il PDF prodotto, compreso il testo fuori dall'area visibile
    della pagina, e segnala i dati sensibili ancora leggibili. I valori non
    vengono riportati per esteso: solo pagina e categoria.
    """
    found: List[str] = []
    try:
        # i nomi trovati dal modello sono gia' in `derived`: nessuna nuova chiamata
        with clin_llm.cache_only(), fitz.open(str(output_path)) as doc:
            for page in doc:
                words = page.get_text("words", clip=fitz.INFINITE_RECT())
                if not words and clin_ocr.available():
                    words = clin_ocr.ocr_words(page)  # pagina redatta sui pixel
                text = "\n".join(" ".join(w[4] for w in line) for line in _group_lines(words))
                protected = _protected_spans(text, whitelist_words or [])
                for m in find_matches(text, level, explicit_targets, True,
                                      derived_targets=derived):
                    if m.category not in _RESIDUAL_CATEGORIES:
                        continue
                    if subtract_protected(m.start, m.end, protected):
                        found.append(f"pag. {page.number + 1}: {m.category}")
    except Exception:
        return []
    return list(dict.fromkeys(found))


def _group_lines(words: list) -> List[list]:
    lines: Dict[Tuple[int, int], list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)
    return list(lines.values())


def safe_output_stem(stem: str, level: int, names: Sequence[str] = (),
                     explicit_targets: Optional[Sequence[str]] = None) -> str:
    """
    Ripulisce il nome del file: "ROSSI_MARIO_referto_12-03-2024" diventa
    "ANON_ANON_referto_ANON". Il nome del file viaggia con il documento
    (email, chat, caricamenti) e spesso contiene il nominativo.
    """
    spaced = stem.replace("_", " ")  # 1:1, cosi' gli indici restano validi
    # date e ID nel nome file si riconoscono anche senza etichetta
    spans = [(m.start, m.end) for m in find_matches(
        spaced, max(level, 10), explicit_targets, True, derived_targets=names)
        if m.category in ("ID ufficiali", "Personalizzati", "Ricorrenze nominativo",
                          "Date", "Dati bancari", "Persone", "Documenti")]
    if not spans:
        return stem
    merged: List[Tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    chars = list(stem)
    for s, e in reversed(merged):
        chars[s:e] = list("ANON")
    return re.sub(r"(ANON[ _\-.]*){2,}", "ANON_", "".join(chars)).strip("_ -.") or "documento"


def _verify_output(output_path: Path, targets: Sequence[str]) -> List[str]:
    """Riapre l'output e verifica che i bersagli espliciti non siano piu' presenti."""
    leftovers: List[str] = []
    try:
        with fitz.open(str(output_path)) as doc:
            text = "\n".join(page.get_text() for page in doc)
        for t, rx in zip([t for t in targets if len(t.strip()) >= 2], _target_regexes(targets)):
            if len(t.strip()) >= 3 and rx.search(text):
                leftovers.append(t.strip())
    except Exception:
        return []
    return leftovers


def anonymize_single_pdf(
    input_path: str,
    output_path: str,
    sensitivity_level: int = 5,
    remove_images: bool = True,
    explicit_targets: Optional[List[str]] = None,
    whitelist_words: Optional[List[str]] = None,
    fill_color=(0, 0, 0),
    **kwargs,
) -> int:
    """
    Compatibilita' storica: anonimizza un file e restituisce il numero di
    redazioni applicate. Solleva `RuntimeError` in caso di errore.

    Tutte le categorie di dati sono sempre attive.
    """
    res = anonymize_document(
        Path(input_path),
        Path(output_path),
        sensitivity_level=sensitivity_level,
        remove_images=remove_images,
        explicit_targets=explicit_targets,
        whitelist_words=whitelist_words,
        fill_color=fill_color,
        **kwargs,
    )
    if res.error:
        raise RuntimeError(res.error)
    return res.redactions


# --------------------------------------------------------------------------
# Elaborazione in lotto
# --------------------------------------------------------------------------
def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return path


def process_batch_files(
    file_list: List[Path],
    output_dir: Path,
    sensitivity_level: int = 5,
    remove_images: bool = True,
    explicit_targets: Optional[List[str]] = None,
    whitelist_words: Optional[List[str]] = None,
    fill_color: Tuple[float, float, float] = (0, 0, 0),
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    keep_labels: bool = False,
    sanitize_metadata: bool = True,
    verify_output: bool = True,
    overwrite: bool = True,
    suffix: str = "_ANONIMIZZATO",
    should_cancel: Optional[Callable[[], bool]] = None,
    result_callback: Optional[Callable[[FileResult], None]] = None,
    batch_result: Optional[BatchResult] = None,
    table_settings=None,
    propagate_names: bool = True,
    anonymize_filenames: bool = True,
) -> Tuple[int, int]:
    """
    Elabora un elenco di documenti. Restituisce `(file_elaborati, redazioni)`.

    Riconosce i PDF e, se `table_settings` e' fornito, anche i fogli di calcolo
    (CSV, TSV, XLSX), instradandoli al motore tabellare.

    Un errore su un singolo file non interrompe il lotto: viene registrato nel
    relativo `FileResult` e segnalato tramite `result_callback`. Passando
    `batch_result` si ottiene l'elenco completo degli esiti.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    targets = [p for p in file_list if not p.stem.endswith(suffix)]
    total = len(targets)
    collected = batch_result if batch_result is not None else BatchResult()

    for idx, src_path in enumerate(targets, start=1):
        if should_cancel and should_cancel():
            collected.cancelled = True
            break

        ext = src_path.suffix.lower()
        out_ext = ".pdf" if ext in clin_ocr.IMAGE_SUFFIXES else src_path.suffix
        out_path = output_dir / f"{src_path.stem}{suffix}{out_ext}"
        if not overwrite:
            out_path = _unique_path(out_path)

        if out_path.resolve() == src_path.resolve():
            res = FileResult(input_path=src_path,
                             error="Il file di output coincide con l'input.")
        elif ext == ".pdf" or ext in clin_ocr.IMAGE_SUFFIXES:
            res = anonymize_document(
                src_path,
                out_path,
                sensitivity_level=sensitivity_level,
                remove_images=remove_images,
                explicit_targets=explicit_targets,
                whitelist_words=whitelist_words,
                fill_color=fill_color,
                keep_labels=keep_labels,
                sanitize_metadata=sanitize_metadata,
                verify_output=verify_output,
                should_cancel=should_cancel,
                propagate_names=propagate_names,
            )
        else:
            # Import ritardato: il motore tabellare importa a sua volta da qui.
            from tabular_engine import TABLE_SUFFIXES, anonymize_table

            if ext not in TABLE_SUFFIXES:
                res = FileResult(input_path=src_path,
                                 error=f"Formato non supportato: {ext}")
            elif table_settings is None:
                res = FileResult(input_path=src_path,
                                 error="Impostazioni tabellari mancanti.")
            else:
                res = anonymize_table(src_path, out_path, table_settings,
                                      should_cancel=should_cancel)

        if anonymize_filenames and res.ok and res.output_path and res.output_path.exists():
            clean = safe_output_stem(src_path.stem, sensitivity_level,
                                     res.detected_names, explicit_targets)
            if clean != src_path.stem:
                target = _unique_path(output_dir / f"{clean}{suffix}{out_ext}")
                try:
                    res.output_path.rename(target)
                    res.output_path = target
                except OSError as exc:
                    res.warnings.append(f"Nome del file non ripulito: {exc}")
        res.detected_names = []

        collected.results.append(res)
        if result_callback:
            result_callback(res)
        if progress_callback:
            progress_callback(idx, total, src_path.name)

    return collected.processed, collected.total_redactions
