# Clin – app per telefono

App web di **Clin – Clinical Data Anonymizer for AI**: anonimizza foto, PDF e
testi sanitari **sul telefono**, prima dell'invio a Claude, ChatGPT, Gemini o
un'altra IA.

Aprire **https://enricoarmiento.github.io/clin-app/** e aggiungerla alla
schermata Home.

- Tutta l'elaborazione avviene nel browser (Pyodide, Tesseract.js, pdf.js,
  ospitati qui e verificati con SHA-256).
- La Content-Security-Policy `connect-src 'self'` impedisce connessioni verso
  altri siti: i documenti non lasciano il telefono. Questo sito serve solo file
  statici e non riceve dati.
- Questo repository contiene solo l'app compilata, generata automaticamente.

Carattere Albert Sans: SIL Open Font License 1.1 (`fonts/OFL-AlbertSans.txt`).
Pyodide (MPL-2.0), Tesseract.js (Apache-2.0), pdf.js (Apache-2.0).
