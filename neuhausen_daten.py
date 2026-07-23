#!/usr/bin/env python3
"""
Holt die Vorstoss- und Berichtsseiten der Gemeinde Neuhausen am Rheinfall
und schreibt die aufbereiteten Daten als neuhausen_daten.js in denselben
Ordner. Die Datei neuhausen-monitor.html liest diese Daten automatisch.

Abhaengigkeiten:  pip install requests beautifulsoup4
Aufruf:           python3 neuhausen_daten.py
Automatisierung:  per Cron oder launchd regelmaessig ausfuehren.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _ZEITZONE = ZoneInfo("Europe/Zurich")
except Exception:
    _ZEITZONE = None

import requests
from bs4 import BeautifulSoup

BASIS = Path(__file__).resolve().parent
AUSGABE = BASIS / "neuhausen_daten.js"

VORSTOSS_QUELLEN = [
    {"art": "Kleine Anfrage", "kuerzel": "ka", "url": "https://neuhausen.ch/kleine_anfrage"},
    {"art": "Interpellation", "kuerzel": "ip", "url": "https://neuhausen.ch/interpellationen"},
    {"art": "Postulat",       "kuerzel": "po", "url": "https://neuhausen.ch/postulate"},
    {"art": "Motion",         "kuerzel": "mo", "url": "https://neuhausen.ch/motionen"},
]
BA_URL = "https://neuhausen.ch/berichte_antraege"
BP_URL = "https://neuhausen.ch/beschluesse_protokolle"
AKTUELLES_URL = "https://neuhausen.ch/aktuelles"
PRESSE_SHN_URL = "https://www.shn.ch/region/neuhausen"
PRESSE_SHAZ_FEED = "https://www.shaz.ch/feed/"
PRESSE_GOOGLE_FEED = ("https://news.google.com/rss/search?"
                      "q=%22Neuhausen+am+Rheinfall%22&hl=de-CH&gl=CH&ceid=CH:de")
PRESSE_ARCHIV_AUSGABE = BASIS / "presse_archiv.js"
PRESSE_ANZEIGE_TAGE = 365        # juengere Artikel stehen direkt auf der Seite
PRESSE_LEAD_MAX_PRO_LAUF = 40    # so viele fehlende Leads werden pro Lauf geholt
PRESSE_RUECKFUELL_AB_JAHR = 2020
KENNZAHLEN_AUSGABE = BASIS / "kennzahlen.js"

# Offizielle SDMX-Schnittstelle der Bundesstatistik (data.stats.swiss).
# Leerwohnungszaehlung: absolute Anzahl leer stehender Wohnungen je Gemeinde,
# aufgeschluesselt nach Anzahl Zimmer und Typ. Neuhausen = BFS-Nr 2937.
SDMX_BASIS = "https://disseminate.stats.swiss/rest/data"
SDMX_LEERWOHNUNG = "CH1.LWZ,DF_LWZ_1,1.0.0"
# City Statistics (urbane Schweiz): enthaelt Neuhausen und den monatlichen
# Netto-Mietzins pro m2 (2016-2024). WICHTIG: kein Median, sondern ein
# durchschnittlicher Netto-Mietzins; Werte koennen statistisch unzuverlaessig
# oder aus Datenschutzgruenden unterdrueckt sein.
SDMX_CITYSTAT = "CH1.CITYSTAT,DF_CITYSTAT_CHURB_2"
# Historische Gemeinnuetzig-CSV (nur 2018) ueber die BFS-Asset-Schnittstelle.
GEMEINNUETZIG_ASSET = "https://dam-api.bfs.admin.ch/hub/api/dam/assets/16564299/master"
# Wohnungsbestand nach Zimmerzahl (fuer die Prozent-Berechnung als Nenner).
# Die Dataflow-Kennung wird per Diagnose am echten System verifiziert.
SDMX_BESTAND = "CH1.GWS,DF_GWS_WHG_1,1.0.0"
KENNZAHLEN_PRUEFTAKT_TAGE = 7   # amtliche Zahlen aendern sich selten
KENNZAHLEN_VERSION = 30          # bei Ausbau/Korrektur erhoehen: erzwingt Neuabfrage
STATTAB_BASIS = "https://www.pxweb.bfs.admin.ch/api/v1/de"
STATTAB_SEITE = "https://www.pxweb.bfs.admin.ch/pxweb/de"
FEED_AUSGABE = BASIS / "feed.xml"
VOLLTEXT_AUSGABE = BASIS / "volltext.js"
VOLLTEXT_MAX_ZEICHEN = 150000   # Textobergrenze pro Dokument
OCR_MAX_SEITEN = 40             # OCR hoechstens fuer so viele Seiten pro PDF
OCR_AUFLOESUNG = 200            # dpi fuer die Texterkennung

# ---------------------------------------------------------------------------
# FRAKTIONSZUORDNUNG  (bitte selbst ergaenzen und korrigieren!)
#
# Format:  "Vorname Nachname": "Fraktion",
# Quelle aktuelle Mitglieder: https://neuhausen.ch/einwohnerrat_mitglieder
# Historische Zuordnungen stammen aus Parteiangaben in den Vorstosstiteln.
# Personen ohne Eintrag erscheinen als "Unbekannt"; das Skript listet sie
# nach jedem Lauf im Terminal auf.
# Hinweis: Gemaess Sitzverteilung bilden SP und parteilos eine Fraktion.
# Wer das zusammenfassen will, ersetzt "parteilos" unten durch "SP".
# ---------------------------------------------------------------------------
FRAKTIONEN = {
    # --- Aktuelle Mitglieder (Legislatur 2025-2028) ---
    "Roland Müller": "Grüne",
    "Nina Schärrer": "FDP",
    "Fabian Bolli": "GLP",
    "Urim Dakaj": "SP",
    "Oliver Fessler": "SVP",
    "Peter Fischli": "FDP",
    "Herbert Hirsiger": "SVP",
    "Arnold Isliker": "SVP",
    "Deborah Isliker": "SVP",
    "Melanie Knuchel": "SP",
    "Matthias Koch": "GLP",
    "Bernhard Koller": "EDU",
    "Thomas Leuzinger": "SP",
    "Dimitrij Ruh": "SP",
    "Christian Schenk": "SP",
    "Ernst Schläpfer": "SP",
    "Silvia Schlegel": "Die Mitte",
    "Urs Schüpbach": "parteilos",
    "Marco Torsello": "FDP",
    "Isabella Zellweger": "SVP",
    # --- Fruehere Mitglieder (Partei aus Vorstosstiteln belegt) ---
    "Jakob Walter": "SP",
    "Renzo Loiudice": "SP",
    "August Hafner": "SP",
    "Priska Weber-Widmer": "SP",
    "Daniel Borer": "SP",
    "Willi Josel": "SVP",
    "Peter Schmid": "SVP",
    "Walter Herrmann": "FDP",
    "Walter Hermann": "FDP",  # Schreibvariante auf der Gemeindeseite
    "Felix Tenger": "FDP",
    "Thomas Theiler": "Die Mitte",   # damals CVP
    "Marcel Stettler": "Die Mitte",  # damals CVP
    "Rita Flück Hänzi": "Die Mitte", # damals CVP
    "Urs Hinnen": "ÖBS",
    # --- Weitere Zuordnungen (manuell ergaenzt) ---
    "Nicole Hinder": "AL",
    "Adrian Schüpbach": "SVP",
    "Randy Ruh": "GLP",
    "Sabina Tektas-Sorg": "SP",
    "Sara Jucker": "SVP",
    "Andreas Neuenschwander": "SVP",
    "Ruedi Meier": "SP",
    "Rolf Forster": "SVP",
    "Robert Eichmann": "SVP",
    "Markus Anderegg": "FDP",
    "Michael Bernath": "ÖBS",
    "René Sauzet": "FDP",
    # --- Schreibvarianten auf der Gemeindeseite ---
    "Bernahrd Koller": "EDU",   # Tippfehler fuer Bernhard Koller
    "Sarah Jucker": "SVP",      # Variante von Sara Jucker
    # --- Keine Personen: eigene Kategorien in der Statistik ---
    "GPK": "GPK",               # Geschaeftspruefungskommission
    "Volksmotion": "Volksmotion",
}

_NAME_TITEL_RE = re.compile(r"^(dr|prof|med)\.?\s+", re.I)

# Personen, die bei einem Lauf nicht zugeordnet werden konnten
UNBEKANNTE_PERSONEN = set()


def _norm_name(name: str) -> str:
    """Titel wie 'Dr.' entfernen, Leerraum normalisieren."""
    n = sauber(name)
    n = _NAME_TITEL_RE.sub("", n)
    return n


def fraktionen_fuer(person_feld: str) -> list:
    """
    Ermittelt die Fraktion(en) fuer das Personenfeld eines Vorstosses.
    Mehrere Einreichende ("A und B", "A / B") werden einzeln zugeordnet.
    Nicht zuordenbare Namen landen in UNBEKANNTE_PERSONEN.
    """
    if not person_feld or person_feld == "-":
        return ["Unbekannt"]
    teile = re.split(r"\s+und\s+|\s*/\s*|\s*&\s*|\s*,\s*", person_feld)
    gefunden = []
    for teil in teile:
        n = _norm_name(teil)
        if not n:
            continue
        # Sammelbegriffe ohne Fraktionsaussage ueberspringen
        if re.match(r"^mitunterzeichn|^weitere$|^andere$", n, re.I):
            continue
        frak = FRAKTIONEN.get(n)
        if frak is None and re.match(r"^[A-Za-zÀ-ÿ]\.\s*\S", n):
            # Nur echte Kurzformen wie "R. Müller" ueber den Nachnamen aufloesen
            for voll, f in FRAKTIONEN.items():
                nachname = voll.split()[-1]
                if n.endswith(" " + nachname) or n.split()[-1] == nachname:
                    frak = f
                    break
        if frak is None:
            UNBEKANNTE_PERSONEN.add(n)
        gefunden.append(frak or "Unbekannt")
    einzig = []
    for f in gefunden:
        if f not in einzig:
            einzig.append(f)
    return einzig or ["Unbekannt"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    "Accept-Language": "de-CH,de;q=0.9",
}
TIMEOUT = 30
DATUM_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def absolut(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://neuhausen.ch" + href
    return "https://neuhausen.ch/" + href


def sauber(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # Zerlegte Umlaute (a + Trema) zu normalen Umlauten zusammenfassen
    return unicodedata.normalize("NFC", text)


def hole(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    # Zeichensatz: Header-Angabe respektieren, sonst UTF-8 annehmen.
    ctype = resp.headers.get("content-type", "")
    if "charset" not in ctype.lower():
        resp.encoding = "utf-8"
    return resp.text


def _zelle(zeile, klasse):
    """Text einer Pseudo-Zelle (span.td.row_X) innerhalb einer Zeile."""
    el = zeile.find("span", class_=lambda c: c and "td" in c.split() and klasse in c.split())
    return sauber(el.get_text()) if el else ""


def _zellen_element(zeile, klasse):
    return zeile.find("span", class_=lambda c: c and "td" in c.split() and klasse in c.split())


def parse_vorstoesse(html: str, quelle: dict) -> list:
    """
    Die Seiten bilden Tabellen mit div/span nach:
      div.tr  >  span.td.row_1 (Datum), row_3 (Name), row_2 (Nummer),
                 row_4 (Inhalt mit PDF-Links), row_5 (Status),
                 row_6 (Duplikat fuer Mobilansicht, wird ignoriert)
    """
    soup = BeautifulSoup(html, "html.parser")
    ergebnis = []
    gesehen = set()

    for zeile in soup.find_all("div", class_="tr"):
        datum = _zelle(zeile, "row_1")
        if not DATUM_RE.match(datum):
            continue  # Kopfzeilen (span.th) und Fremdes ueberspringen

        person = _zelle(zeile, "row_3")
        nummer = _zelle(zeile, "row_2")
        status = _zelle(zeile, "row_5")

        inhalt = _zellen_element(zeile, "row_4")
        haupt, antwort = None, None
        if inhalt is not None:
            for a in inhalt.find_all("a", href=True):
                if "/fileupload/" not in a["href"]:
                    continue
                t = sauber(a.get_text())
                u = absolut(a["href"])
                if haupt is None:
                    haupt = {"titel": t, "url": u}
                elif antwort is None and re.search(r"beantwortung|antwort", t, re.I):
                    antwort = {"titel": t, "url": u}

        titel = haupt["titel"] if haupt else (sauber(inhalt.get_text()) if inhalt else "")
        if not titel:
            continue
        url = haupt["url"] if haupt else quelle["url"]

        schluessel = f"{quelle['kuerzel']}|{datum}|{nummer}|{titel}"
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)

        ergebnis.append({
            "schluessel": schluessel,
            "datum": datum,
            "person": person or "-",
            "nummer": nummer,
            "art": quelle["art"],
            "kuerzel": quelle["kuerzel"],
            "titel": titel,
            "url": url,
            "antwortUrl": antwort["url"] if antwort else None,
            "status": status,
            "fraktionen": fraktionen_fuer(person),
        })
    return ergebnis


def parse_berichte(html: str) -> list:
    """
    Gleiche Pseudo-Tabellenstruktur (div.tr > span.td.row_X), aber andere
    Spalten: Datum, Art, Inhalt, Status (keine Person). Da die row-Nummern
    abweichen koennen, wird flexibel gearbeitet:
      Datum  = Zelle mit Datumsformat
      Inhalt = Zelle mit den meisten fileupload-Links
      Status = letzte kurze Textzelle (pendent/erledigt/...)
      Art    = uebrige Textzelle
    Duplikat-Zellen (Mobilansicht) werden ueber den Schluessel entschaerft.
    """
    soup = BeautifulSoup(html, "html.parser")
    ergebnis = []
    gesehen = set()
    STATUS_WORTE = re.compile(r"pendent|erledigt|zur(ü|ue)ck", re.I)

    for zeile in soup.find_all("div", class_="tr"):
        zellen = zeile.find_all(
            "span", class_=lambda c: c and "td" in c.split()
        )
        if len(zellen) < 3:
            continue

        datum, art, status = "", "", ""
        inhalt_zelle, max_links = None, 0

        for td in zellen:
            text = sauber(td.get_text())
            n_links = len([a for a in td.find_all("a", href=True)
                           if "/fileupload/" in a["href"]])
            if n_links > max_links:
                max_links, inhalt_zelle = n_links, td
                continue
            if not datum and DATUM_RE.match(text):
                datum = text
            elif STATUS_WORTE.search(text) and len(text) < 40:
                status = text
            elif not art and text and n_links == 0:
                art = text

        if not datum or inhalt_zelle is None:
            continue

        dokumente, seen2 = [], set()
        for a in inhalt_zelle.find_all("a", href=True):
            if "/fileupload/" not in a["href"]:
                continue
            t = sauber(a.get_text())
            u = absolut(a["href"])
            if (t, u) in seen2:
                continue
            seen2.add((t, u))
            dokumente.append({"titel": t, "url": u})
        if not dokumente:
            continue

        schluessel = f"ba|{datum}|{dokumente[0]['titel']}"
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)

        ergebnis.append({
            "schluessel": schluessel,
            "datum": datum,
            "art": art or "Bericht und Antrag",
            "status": status,
            "haupt": dokumente[0],
            "beilagen": dokumente[1:],
        })
    return ergebnis


def parse_beschluesse(html: str) -> list:
    """
    Beschluesse & Protokolle: gleiche Pseudo-Tabellenstruktur, Spalten
    Datum, Nummer, Inhalt. Da die row-Nummern abweichen koennen, wird
    flexibel gearbeitet:
      Datum  = Zelle mit Datumsformat
      Inhalt = Zelle mit den meisten fileupload-Links
      Nummer = kuerzeste uebrige Textzelle ohne Links
    """
    soup = BeautifulSoup(html, "html.parser")
    ergebnis = []
    gesehen = set()

    for zeile in soup.find_all("div", class_="tr"):
        zellen = zeile.find_all(
            "span", class_=lambda c: c and "td" in c.split()
        )
        if len(zellen) < 2:
            continue

        datum, nummer = "", ""
        inhalt_zelle, max_links = None, 0
        text_zellen = []

        for td in zellen:
            text = sauber(td.get_text())
            n_links = len([a for a in td.find_all("a", href=True)
                           if "/fileupload/" in a["href"]])
            if n_links > max_links:
                max_links, inhalt_zelle = n_links, td
                continue
            if not datum and DATUM_RE.match(text):
                datum = text
            elif text and n_links == 0:
                text_zellen.append(text)

        if not datum:
            continue
        if text_zellen:
            nummer = min(text_zellen, key=len)

        dokumente, seen2 = [], set()
        if inhalt_zelle is not None:
            for a in inhalt_zelle.find_all("a", href=True):
                if "/fileupload/" not in a["href"]:
                    continue
                t = sauber(a.get_text())
                u = absolut(a["href"])
                if (t, u) in seen2:
                    continue
                seen2.add((t, u))
                dokumente.append({"titel": t, "url": u})

        if dokumente:
            haupt = dokumente[0]
            beilagen = dokumente[1:]
        else:
            # Zeile ohne Dokumentlinks: laengste Textzelle als Titel verwenden
            lange = [t for t in text_zellen if t != nummer]
            if not lange:
                continue
            haupt = {"titel": max(lange, key=len), "url": BP_URL}
            beilagen = []

        schluessel = f"bp|{datum}|{nummer}|{haupt['titel']}"
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)

        ergebnis.append({
            "schluessel": schluessel,
            "datum": datum,
            "nummer": nummer,
            "haupt": haupt,
            "beilagen": beilagen,
        })
    return ergebnis


# ---------------------------------------------------------------------------
# Volltext-Erfassung: extrahiert den Text aller verlinkten PDFs fuer die
# Volltextsuche. Bereits verarbeitete Dokumente werden aus volltext.js
# wiederverwendet (Zwischenspeicher), nur Neues wird heruntergeladen.
# Scans ohne Textebene werden per Texterkennung (tesseract) erfasst,
# sofern die Werkzeuge vorhanden sind (auf GitHub: ja).
# ---------------------------------------------------------------------------

def _lade_volltext_cache() -> dict:
    if not VOLLTEXT_AUSGABE.exists():
        return {}
    try:
        roh = VOLLTEXT_AUSGABE.read_text(encoding="utf-8")
        start = roh.find("{")
        ende = roh.rfind("}")
        cache = json.loads(roh[start:ende + 1])
        return cache if isinstance(cache, dict) else {}
    except Exception:
        return {}


def _normalisiere_volltext(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text[:VOLLTEXT_MAX_ZEICHEN]


def _pdf_text(daten: bytes) -> str:
    """Textebene eines PDFs auslesen (leer, wenn keine vorhanden)."""
    from pypdf import PdfReader
    import io
    leser = PdfReader(io.BytesIO(daten))
    if getattr(leser, "is_encrypted", False):
        try:
            leser.decrypt("")
        except Exception:
            return ""
    teile = []
    for seite in leser.pages:
        teile.append(seite.extract_text() or "")
        if sum(len(t) for t in teile) > VOLLTEXT_MAX_ZEICHEN:
            break
    return " ".join(teile)


def _ocr_moeglich() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def _ocr_text(daten: bytes) -> str:
    """Texterkennung fuer Scans: PDF zu Bildern, dann tesseract (Deutsch)."""
    with tempfile.TemporaryDirectory() as ordner:
        pfad = Path(ordner)
        (pfad / "dok.pdf").write_bytes(daten)
        subprocess.run(
            ["pdftoppm", "-r", str(OCR_AUFLOESUNG), "-png",
             "-l", str(OCR_MAX_SEITEN), "dok.pdf", "seite"],
            cwd=ordner, check=True, capture_output=True, timeout=300,
        )
        teile = []
        for bild in sorted(pfad.glob("seite*.png")):
            ergebnis = subprocess.run(
                ["tesseract", str(bild), "stdout", "-l", "deu"],
                capture_output=True, timeout=300,
            )
            teile.append(ergebnis.stdout.decode("utf-8", errors="ignore"))
            if sum(len(t) for t in teile) > VOLLTEXT_MAX_ZEICHEN:
                break
        return " ".join(teile)


def _hole_pdf(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.content


def _sammle_dokument_urls(vorstoesse, berichte, beschluesse) -> list:
    urls = []
    gesehen = set()

    def nimm(u):
        if u and "/fileupload/" in u and u not in gesehen:
            gesehen.add(u)
            urls.append(u)

    for v in vorstoesse:
        nimm(v.get("url"))
        nimm(v.get("antwortUrl"))
    for gruppe in (berichte, beschluesse):
        for e in gruppe:
            nimm(e.get("haupt", {}).get("url"))
            for b in e.get("beilagen", []):
                nimm(b.get("url"))
    return urls


def baue_volltext(vorstoesse, berichte, beschluesse) -> None:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("Volltext uebersprungen: pypdf fehlt "
              "(Installation: python3 -m pip install --user pypdf)")
        return

    cache = _lade_volltext_cache()
    urls = _sammle_dokument_urls(vorstoesse, berichte, beschluesse)
    kann_ocr = _ocr_moeglich()

    # Erneut versuchen, was frueher scheiterte oder mangels OCR offen blieb
    offen = [u for u in urls
             if u not in cache
             or cache[u].get("q") in ("fehler",)
             or (cache[u].get("q") == "ocr_fehlt" and kann_ocr)]

    neu = ocr_n = leer = fehler = 0
    for i, url in enumerate(offen, 1):
        try:
            daten = _hole_pdf(url)
            text = _pdf_text(daten)
            if len(text.strip()) >= 50:
                cache[url] = {"t": _normalisiere_volltext(text), "q": "pdf"}
            elif kann_ocr:
                try:
                    ocr = _ocr_text(daten)
                    if len(ocr.strip()) >= 50:
                        cache[url] = {"t": _normalisiere_volltext(ocr), "q": "ocr"}
                        ocr_n += 1
                    else:
                        cache[url] = {"t": "", "q": "leer"}
                        leer += 1
                except Exception:
                    cache[url] = {"t": "", "q": "fehler"}
                    fehler += 1
            else:
                cache[url] = {"t": "", "q": "ocr_fehlt"}
            neu += 1
        except Exception:
            cache[url] = {"t": "", "q": "fehler"}
            fehler += 1
        if i % 25 == 0 or i == len(offen):
            print(f"  Volltext: {i}/{len(offen)} neue Dokumente verarbeitet")
        time.sleep(0.15)

    js = "window.NEUHAUSEN_VOLLTEXT = " + json.dumps(cache, ensure_ascii=False) + ";\n"
    VOLLTEXT_AUSGABE.write_text(js, encoding="utf-8")

    erfasst = sum(1 for e in cache.values() if e.get("q") in ("pdf", "ocr"))
    ohne = sum(1 for e in cache.values() if e.get("q") == "leer")
    ausstehend = sum(1 for e in cache.values() if e.get("q") == "ocr_fehlt")
    print(f"Volltext: {erfasst} von {len(cache)} Dokumenten erfasst "
          f"(neu: {neu}, davon OCR: {ocr_n}, ohne Text: {ohne}, Fehler: {fehler})")
    if ausstehend and not kann_ocr:
        print(f"  Hinweis: {ausstehend} Scans warten auf Texterkennung; "
              "die passiert automatisch beim naechsten GitHub-Lauf "
              "(lokal: tesseract und poppler installieren).")


_WOCHENTAGE = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONATE = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _rfc822(datum: str) -> str:
    """dd.mm.yyyy -> RFC-822-Datum (12:00 Schweizer Zeit), fuer RSS."""
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", datum)
    if not m:
        return ""
    dt = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, 0,
                  tzinfo=_ZEITZONE)
    offset = dt.strftime("%z") or "+0000"
    return (f"{_WOCHENTAGE[dt.weekday()]}, {dt.day:02d} "
            f"{_MONATE[dt.month - 1]} {dt.year} 12:00:00 {offset}")


def baue_feed(vorstoesse: list, berichte: list, beschluesse: list, aktuelles: list, presse: list) -> str:
    """Erzeugt einen RSS-2.0-Feed mit den neuesten Eintraegen aller Bereiche."""
    from xml.sax.saxutils import escape

    def datum_zahl(d):
        m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", d)
        return int(m.group(3) + m.group(2) + m.group(1)) if m else 0

    eintraege = []
    for v in vorstoesse:
        eintraege.append({
            "titel": f"[{v['art']}] {v['titel']}",
            "url": v["url"], "datum": v["datum"], "id": v["schluessel"],
        })
    for b in berichte:
        eintraege.append({
            "titel": f"[Bericht & Antrag] {b['haupt']['titel']}",
            "url": b["haupt"]["url"], "datum": b["datum"], "id": b["schluessel"],
        })
    for p in beschluesse:
        eintraege.append({
            "titel": f"[Beschluss/Protokoll] {p['haupt']['titel']}",
            "url": p["haupt"]["url"], "datum": p["datum"], "id": p["schluessel"],
        })
    for a in aktuelles:
        eintraege.append({
            "titel": f"[Aktuelles] {a['titel']}",
            "url": AKTUELLES_URL, "datum": a["datum"], "id": a["schluessel"],
        })
    for p in presse:
        eintraege.append({
            "titel": f"[Presse \u00b7 {p['quelle']}] {p['titel']}",
            "url": p["url"], "datum": p["datum"], "id": p["schluessel"],
        })

    eintraege.sort(key=lambda e: datum_zahl(e["datum"]), reverse=True)
    eintraege = eintraege[:60]

    jetzt = datetime.now(_ZEITZONE)
    offset = jetzt.strftime("%z") or "+0000"
    build = (f"{_WOCHENTAGE[jetzt.weekday()]}, {jetzt.day:02d} "
             f"{_MONATE[jetzt.month - 1]} {jetzt.year} "
             f"{jetzt.hour:02d}:{jetzt.minute:02d}:00 {offset}")

    teile = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>Einwohnerrat Neuhausen am Rheinfall</title>",
        "<link>https://neuhausen.ch/einwohnerrat</link>",
        "<description>Vorstösse, Berichte &amp; Anträge sowie Beschlüsse &amp; "
        "Protokolle des Einwohnerrats Neuhausen am Rheinfall. "
        "Automatisch aufbereitet.</description>",
        "<language>de-ch</language>",
        f"<lastBuildDate>{build}</lastBuildDate>",
    ]
    for e in eintraege:
        teile.append("<item>")
        teile.append(f"<title>{escape(e['titel'])}</title>")
        teile.append(f"<link>{escape(e['url'])}</link>")
        teile.append(f'<guid isPermaLink="false">{escape(e["id"])}</guid>')
        pub = _rfc822(e["datum"])
        if pub:
            teile.append(f"<pubDate>{pub}</pubDate>")
        teile.append("</item>")
    teile.append("</channel>")
    teile.append("</rss>")
    return "\n".join(teile) + "\n"


def parse_aktuelles(html: str) -> list:
    """
    Aktuelles-Seite: Meldungen als Ueberschriften-Folge
      h2 = Titel, optional h3 = Untertitel, h4 = Datum (tt.mm.jjjj),
      danach Absaetze mit dem Meldungstext.
    Es wird ein kurzer Anriss (max. ~260 Zeichen) erzeugt.
    """
    soup = BeautifulSoup(html, "html.parser")
    ergebnis = []
    gesehen = set()
    aktuell = None

    def abschliessen():
        nonlocal aktuell
        if aktuell and aktuell["datum"] and aktuell["titel"]:
            anriss = re.sub(r"\s+", " ", " ".join(aktuell["absaetze"])).strip()
            if len(anriss) > 260:
                anriss = anriss[:260].rsplit(" ", 1)[0] + " \u2026"
            schluessel = f"ak|{aktuell['datum']}|{aktuell['titel']}"
            if schluessel not in gesehen:
                gesehen.add(schluessel)
                ergebnis.append({
                    "schluessel": schluessel,
                    "datum": aktuell["datum"],
                    "titel": aktuell["titel"],
                    "untertitel": aktuell["untertitel"],
                    "anriss": anriss,
                })
        aktuell = None

    for el in soup.find_all(["h2", "h3", "h4", "p"]):
        text = sauber(el.get_text())
        if el.name == "h2":
            abschliessen()
            if text:
                aktuell = {"titel": text, "untertitel": "", "datum": "",
                           "absaetze": []}
        elif aktuell is None:
            continue
        elif el.name == "h3":
            if not aktuell["datum"] and not aktuell["untertitel"]:
                aktuell["untertitel"] = text
        elif el.name == "h4":
            if DATUM_RE.match(text):
                aktuell["datum"] = text
        elif el.name == "p":
            if aktuell["datum"] and text and len(" ".join(aktuell["absaetze"])) < 400:
                aktuell["absaetze"].append(text)

    abschliessen()
    return ergebnis


# ---------------------------------------------------------------------------
# Presse: Artikel ueber Neuhausen aus regionalen Medien.
# Quellen: SHN-Rubrik Neuhausen (HTML), Schaffhauser AZ (RSS),
# Google News (RSS, Auffangnetz fuer weitere Medien).
# Es werden nur Titel, Datum, Anriss und Link uebernommen; gelesen wird
# beim jeweiligen Medium.
# ---------------------------------------------------------------------------

def _titel_schluessel(titel: str) -> str:
    """Normalisierter Titel fuer den Dubletten-Abgleich zwischen Quellen."""
    t = unicodedata.normalize("NFKD", titel or "").lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t[:60]


def _datum_aus_text(wert: str) -> str:
    """RFC-822- oder ISO-Datum zu tt.mm.jjjj (Schweizer Zeit)."""
    wert = (wert or "").strip()
    if not wert:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(wert)
    except Exception:
        try:
            dt = datetime.fromisoformat(wert.replace("Z", "+00:00"))
        except Exception:
            return ""
    try:
        if dt.tzinfo is not None and _ZEITZONE is not None:
            dt = dt.astimezone(_ZEITZONE)
    except Exception:
        pass
    return dt.strftime("%d.%m.%Y")


def _kuerze_anriss(text: str, laenge: int = 240) -> str:
    import html as _html
    text = _html.unescape(_html.unescape(text or ""))
    text = sauber(re.sub(r"<[^>]+>", " ", text))
    if len(text) > laenge:
        text = text[:laenge].rsplit(" ", 1)[0] + " \u2026"
    return text


def _parse_feed(xml_text: str) -> list:
    """Minimaler RSS-2.0- und Atom-Parser (ohne Zusatzbibliotheken)."""
    import xml.etree.ElementTree as ET
    try:
        wurzel = ET.fromstring(xml_text.encode("utf-8")
                               if isinstance(xml_text, str) else xml_text)
    except Exception:
        return []

    def lokal(tag):
        return tag.rsplit("}", 1)[-1]

    eintraege = []
    for item in wurzel.iter():
        if lokal(item.tag) not in ("item", "entry"):
            continue
        titel, url, datum, anriss, quelle = "", "", "", "", ""
        for kind in item:
            name = lokal(kind.tag)
            text = (kind.text or "").strip()
            if name == "title":
                import html as _html
                titel = sauber(_html.unescape(_html.unescape(text)))
            elif name == "link":
                url = text or kind.get("href", "")
            elif name in ("pubDate", "published", "updated") and not datum:
                datum = _datum_aus_text(text)
            elif name in ("description", "summary") and not anriss:
                anriss = _kuerze_anriss(text)
            elif name == "source":
                quelle = sauber(text)
        if titel and url:
            eintraege.append({"titel": titel, "url": url.strip(),
                              "datum": datum, "anriss": anriss,
                              "quelle": quelle})
    return eintraege


def hole_presse() -> tuple:
    """Sammelt Presseartikel; gibt (eintraege, quellen_fehler) zurueck."""
    gesammelt = []
    gesehen = set()
    quellen_fehler = []

    def nimm(titel, url, datum, anriss, quelle):
        ts = _titel_schluessel(titel)
        if not ts or ts in gesehen or not datum:
            return
        gesehen.add(ts)
        gesammelt.append({
            "schluessel": f"pr|{datum}|{ts}",
            "datum": datum,
            "titel": titel,
            "quelle": quelle,
            "url": url,
            "anriss": anriss,
        })

    # 1) SHN, Rubrik Neuhausen (Datum steckt im Artikel-Link)
    try:
        for titel, url, datum in _parse_shn_liste(hole(PRESSE_SHN_URL)):
            nimm(titel, url, datum, "", "SHN")
    except Exception as e:
        quellen_fehler.append(f"SHN: {e}")

    # 2) Schaffhauser AZ (RSS), gefiltert auf Neuhausen
    try:
        for e in _parse_feed(hole(PRESSE_SHAZ_FEED)):
            if "neuhausen" not in (e["titel"] + " " + e["anriss"]).lower():
                continue
            nimm(e["titel"], e["url"], e["datum"], e["anriss"], "Schaffhauser AZ")
    except Exception as e:
        quellen_fehler.append(f"Schaffhauser AZ: {e}")

    # 3) Google News als Auffangnetz (weitere Medien)
    try:
        for e in _parse_feed(hole(PRESSE_GOOGLE_FEED)):
            titel = e["titel"]
            quelle = e["quelle"] or "weitere Medien"
            # Google haengt die Quelle mit wechselnden Trennzeichen an den Titel an
            if e["quelle"]:
                titel = re.sub(
                    r"[\s\-\u2013\u2014|\u00b7]+" + re.escape(e["quelle"]) + r"$",
                    "", titel).strip()
            nimm(titel, e["url"], e["datum"], _kuerze_anriss(e["anriss"]), quelle)
    except Exception as e:
        quellen_fehler.append(f"Google News: {e}")

    def datum_zahl(d):
        m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", d)
        return int(m.group(3) + m.group(2) + m.group(1)) if m else 0

    gesammelt.sort(key=lambda e: datum_zahl(e["datum"]), reverse=True)
    return gesammelt, quellen_fehler


def _parse_shn_liste(html: str) -> list:
    """Artikel-Links der SHN-Rubrik Neuhausen: [(titel, url, tt.mm.jjjj)]."""
    soup = BeautifulSoup(html, "html.parser")
    ergebnis = []
    for a in soup.find_all("a", href=True):
        if "/region/neuhausen/" not in a["href"]:
            continue
        m = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", a["href"])
        if not m:
            continue
        titel = sauber(a.get_text())
        if len(titel) < 15:
            continue  # Navigations- und Weiterlesen-Links ueberspringen
        url = a["href"]
        if url.startswith("/"):
            url = "https://www.shn.ch" + url
        ergebnis.append((titel, url, f"{m.group(3)}.{m.group(2)}.{m.group(1)}"))
    return ergebnis


def _presse_datum_zahl(d: str) -> int:
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", d or "")
    return int(m.group(3) + m.group(2) + m.group(1)) if m else 0


def _lade_presse_archiv() -> dict:
    """Bestehendes Archiv aus presse_archiv.js lesen (dient als Zwischenspeicher)."""
    leer = {"rueckfuellung": False, "eintraege": {}}
    if not PRESSE_ARCHIV_AUSGABE.exists():
        return leer
    try:
        roh = PRESSE_ARCHIV_AUSGABE.read_text(encoding="utf-8")
        obj = json.loads(roh[roh.find("{"):roh.rfind("}") + 1])
        if isinstance(obj, dict) and isinstance(obj.get("eintraege"), dict):
            obj.setdefault("rueckfuellung", False)
            return obj
    except Exception:
        pass
    return leer


def _hole_lead(url: str) -> str:
    """Oeffentliche Seitenbeschreibung (og:description) eines Artikels holen."""
    soup = BeautifulSoup(hole(url), "html.parser")
    for suche in ({"property": "og:description"}, {"name": "description"}):
        meta = soup.find("meta", attrs=suche)
        if meta and meta.get("content"):
            return _kuerze_anriss(meta["content"])
    return ""


def sammle_rueckfuellung() -> tuple:
    """Einmaliger Versuch, aeltere Artikel bis PRESSE_RUECKFUELL_AB_JAHR zu holen."""
    funde = []
    meldungen = []

    # 1) SHN-Rubrik seitenweise zurueckblaettern
    try:
        seiten = 0
        for seite in range(1, 250):
            liste = _parse_shn_liste(hole(f"{PRESSE_SHN_URL}?page={seite}"))
            if not liste:
                break
            seiten += 1
            aelter = False
            for titel, url, datum in liste:
                jahr = int(datum[-4:])
                if jahr >= PRESSE_RUECKFUELL_AB_JAHR:
                    funde.append({"titel": titel, "url": url, "datum": datum,
                                  "anriss": "", "quelle": "SHN"})
                else:
                    aelter = True
            if aelter:
                break
            time.sleep(0.2)
        meldungen.append(f"SHN-Archiv: {seiten} Seiten gelesen")
    except Exception as e:
        meldungen.append(f"SHN-Archiv abgebrochen: {e}")

    # 2) Google News in Halbjahres-Fenstern
    try:
        jahr, fenster = PRESSE_RUECKFUELL_AB_JAHR, 0
        heute = datetime.now(_ZEITZONE) if _ZEITZONE else datetime.now()
        while jahr <= heute.year:
            for start, ende in ((f"{jahr}-01-01", f"{jahr}-07-01"),
                                (f"{jahr}-07-01", f"{jahr + 1}-01-01")):
                url = (PRESSE_GOOGLE_FEED
                       + f"+after:{start}+before:{ende}")
                for e in _parse_feed(hole(url)):
                    titel = e["titel"]
                    if e["quelle"]:
                        titel = re.sub(
                            r"[\s\-\u2013\u2014|\u00b7]+" + re.escape(e["quelle"]) + r"$",
                            "", titel).strip()
                    funde.append({"titel": titel, "url": e["url"],
                                  "datum": e["datum"], "anriss": _kuerze_anriss(e["anriss"]),
                                  "quelle": e["quelle"] or "weitere Medien"})
                fenster += 1
                time.sleep(0.2)
            jahr += 1
        meldungen.append(f"Google News: {fenster} Zeitfenster abgefragt")
    except Exception as e:
        meldungen.append(f"Google News abgebrochen: {e}")

    # 3) Schaffhauser AZ ueber den Such-Feed
    try:
        az_seiten = 0
        for p in range(1, 11):
            xml = hole(f"https://www.shaz.ch/?s=Neuhausen&feed=rss2&paged={p}")
            eintraege = _parse_feed(xml)
            if not eintraege:
                break
            az_seiten += 1
            for e in eintraege:
                if "neuhausen" not in (e["titel"] + " " + e["anriss"]).lower():
                    continue
                funde.append({"titel": e["titel"], "url": e["url"],
                              "datum": e["datum"], "anriss": e["anriss"],
                              "quelle": "Schaffhauser AZ"})
            time.sleep(0.2)
        meldungen.append(f"Schaffhauser AZ: {az_seiten} Suchseiten gelesen")
    except Exception as e:
        meldungen.append(f"Schaffhauser AZ abgebrochen: {e}")

    return funde, meldungen


def baue_presse() -> tuple:
    """
    Fuehrt alles zusammen: aktuelle Quellen, einmalige Rueckfuellung,
    dauerhaftes Archiv (presse_archiv.js) und Lead-Texte.
    Gibt (juengste Eintraege fuer die Seite, fehlerliste) zurueck.
    """
    archiv = _lade_presse_archiv()
    eintraege = archiv["eintraege"]
    titel_index = {}
    for s, e in eintraege.items():
        titel_index[_titel_schluessel(e.get("titel", ""))] = s

    def einpflegen(kandidat):
        ts = _titel_schluessel(kandidat["titel"])
        if not ts or not kandidat.get("datum"):
            return 0
        if ts in titel_index:
            vorhanden = eintraege[titel_index[ts]]
            if not vorhanden.get("anriss") and kandidat.get("anriss"):
                vorhanden["anriss"] = kandidat["anriss"]
            return 0
        s = f"pr|{kandidat['datum']}|{ts}"
        eintraege[s] = {
            "schluessel": s, "datum": kandidat["datum"],
            "titel": kandidat["titel"], "quelle": kandidat["quelle"],
            "url": kandidat["url"], "anriss": kandidat.get("anriss", ""),
        }
        titel_index[ts] = s
        return 1

    aktuelle, quellen_fehler = hole_presse()
    neu_aktuell = sum(einpflegen(e) for e in aktuelle)

    if not archiv.get("rueckfuellung"):
        print("  Presse: einmalige Rueckfuellung bis "
              f"{PRESSE_RUECKFUELL_AB_JAHR} laeuft (dauert etwas laenger) ...")
        rueck, meldungen = sammle_rueckfuellung()
        neu_rueck = sum(einpflegen(e) for e in rueck)
        for m in meldungen:
            print(f"    {m}")
        print(f"    Rueckfuellung: {neu_rueck} zusaetzliche Artikel erfasst")
        archiv["rueckfuellung"] = True

    # Lead-Texte: fehlende Anrisse einmalig von den Artikelseiten holen
    geholt = 0
    for e in eintraege.values():
        if e.get("anriss") or e.get("lv"):
            continue
        if "news.google.com" in e.get("url", ""):
            e["lv"] = 1  # Google-Umleitungen liefern keine Beschreibung
            continue
        if geholt >= PRESSE_LEAD_MAX_PRO_LAUF:
            continue
        try:
            e["anriss"] = _hole_lead(e["url"])
        except Exception:
            e["anriss"] = ""
        e["lv"] = 1
        geholt += 1
        time.sleep(0.15)

    js = ("window.NEUHAUSEN_PRESSE_ARCHIV = "
          + json.dumps(archiv, ensure_ascii=False) + ";\n")
    PRESSE_ARCHIV_AUSGABE.write_text(js, encoding="utf-8")

    heute = datetime.now(_ZEITZONE) if _ZEITZONE else datetime.now()
    from datetime import timedelta
    grenze = int((heute - timedelta(days=PRESSE_ANZEIGE_TAGE)).strftime("%Y%m%d"))
    alle = sorted(eintraege.values(),
                  key=lambda e: _presse_datum_zahl(e["datum"]), reverse=True)
    juengste = [e for e in alle if _presse_datum_zahl(e["datum"]) >= grenze]
    print(f"  Presse: {len(juengste)} Eintraege auf der Seite, "
          f"{len(alle) - len(juengste)} im Archiv "
          f"(neu: {neu_aktuell}, Leads geholt: {geholt})")
    return juengste, quellen_fehler


# ---------------------------------------------------------------------------
# Kennzahlen: amtliche Zahlen zur Gemeinde (Etappe 1).
# Quellen: BFS STAT-TAB (Bevoelkerung, Beschaeftigung) via oeffentliche
# Schnittstelle ohne Anmeldung, plus verifizierte Steuerfuesse des Kantons.
# Ergebnis: kennzahlen.js, hoechstens einmal pro Woche neu abgefragt.
# ---------------------------------------------------------------------------

_STATTAB_META = {}
_STATTAB_BASIS_OK = {}


def _stattab_reihe(cube: str, festlegungen: list,
                   region: str = "neuhausen am rheinfall",
                   fest: dict = None, summiere: list = None) -> tuple:
    """
    Zeitreihe fuer Neuhausen am Rheinfall aus einem STAT-TAB-Datenwuerfel.
    Liest zuerst die Struktur des Wuerfels und waehlt dann:
      - die Gemeinde Neuhausen am Rheinfall,
      - alle Jahre,
      - pro uebriger Dimension den Wert gemaess `fest` (dict: code->begriffe),
        sonst `festlegungen` (Liste von Suchbegriffen), sonst ein Total.
    Lieber gar keine Zahl als eine falsche: Ohne eindeutige Auswahl
    wird abgebrochen.
    Gibt ([(jahr, wert), ...], quellen_url) zurueck.
    """
    fest = fest or {}
    summiere = [s.lower() for s in (summiere or [])]
    basis = f"{STATTAB_BASIS}/{cube}/{cube}.px"
    if cube not in _STATTAB_META:
        r = requests.get(basis, headers=HEADERS, timeout=60)
        if r.status_code >= 400:
            # Manche Wuerfel brauchen andere Adressformen: kurz (einstufig)
            # oder dreistufig (Unterordner gleichen Namens).
            for alt_basis in (f"{STATTAB_BASIS}/{cube}.px",
                              f"{STATTAB_BASIS}/{cube}/{cube}/{cube}.px"):
                r2 = requests.get(alt_basis, headers=HEADERS, timeout=60)
                if r2.status_code < 400:
                    basis, r = alt_basis, r2
                    break
        r.raise_for_status()
        _STATTAB_META[cube] = r.json()
        _STATTAB_BASIS_OK[cube] = basis
    meta = _STATTAB_META[cube]
    basis = _STATTAB_BASIS_OK.get(cube, basis)

    def wahl(texte, begriffe):
        klein = [t.lower() for t in texte]
        for b in begriffe:
            b = b.lower()
            for i, t in enumerate(klein):
                if t == b:
                    return i
            for i, t in enumerate(klein):
                if t.startswith(b):
                    return i
            for i, t in enumerate(klein):
                if b in t:
                    return i
        return None

    def waehle_total(code, texte, werte):
        """Fuer Nicht-Regions-Dimensionen: eindeutig das Total nehmen.
        Bricht ab, wenn kein klarer Total-Wert existiert (kein Raten)."""
        i = wahl(texte, ["schweiz und ausland", "total", "gesamt",
                         "alle ", "insgesamt", "- total"])
        if i is not None:
            return werte[i]
        if len(werte) == 1:
            return werte[0]
        return None

    abfrage = []
    zeit_code = None
    for var in meta.get("variables", []):
        code = var["code"]
        texte = var.get("valueTexts", [])
        if var.get("time") or code.lower() in ("jahr", "periode"):
            zeit_code = code
            abfrage.append({"code": code,
                            "selection": {"filter": "all", "values": ["*"]}})
            continue
        ist_regions_dim = any(w in code.lower() for w in
                              ("gemeinde", "kanton", "bezirk", "region"))
        i = wahl(texte, [region]) if ist_regions_dim else None
        if i is not None:
            abfrage.append({"code": code,
                            "selection": {"filter": "item",
                                          "values": [var["values"][i]]}})
            continue
        # gezielte Festlegung einzelner Dimensionen (code -> Suchbegriffe)
        if code in fest:
            i = wahl(texte, fest[code])
            if i is None:
                raise RuntimeError(f"{cube}: Festlegung '{fest[code]}' "
                                   f"nicht gefunden in '{code}'")
            abfrage.append({"code": code,
                            "selection": {"filter": "item",
                                          "values": [var["values"][i]]}})
            continue
        # Dimension ueber alle Werte summieren (z. B. Gebaeudetyp ohne Total)
        if code.lower() in summiere:
            abfrage.append({"code": code,
                            "selection": {"filter": "all", "values": ["*"]}})
            continue
        i = wahl(texte, festlegungen)
        if i is not None:
            abfrage.append({"code": code,
                            "selection": {"filter": "item",
                                          "values": [var["values"][i]]}})
            continue
        total = waehle_total(code, texte, var["values"])
        if total is None:
            raise RuntimeError(f"{cube}: kein eindeutiges Total fuer '{code}'")
        abfrage.append({"code": code,
                        "selection": {"filter": "item", "values": [total]}})
    if zeit_code is None:
        raise RuntimeError(f"{cube}: keine Zeitachse gefunden")

    antwort = requests.post(
        basis, json={"query": abfrage, "response": {"format": "json-stat2"}},
        headers=HEADERS, timeout=90)
    antwort.raise_for_status()
    js = antwort.json()
    if "dataset" in js:  # aeltere json-stat-Variante
        js = js["dataset"]
        ids = js["dimension"]["id"]
        dims = js["dimension"]
    else:
        ids = js["id"]
        dims = js["dimension"]

    zeit_id = None
    for i in ids:
        if i.lower() == zeit_code.lower():
            zeit_id = i
            break
    if zeit_id is None:
        zeit_id = ids[-1]

    # Groessen aller Dimensionen in Reihenfolge (json-stat ist zeilen-major)
    groessen = []
    for dim_id in ids:
        kat = dims[dim_id]["category"]
        idx = kat.get("index")
        if isinstance(idx, list):
            groessen.append(len(idx))
        else:
            groessen.append(len(idx))
    werte = js["value"]

    kat = dims[zeit_id]["category"]
    index = kat.get("index")
    labels = kat.get("label", {})
    if isinstance(index, list):
        index = {c: p for p, c in enumerate(index)}
    zeit_pos_von_id = ids.index(zeit_id)

    # Fuer jede Zelle die Zeit-Koordinate bestimmen und Werte je Jahr summieren.
    def koordinate(flach):
        koords = []
        rest = flach
        for g in reversed(groessen):
            koords.append(rest % g)
            rest //= g
        return list(reversed(koords))

    jahr_von_pos = {p: str(labels.get(c, c))[:4]
                    for c, p in index.items()}
    summe = {}
    gesehen = set()
    for flach, w in enumerate(werte):
        zpos = koordinate(flach)[zeit_pos_von_id]
        jahr = jahr_von_pos.get(zpos)
        if not (jahr and re.match(r"^\d{4}$", jahr)):
            continue
        if isinstance(w, (int, float)):
            summe[jahr] = summe.get(jahr, 0) + w
            gesehen.add(jahr)
        # Strings wie "..." (Daten nicht verfuegbar) oder None -> ignorieren

    # Nur Jahre mit mindestens einem echten Wert behalten
    summe = {j: v for j, v in summe.items() if j in gesehen}

    reihe = sorted(summe.items(), key=lambda kv: int(kv[0]))
    if not reihe:
        raise RuntimeError(f"{cube}: leere Zeitreihe")
    return reihe, f"{STATTAB_SEITE}/{cube}/-/{cube}.px/"


def _wachstum(reihe: list, ab_jahr: int):
    """Prozentuale Veraenderung vom ersten Wert ab `ab_jahr` bis zum letzten."""
    ab = [(j, w) for j, w in reihe if int(j) >= ab_jahr and w]
    if len(ab) < 2 or not ab[0][1]:
        return None
    return round((ab[-1][1] / ab[0][1] - 1) * 100, 1)


def _sdmx_csv(dataflow: str, schluessel: str, timeout: int = 90) -> list:
    """Ruft die SDMX-API im CSV-Format ab und gibt eine Liste von dict-Zeilen
    zurueck (Spaltenname -> Wert). Robust gegen die begleitenden Struktur- und
    Metadatenspalten der Bundesstatistik-Schnittstelle."""
    import csv
    import io
    url = (f"{SDMX_BASIS}/{dataflow}/{schluessel}"
           f"?dimensionAtObservation=AllDimensions")
    kopf = dict(HEADERS)
    kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
    r = requests.get(url, headers=kopf, timeout=timeout)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", "replace")
    leser = csv.DictReader(io.StringIO(text))
    return list(leser), url


def _sdmx_struktur_dimensionen(struktur_id: str) -> list:
    """Laedt die Dimensionsliste einer SDMX-Datenstruktur (DSD).
    Gibt die Dimensions-IDs in Reihenfolge zurueck."""
    import re as _re
    url = (f"https://disseminate.stats.swiss/rest/datastructure/CH1.GWS/"
           f"{struktur_id}/1.0.0?references=children")
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    txt = r.content.decode("utf-8", "replace")
    # Dimensionen mit Position; TimeDimension separat
    dims = _re.findall(
        r'<structure:Dimension[^>]*\bid="([^"]+)"', txt)
    if not dims:
        dims = _re.findall(r'<Dimension[^>]*\bid="([^"]+)"', txt)
    return dims


# Kandidaten fuer die Gemeinde-Regionsdimension (nach Haeufigkeit im BFS-SDMX)
_GEMEINDE_DIMS = ("GR_KT_GDE", "GDENR", "GEO", "REGION", "GEBIET", "MUNICIPALITY")


def _leerwohnung_bestand(bfs_nr: str = "2937") -> tuple:
    """Findet den GWS-Wohnungsbestand je Jahr fuer die Gemeinde.

    Nutzt zuerst das vom BFS dokumentierte Filtermuster fuer DF_GWS_REG5
    ('A....{Gebietscode}', FREQ zuerst, Gebietscode zuletzt). Erst wenn das
    nicht greift, wird die Struktur analysiert und der Schluessel selbst
    gebaut. Gibt (bestand_dict, dataflow_id) zurueck; sonst ({}, "").
    """
    import re as _re

    # --- Stufe 1: dokumentiertes Muster fuer REG5 -------------------------
    # Quelle: BFS-Beispiel "A....8100" fuer diesen Datenwuerfel.
    for muster in (f"A....{bfs_nr}", f"A...{bfs_nr}", f"A.....{bfs_nr}"):
        url = (f"{SDMX_BASIS}/CH1.GWS,DF_GWS_REG5,1.0.0/{muster}"
               f"?dimensionAtObservation=AllDimensions")
        try:
            kopf = dict(HEADERS)
            kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
            r = requests.get(url, headers=kopf, timeout=90)
            if r.status_code >= 400 or len(r.content) < 80:
                continue
            bestand = _bestand_aus_csv(
                r.content.decode("utf-8-sig", "replace"))
            if len(bestand) >= 3:
                return bestand, "DF_GWS_REG5"
        except Exception:
            continue

    # --- Stufe 2: Struktur analysieren und Schluessel selbst bauen --------
    try:
        rreg = requests.get(
            "https://disseminate.stats.swiss/rest/dataflow/CH1.GWS",
            headers=HEADERS, timeout=60)
        rreg.raise_for_status()
        reg_txt = rreg.content.decode("utf-8", "replace")
    except Exception:
        return {}, ""

    # Dataflow-ID -> Struktur-ID (DSD). Mehrere XML-Formen abdecken.
    df_zu_dsd = {}
    # Form A: Dataflow-Block mit eingebettetem Structure-Ref
    for m in _re.finditer(
            r'<(?:structure:)?Dataflow[^>]*\bid="(DF_GWS_[^"]+)"(.*?)'
            r'</(?:structure:)?Dataflow>', reg_txt, _re.DOTALL):
        dfid, block = m.group(1), m.group(2)
        ref = _re.search(r'\bid="(DSD_GWS_[^"]+)"', block)
        if ref:
            df_zu_dsd[dfid] = ref.group(1)
    # Form B: falls kein Block matchte, DF- und DSD-IDs paarweise annehmen
    if not df_zu_dsd:
        for dfid in _re.findall(r'\bid="(DF_GWS_REG\d)"', reg_txt):
            df_zu_dsd[dfid] = dfid.replace("DF_", "DSD_")

    # 2) Fuer jeden Dataflow die Dimensionen pruefen; nur die mit
    #    gemeindefaehiger Regionsdimension weiterverfolgen. DF_GWS_REG5 ist
    #    der verifizierte Wohnungsbestand-Datenfluss (Gemeindeebene) und wird
    #    zuerst geprueft; die uebrigen bleiben als Rueckfallebene.
    bevorzugt = "DF_GWS_REG5"
    reihenfolge = ([bevorzugt] if bevorzugt in df_zu_dsd else []) + \
                  [d for d in df_zu_dsd if d != bevorzugt]
    for dfid in reihenfolge:
        dsd = df_zu_dsd[dfid]
        try:
            dims = _sdmx_struktur_dimensionen(dsd)
        except Exception:
            continue
        # Regionsdimension finden
        region_pos = None
        for i, d in enumerate(dims):
            if d.upper() in _GEMEINDE_DIMS:
                region_pos = i
                break
        if region_pos is None:
            continue   # nur Kantonsebene o. Ae., ueberspringen

        # 3) Datenschluessel bauen: an Regionsposition die BFS-Nr, sonst leer.
        #    Zeitdimension nicht mitzaehlen (die steht separat).
        n = len(dims)
        teile = ["" for _ in range(n)]
        teile[region_pos] = str(bfs_nr)
        schluessel = ".".join(teile)
        for frq in ("A", ""):
            sk = schluessel
            # FREQ ist oft die erste Dimension; wenn vorhanden auf A setzen
            if "FREQ" in dims:
                fpos = dims.index("FREQ")
                t2 = teile[:]
                t2[fpos] = frq or "A"
                sk = ".".join(t2)
            url = (f"{SDMX_BASIS}/CH1.GWS,{dfid},1.0.0/{sk}"
                   f"?dimensionAtObservation=AllDimensions")
            try:
                kopf = dict(HEADERS)
                kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
                r = requests.get(url, headers=kopf, timeout=90)
                # Leere SDMX-Antworten sind sehr kurz; echte Daten haben
                # mindestens eine Kopf- und Datenzeile.
                if r.status_code >= 400 or len(r.content) < 80:
                    continue
                bestand = _bestand_aus_csv(
                    r.content.decode("utf-8-sig", "replace"))
                if len(bestand) >= 3:
                    return bestand, dfid
            except Exception:
                continue
    return {}, ""


def _bestand_aus_csv(text: str) -> dict:
    """Summiert aus einer GWS-CSV den Gesamtwohnungsbestand je Jahr.
    Nimmt nur Totalzeilen (alle Aufschluesselungs-Dimensionen auf Total '_T'
    oder gleichwertig), um Doppelzaehlung zu vermeiden. Faellt, wenn keine
    reinen Totalzeilen existieren, auf die Summe je Jahr ueber die
    feinste Vollpartition zurueck."""
    import csv
    import io
    zeilen = list(csv.DictReader(io.StringIO(text)))
    if not zeilen:
        return {}
    # Aufschluesselungs-Dimensionen (alles ausser Zeit/Wert/Struktur/Region)
    ignor = {"TIME_PERIOD", "OBS_VALUE", "STRUCTURE", "STRUCTURE_ID",
             "STRUCTURE_NAME", "ACTION", "FREQ"}
    spalten = [k for k in zeilen[0].keys()
               if k and k.upper() == k and k not in ignor
               and not k.startswith("DIFF") and k not in ("DECIMALS",
               "OBS_STATUS")]
    # Regionsspalte ausschliessen (sie ist konstant = unsere Gemeinde)
    aufschluessel = [k for k in spalten
                     if k.upper() not in _GEMEINDE_DIMS
                     and not k.lower().startswith("grossregion")]

    def ist_total(row):
        for k in aufschluessel:
            v = (row.get(k) or "").strip()
            if v and v not in ("_T", "T", "_Z", "TOTAL", "0"):
                return False
        return True

    bestand = {}
    total_zeilen = [z for z in zeilen if ist_total(z)]
    quelle = total_zeilen if total_zeilen else zeilen
    aggregiert = not total_zeilen
    for z in quelle:
        jahr = (z.get("TIME_PERIOD") or "").strip()[:4]
        wert = (z.get("OBS_VALUE") or "").strip()
        if not (_re_jahr(jahr) and wert):
            continue
        try:
            v = float(wert)
        except ValueError:
            continue
        if aggregiert:
            bestand[jahr] = bestand.get(jahr, 0) + v
        else:
            bestand[jahr] = v
    # Plausibilisierung
    return {j: int(round(v)) for j, v in bestand.items()
            if 10 <= v <= 100000}


def _re_jahr(s: str) -> bool:
    return bool(re.match(r"^(19|20)\d{2}$", s or ""))


def _anzahl_aus_gemeinde() -> dict:
    """Liest die Anzahl leer stehender Wohnungen je Jahr aus der amtlichen
    Gemeinde-Meldung ('1. Juni JJJJ NN Wohnungen P.PP %'). Ergaenzt die
    neuesten Jahre, die im BFS-Dataflow noch fehlen. Gibt {jahr: anzahl}."""
    roh = requests.get(AKTUELLES_URL, headers=HEADERS, timeout=60)
    roh.raise_for_status()
    anzahl = {}
    for m in re.finditer(
            r"1\.\s*Juni\s*(20\d{2})\s+(\d+)\s+Wohnungen", roh.text):
        jahr, zahl = m.group(1), m.group(2)
        try:
            wert = int(zahl)
            if 0 <= wert <= 5000 and jahr not in anzahl:
                anzahl[jahr] = wert
        except ValueError:
            continue
    return anzahl


def _bestand_aus_gemeinde() -> dict:
    """Liest den Wohnungsbestand je Jahr aus der amtlichen Aktuelles-Meldung
    der Gemeinde ('Auf der Basis von N Wohnungen ...'). Ergaenzt die neuesten
    Jahre, die im GWS-Dataflow noch fehlen. Gibt {jahr: bestand}."""
    roh = requests.get(AKTUELLES_URL, headers=HEADERS, timeout=60)
    roh.raise_for_status()
    text = roh.text
    bestand = {}
    # Muster: "Basis von 6'369 Wohnungen ... 1. Juni 2026"
    # METHODIK: Die "Basis von N Wohnungen" ist der GWS-Bestand am Ende des
    # VORJAHRES der Zaehlung. Die Meldung nennt das Zaehljahr (2026), der
    # Bestand 6369 gehoert aber zum GWS-Bezugsjahr 2025. Wir legen ihn unter
    # (Zaehljahr - 1) ab, passend zur Ziffer-Berechnung mit Vorjahresbestand.
    for m in re.finditer(
            r"[Bb]asis von\s+([\d'\u2019]+)\s+Wohnungen.{0,40}?"
            r"1\.\s*Juni\s*(20\d{2})", text, re.DOTALL):
        roh_zahl = m.group(1).replace("'", "").replace("\u2019", "")
        gws_bezugsjahr = str(int(m.group(2)) - 1)
        try:
            wert = int(roh_zahl)
            if 1000 <= wert <= 100000:
                bestand[gws_bezugsjahr] = wert
        except ValueError:
            continue
    return bestand


def _bestand_nach_zimmer(bfs_nr: str = "2937") -> dict:
    """Holt den Wohnungsbestand aufgeschluesselt nach Zimmerzahl aus dem
    GWS-Datenfluss REG5. Ermoeglicht die Leerstandsquote je Wohnungsgroesse,
    also die Frage: bei welcher Groesse steht anteilig am meisten leer?
    Gibt {zimmer_label: {jahr: bestand}} zurueck; bei Misserfolg {}."""
    import re as _re
    zimmer_namen = {"1": "1 Zimmer", "2": "2 Zimmer", "3": "3 Zimmer",
                    "4": "4 Zimmer", "5": "5 Zimmer", "6": "6+ Zimmer"}

    def aus_zeilen(zeilen, zimmer_dim):
        """Baut aus CSV-Zeilen {label: {jahr: bestand}}; nur Zeilen, bei
        denen alle uebrigen Aufschluesselungen Total sind."""
        if not zeilen:
            return {}
        ignor = {"TIME_PERIOD", "OBS_VALUE", "STRUCTURE", "STRUCTURE_ID",
                 "STRUCTURE_NAME", "ACTION", "FREQ", "DECIMALS", "OBS_STATUS"}
        spalten = [k for k in zeilen[0].keys()
                   if k and k.upper() == k and k not in ignor
                   and not k.startswith("DIFF")]
        andere = [k for k in spalten
                  if k != zimmer_dim and k.upper() not in _GEMEINDE_DIMS]
        ergebnis = {}
        for z in zeilen:
            if any((z.get(k) or "").strip() not in ("_T", "", "T")
                   for k in andere):
                continue
            zc = (z.get(zimmer_dim) or "").strip()
            if zc not in zimmer_namen:
                continue
            jahr = (z.get("TIME_PERIOD") or "").strip()[:4]
            wert = (z.get("OBS_VALUE") or "").strip()
            if not (_re_jahr(jahr) and wert):
                continue
            try:
                v = int(round(float(wert)))
            except ValueError:
                continue
            if 0 <= v <= 100000:
                ergebnis.setdefault(zimmer_namen[zc], {})[jahr] = v
        return ergebnis

    # --- Stufe 1: dokumentiertes REG5-Muster, Zimmerdimension per Name ----
    for muster in (f"A....{bfs_nr}", f"A...{bfs_nr}", f"A.....{bfs_nr}"):
        url = (f"{SDMX_BASIS}/CH1.GWS,DF_GWS_REG5,1.0.0/{muster}"
               f"?dimensionAtObservation=AllDimensions")
        try:
            kopf = dict(HEADERS)
            kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
            r = requests.get(url, headers=kopf, timeout=90)
            if r.status_code >= 400 or len(r.content) < 80:
                continue
            import csv as _csv
            import io as _io
            zeilen = list(_csv.DictReader(
                _io.StringIO(r.content.decode("utf-8-sig", "replace"))))
            if not zeilen:
                continue
            # Zimmerspalte am Namen erkennen (WAZIMS o. Ae.)
            zdim = None
            for k in zeilen[0].keys():
                if k and ("ZIM" in k.upper() or "ROOM" in k.upper()):
                    zdim = k
                    break
            if not zdim:
                continue
            ergebnis = aus_zeilen(zeilen, zdim)
            if len(ergebnis) >= 3:
                return ergebnis
        except Exception:
            continue

    # --- Stufe 2: Struktur analysieren (Rueckfallebene) -------------------
    try:
        rreg = requests.get(
            "https://disseminate.stats.swiss/rest/dataflow/CH1.GWS",
            headers=HEADERS, timeout=60)
        rreg.raise_for_status()
        reg_txt = rreg.content.decode("utf-8", "replace")
    except Exception:
        return {}

    df_zu_dsd = {}
    for m in _re.finditer(
            r'<(?:structure:)?Dataflow[^>]*\bid="(DF_GWS_[^"]+)"(.*?)'
            r'</(?:structure:)?Dataflow>', reg_txt, _re.DOTALL):
        ref = _re.search(r'\bid="(DSD_GWS_[^"]+)"', m.group(2))
        if ref:
            df_zu_dsd[m.group(1)] = ref.group(1)
    if not df_zu_dsd:
        for dfid in _re.findall(r'\bid="(DF_GWS_REG\d)"', reg_txt):
            df_zu_dsd[dfid] = dfid.replace("DF_", "DSD_")

    # Zimmer-Codes wie in der Leerwohnungsquelle
    zimmer_namen = {"1": "1 Zimmer", "2": "2 Zimmer", "3": "3 Zimmer",
                    "4": "4 Zimmer", "5": "5 Zimmer", "6": "6+ Zimmer"}
    bevorzugt = "DF_GWS_REG5"
    reihenfolge = ([bevorzugt] if bevorzugt in df_zu_dsd else []) + \
                  [d for d in df_zu_dsd if d != bevorzugt]
    for dfid in reihenfolge:
        try:
            dims = _sdmx_struktur_dimensionen(df_zu_dsd[dfid])
        except Exception:
            continue
        region_pos = None
        zimmer_dim = None
        for i, d in enumerate(dims):
            du = d.upper()
            if du in _GEMEINDE_DIMS and region_pos is None:
                region_pos = i
            if ("ZIM" in du or "WAZIM" in du or "ROOM" in du) and zimmer_dim is None:
                zimmer_dim = d
        if region_pos is None or zimmer_dim is None:
            continue
        teile = ["" for _ in dims]
        teile[region_pos] = str(bfs_nr)
        if "FREQ" in dims:
            teile[dims.index("FREQ")] = "A"
        url = (f"{SDMX_BASIS}/CH1.GWS,{dfid},1.0.0/{'.'.join(teile)}"
               f"?dimensionAtObservation=AllDimensions")
        try:
            kopf = dict(HEADERS)
            kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
            r = requests.get(url, headers=kopf, timeout=90)
            if r.status_code >= 400 or len(r.content) < 80:
                continue
            import csv as _csv
            import io as _io
            zeilen = list(_csv.DictReader(
                _io.StringIO(r.content.decode("utf-8-sig", "replace"))))
        except Exception:
            continue

        # Aufschluesselungs-Spalten ausser Zimmer muessen Total sein,
        # sonst wuerde nach Bauperiode/Kategorie mehrfach gezaehlt.
        ignor = {"TIME_PERIOD", "OBS_VALUE", "STRUCTURE", "STRUCTURE_ID",
                 "STRUCTURE_NAME", "ACTION", "FREQ", "DECIMALS", "OBS_STATUS"}
        spalten = [k for k in (zeilen[0].keys() if zeilen else [])
                   if k and k.upper() == k and k not in ignor
                   and not k.startswith("DIFF")]
        andere = [k for k in spalten
                  if k != zimmer_dim and k.upper() not in _GEMEINDE_DIMS]

        ergebnis = {}
        for z in zeilen:
            if any((z.get(k) or "").strip() not in ("_T", "", "T")
                   for k in andere):
                continue
            zc = (z.get(zimmer_dim) or "").strip()
            if zc not in zimmer_namen:
                continue
            jahr = (z.get("TIME_PERIOD") or "").strip()[:4]
            wert = (z.get("OBS_VALUE") or "").strip()
            if not (_re_jahr(jahr) and wert):
                continue
            try:
                v = int(round(float(wert)))
            except ValueError:
                continue
            if 0 <= v <= 100000:
                ergebnis.setdefault(zimmer_namen[zc], {})[jahr] = v
        if len(ergebnis) >= 3:
            return ergebnis
    return {}


def _leerwohnungsziffer(region_begriff: str = "neuhausen am rheinfall",
                        bfs_nr: str = "2937") -> tuple:
    """Holt die Leerwohnungsdaten aus der offiziellen SDMX-API der
    Bundesstatistik (data.stats.swiss). Liefert die absolute Anzahl leer
    stehender Wohnungen je Jahr; die Leerwohnungsziffer in Prozent wird mit
    dem Gesamtbestand aus der GWS-Quelle berechnet. Zusaetzlich die
    Aufschluesselung nach Zimmerzahl fuer das neueste Jahr.

    Gibt (reihe, url, extra) zurueck:
      reihe: [(jahr, ziffer_prozent_oder_None), ...] wenn Bestand vorhanden,
             sonst [(jahr, anzahl), ...]
      extra: dict mit anzahl_reihe, bestand_reihe, zimmer_verteilung, ist_ziffer
    Selbstvalidierend: nur plausible Jahre 1990-2100 und nicht-negative Werte."""
    # 1) Alle Leerwohnungsdaten holen (leere Dimensionen = alle Auspraegungen,
    #    wie im funktionierenden Original-Aufruf 2937...V.A). Wir filtern die
    #    Totale dann im Parser heraus. Schluessel-Dimensionen in Reihenfolge:
    #    GR_KT_GDE . WOHN_ANZAHL . LEERWOHN_TYP . MEASURE_DIMENSION . FREQ
    zeilen, url = _sdmx_csv(SDMX_LEERWOHNUNG, f"{bfs_nr}...V.A")
    anzahl = {}
    for z in zeilen:
        jahr = (z.get("TIME_PERIOD") or "").strip()
        wert = (z.get("OBS_VALUE") or "").strip()
        zc = (z.get("WOHN_ANZAHL") or "_T").strip()
        typ = (z.get("LEERWOHN_TYP") or "_T").strip()
        # Nur die Gesamtsumme (alle Zimmer, alle Typen)
        if zc not in ("_T", "", "T") or typ not in ("_T", "", "T"):
            continue
        if jahr and wert:
            try:
                j = int(jahr)
                v = int(round(float(wert)))
                if 1990 <= j <= 2100 and v >= 0:
                    anzahl[jahr] = v
            except ValueError:
                continue
    if len(anzahl) < 3:
        raise RuntimeError(f"zu wenige Leerwohnungs-Werte aus SDMX ({len(anzahl)})")

    # Neueste Jahre, die im BFS-Dataflow noch fehlen (Publikationsverzug),
    # aus der amtlichen Gemeinde-Meldung ergaenzen. Gleiche Quelle wie der
    # Bestand; ueberschreibt keine vorhandenen API-Werte.
    try:
        anzahl_gem = _anzahl_aus_gemeinde()
        for jahr, wert in anzahl_gem.items():
            anzahl.setdefault(jahr, wert)
    except Exception:
        pass

    # 2) Aufschluesselung nach Zimmerzahl fuer das neueste Jahr aus denselben
    #    bereits geladenen Zeilen (Typ Total, einzelne Zimmerkategorien).
    neuestes = max(anzahl)
    zimmer_namen = {"1": "1 Zimmer", "2": "2 Zimmer", "3": "3 Zimmer",
                    "4": "4 Zimmer", "5": "5 Zimmer", "6": "6+ Zimmer"}
    zimmer_reihenfolge = ["1 Zimmer", "2 Zimmer", "3 Zimmer",
                          "4 Zimmer", "5 Zimmer", "6+ Zimmer"]
    verteilung = {}          # neuestes Jahr MIT Zimmerdaten: {name: anzahl}
    verteilung_jahre = {}    # alle Jahre: {name: {jahr: anzahl}}
    for z in zeilen:
        jahr = (z.get("TIME_PERIOD") or "").strip()
        zc = (z.get("WOHN_ANZAHL") or "").strip()
        typ = (z.get("LEERWOHN_TYP") or "_T").strip()
        wert = (z.get("OBS_VALUE") or "").strip()
        # Nur Typ-Total-Zeilen, sonst zaehlen Typ-Unterkategorien mit
        if typ in ("_T", "", "T") and zc in zimmer_namen and wert:
            try:
                v = int(round(float(wert)))
            except ValueError:
                continue
            verteilung_jahre.setdefault(zimmer_namen[zc], {})[jahr] = v

    # Das neueste Jahr der ZIMMERDATEN bestimmen. Es kann aelter sein als das
    # neueste Gesamtjahr, weil dieses aus der Gemeinde-Meldung ergaenzt wird,
    # die keine Aufschluesselung nach Zimmerzahl enthaelt.
    zimmer_jahre = set()
    for jahre_dict in verteilung_jahre.values():
        zimmer_jahre.update(jahre_dict.keys())
    neuestes_zimmer = max(zimmer_jahre) if zimmer_jahre else None
    if neuestes_zimmer:
        for name, jahre_dict in verteilung_jahre.items():
            if neuestes_zimmer in jahre_dict:
                verteilung[name] = jahre_dict[neuestes_zimmer]
    # Aufsteigend nach Zimmerzahl ordnen (1,2,3,4,5,6+)
    verteilung = {n: verteilung[n] for n in zimmer_reihenfolge
                  if n in verteilung}

    # 3) Gesamtbestand fuer die Prozent-Ziffer (Quelle wird automatisch gesucht)
    bestand, bestand_df = _leerwohnung_bestand(bfs_nr)
    # Neueste Jahre (GWS oft ein Jahr im Rueckstand) aus der amtlichen
    # Gemeinde-Meldung ergaenzen, ohne vorhandene GWS-Werte zu ueberschreiben.
    try:
        best_gem = _bestand_aus_gemeinde()
        for jahr, wert in best_gem.items():
            bestand.setdefault(jahr, wert)
    except Exception:
        pass

    # 4) Reihe bauen: Ziffer in % wenn Bestand vorhanden, sonst Anzahl.
    # METHODIK (BFS): Die Leerwohnungsziffer der Zaehlung vom 1. Juni eines
    # Jahres wird mit dem Wohnungsbestand am ENDE DES VORJAHRES berechnet
    # (GWS-Bestand des Vorjahres). Das BFS nennt die Zaehlung 2025 daher
    # ausdruecklich "2025, Basis GWS 2024". Der Bestand ist im dict unter
    # seinem GWS-Bezugsjahr abgelegt; fuer die Zaehlung 'jahr' brauchen wir
    # also bestand[jahr-1].
    anzahl_reihe = sorted((j, v) for j, v in anzahl.items())
    ist_ziffer = bool(bestand)
    genaehert = []
    if ist_ziffer:
        bestand_jahre = sorted(int(j) for j in bestand)
        reihe = []
        for jahr, leer in anzahl_reihe:
            vorjahr = str(int(jahr) - 1)
            best = bestand.get(vorjahr)
            if not best and bestand_jahre:
                # Kein exakter Vorjahresbestand: naechstgelegenes Jahr nehmen.
                # Der Wohnungsbestand aendert sich langsam, daher ist das eine
                # vertretbare Naeherung. Wir merken uns diese Jahre und weisen
                # sie im Kartenhinweis aus, statt die Reihe zu zerloechern.
                ziel = int(vorjahr)
                naechstes = min(bestand_jahre, key=lambda j: abs(j - ziel))
                # Nur nutzen, wenn nicht weiter als 4 Jahre entfernt
                if abs(naechstes - ziel) <= 4:
                    best = bestand[str(naechstes)]
                    genaehert.append(jahr)
            if best and best > 0:
                reihe.append((jahr, round(leer / best * 100, 2)))
        # Falls fuer zu wenige Jahre ein Bestand vorliegt, auf Anzahl
        if len(reihe) < 3:
            ist_ziffer = False
            reihe = anzahl_reihe
            genaehert = []
    else:
        reihe = anzahl_reihe

    # 5) Leerstandsquote je Zimmergroesse: zeigt, bei welcher Wohnungsgroesse
    #    anteilig am meisten leer steht. Aussagekraeftiger fuer einen Mangel
    #    als die blosse Anzahl, da es von jeder Groesse unterschiedlich viele
    #    Wohnungen gibt. Nur wenn der Bestand nach Zimmerzahl verfuegbar ist.
    quote_je_zimmer = {}
    try:
        bestand_zimmer = _bestand_nach_zimmer(bfs_nr)
        for label, jahre_bestand in bestand_zimmer.items():
            leer_jahre = verteilung_jahre.get(label, {})
            for jahr, leer in leer_jahre.items():
                # Bestand des Vorjahres (gleiche Methodik wie Gesamtziffer)
                best = jahre_bestand.get(str(int(jahr) - 1))
                if best and best > 0:
                    quote_je_zimmer.setdefault(label, {})[jahr] = \
                        round(leer / best * 100, 2)
    except Exception:
        quote_je_zimmer = {}

    extra = {
        "anzahl_reihe": anzahl_reihe,
        "bestand_reihe": sorted(bestand.items()),
        "bestand_quelle": bestand_df,
        "zimmer_verteilung": verteilung,
        "zimmer_verteilung_jahre": verteilung_jahre,
        "quote_je_zimmer": quote_je_zimmer,
        "neuestes_jahr": neuestes,
        "neuestes_zimmer_jahr": neuestes_zimmer,
        "genaeherte_jahre": genaehert,
        "ist_ziffer": ist_ziffer,
    }
    return reihe, url, extra


def _sdmx_struktur_dims_codelisten(agency_dataflow: str, version: str = "") -> tuple:
    """Laedt eine SDMX-Struktur (v2-Endpunkt) und gibt (dimensionen, codelisten)
    zurueck. dimensionen: Liste {id, position, codelist}. codelisten:
    {codelist_id: {code: name}}. Nach dem Vorbild des BFS-Recherchepakets."""
    import xml.etree.ElementTree as ET
    agency, dataflow = agency_dataflow.split(",", 1) if "," in agency_dataflow \
        else (agency_dataflow, "")
    ver = version or "+"
    url = (f"https://disseminate.stats.swiss/rest/v2/structure/dataflow/"
           f"{agency}/{dataflow}/{ver}?references=all&detail=referencepartial")
    r = requests.get(url, headers=HEADERS, timeout=90)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"s": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure",
          "c": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"}
    codelisten = {}
    for cl in root.findall(".//s:Codelist", ns):
        clid = cl.attrib.get("id", "")
        codes = {}
        for code in cl.findall("s:Code", ns):
            cid = code.attrib.get("id", "")
            namen = code.findall("c:Name", ns)
            gewaehlt = ""
            for nm in namen:
                lang = nm.attrib.get(
                    "{http://www.w3.org/XML/1998/namespace}lang", "")
                if lang in ("de", "de-CH"):
                    gewaehlt = nm.text or ""
                    break
            if not gewaehlt and namen:
                gewaehlt = namen[0].text or ""
            codes[cid] = gewaehlt
        if clid:
            codelisten[clid] = codes
    strukturen = root.findall(".//s:DataStructure", ns)
    if not strukturen:
        raise RuntimeError("keine DataStructure gefunden")
    ds = strukturen[0]
    dims = []
    for dim in ds.findall(
            ".//s:DataStructureComponents/s:DimensionList/s:Dimension", ns):
        ref = dim.find(".//c:Enumeration/c:Ref", ns)
        dims.append({
            "id": dim.attrib.get("id", ""),
            "position": int(dim.attrib.get("position", "999")),
            "codelist": ref.attrib.get("id", "") if ref is not None else "",
        })
    dims.sort(key=lambda d: d["position"])
    return dims, codelisten


def _sdmx_gemeinde_schluessel(dims, codelisten, bfs_nr="2937",
                              mess_dim_wert=None):
    """Baut den SDMX-Datenschluessel: an der Gemeindedimension den Gemeinde-
    Code, an FREQ ein 'A', sonst leer. Findet den Code flexibel: exakte
    BFS-Nr, ein Code der die BFS-Nr enthaelt, oder ein Code dessen Name
    'Neuhausen' nennt. Gibt (schluessel, geo_dim_id, gemeinde_code)."""
    def norm(s):
        return re.sub(r"\s+", " ", (s or "").casefold()).strip()
    geo_dim = None
    gemeinde_code = bfs_nr
    # Stufe 1: Dimension, deren Codeliste Neuhausen kennt
    for dim in dims:
        codes = codelisten.get(dim["codelist"], {})
        # exakter Code
        if bfs_nr in codes and "neuhausen" in norm(codes.get(bfs_nr, "")):
            geo_dim, gemeinde_code = dim["id"], bfs_nr
            break
        # Code, der die BFS-Nr enthaelt, oder Name nennt Neuhausen
        for cid, cname in codes.items():
            if (bfs_nr in cid and cid != bfs_nr) or "neuhausen" in norm(cname):
                geo_dim, gemeinde_code = dim["id"], cid
                break
        if geo_dim:
            break
    if geo_dim is None:
        for dim in dims:
            if any(t in norm(dim["id"]) for t in
                   ("geo", "municip", "commune", "city", "region", "gde",
                    "raum", "gemeinde", "kt_gde", "space", "spatial")):
                geo_dim = dim["id"]
                break
    if geo_dim is None:
        raise RuntimeError("Gemeindedimension nicht bestimmbar")
    teile = []
    for dim in dims:
        if dim["id"] == geo_dim:
            teile.append(gemeinde_code)
        elif "freq" in norm(dim["id"]):
            teile.append("A")
        else:
            teile.append("")
    return ".".join(teile), geo_dim, gemeinde_code


def _netto_mietzins(bfs_nr="2937"):
    """Holt den durchschnittlichen monatlichen Netto-Mietzins pro m2 fuer
    Neuhausen aus dem City-Statistics-Datenfluss. Gibt (reihe, url, guete)
    zurueck. guete: Anteil verwertbarer Jahre. WICHTIG: kein Median.
    Selbstvalidierend: nur plausible Werte (1-100 CHF/m2), Unterdrueckungen
    ('X', '...', leere) werden uebersprungen, nie geschaetzt."""
    dims, codelisten = _sdmx_struktur_dims_codelisten(SDMX_CITYSTAT)
    schluessel, geo, gcode = _sdmx_gemeinde_schluessel(dims, codelisten, bfs_nr)
    url = (f"{SDMX_BASIS}/{SDMX_CITYSTAT}/{schluessel}"
           f"?dimensionAtObservation=AllDimensions&format=csvfile"
           f"&startPeriod=2016&endPeriod=2024")
    kopf = dict(HEADERS)
    kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
    r = requests.get(url, headers=kopf, timeout=90)
    r.raise_for_status()
    import csv as _csv
    import io as _io
    text = r.content.decode("utf-8-sig", "replace")
    zeilen = list(_csv.DictReader(_io.StringIO(text)))
    # Mietzins-Zeilen: Messgroesse enthaelt "netto" und "miet"
    def norm(s):
        return (s or "").casefold()
    werte = {}
    roh_jahre = 0
    for z in zeilen:
        blob = norm(" ".join(str(v) for v in z.values()))
        if "miet" not in blob or "netto" not in blob:
            continue
        jahr = (z.get("TIME_PERIOD") or "").strip()[:4]
        wert = (z.get("OBS_VALUE") or "").strip()
        if not re.match(r"^(19|20)\d{2}$", jahr):
            continue
        roh_jahre += 1
        # Unterdrueckungen / unzuverlaessige Werte ueberspringen
        if wert in ("", "X", "...", "*") or any(
                s in wert for s in ("(", ")", "X")):
            continue
        try:
            v = float(wert.replace(",", "."))
        except ValueError:
            continue
        if 1 <= v <= 100:
            werte[jahr] = round(v, 2)
    reihe = sorted(werte.items())
    guete = len(reihe) / roh_jahre if roh_jahre else 0.0
    return reihe, url, guete


def diagnose_mietzins():
    """Prueft, ob der City-Statistics-Netto-Mietzins fuer Neuhausen
    verwertbare Werte liefert. Aufruf: --diagnose-mietzins"""
    print("===== Netto-Mietzins-Diagnose (City Statistics) =====\n")

    # 1) Struktur roh ansehen: welche Namensraeume und Codelisten kommen?
    print("--- Strukturabfrage (roh) ---")
    struktur_url = ("https://disseminate.stats.swiss/rest/v2/structure/"
                    "dataflow/CH1.CITYSTAT/DF_CITYSTAT_CHURB_2/+"
                    "?references=all&detail=referencepartial")
    try:
        r = requests.get(struktur_url, headers=HEADERS, timeout=90)
        print(f"  HTTP {r.status_code}, {len(r.content):,} Bytes")
        txt = r.content.decode("utf-8", "replace")
        import re as _re
        # Welche Namensraeume nutzt die Antwort?
        ns_treffer = sorted(set(_re.findall(r'xmlns:(\w+)="([^"]+)"', txt)))
        for praefix, url_ns in ns_treffer[:8]:
            print(f"  Namensraum {praefix}: {url_ns[:70]}")
        # Wie viele Codelist- und Code-Elemente gibt es ueberhaupt?
        print(f"  'Codelist'-Vorkommen: {txt.count('Codelist')}")
        print(f"  'Code '-Vorkommen:    {txt.count('<Code ') + txt.count(':Code ')}")
        # Kommt Neuhausen im Strukturtext vor?
        if "euhausen" in txt:
            for m in _re.finditer(r'.{80}[Nn]euhausen.{80}', txt):
                print(f"  NEUHAUSEN gefunden: ...{m.group(0)[:170]}...")
                break
        else:
            print("  Neuhausen kommt in der Struktur NICHT vor.")
    except Exception as e:
        print(f"  FEHLER: {e}")

    # 2) Datenabfrage ohne Regionsfilter: welche Regionen liefert der Wuerfel?
    print("\n--- Datenabfrage fuer Neuhausen (alle Indikatoren) ---")
    url = (f"{SDMX_BASIS}/{SDMX_CITYSTAT}/.2937..A"
           f"?dimensionAtObservation=AllDimensions&format=csvfile")
    try:
        kopf = dict(HEADERS)
        kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
        r = requests.get(url, headers=kopf, timeout=120)
        print(f"  HTTP {r.status_code}, {len(r.content):,} Bytes")
        if r.status_code < 400 and len(r.content) > 100:
            import csv as _csv
            import io as _io
            text = r.content.decode("utf-8-sig", "replace")
            zeilen = list(_csv.DictReader(_io.StringIO(text)))
            print(f"  Zeilen: {len(zeilen)}")
            if zeilen:
                print(f"  Spalten: {list(zeilen[0].keys())[:12]}")
                # Welche Raum-Codes gibt es?
                raum_spalte = None
                for k in zeilen[0].keys():
                    if k and "RAUM" in k.upper():
                        raum_spalte = k
                        break
                if raum_spalte:
                    codes = {}
                    for z in zeilen:
                        c = (z.get(raum_spalte) or "").strip()
                        if c and c not in codes:
                            # Klartextspalte suchen
                            name = ""
                            for k, v in z.items():
                                if k and k != raum_spalte and v and \
                                        not k.isupper() and len(str(v)) > 3:
                                    name = str(v)[:40]
                                    break
                            codes[c] = name
                    print(f"  Verschiedene Raum-Codes: {len(codes)}")
                    print(f"  Erste 12: {list(codes.items())[:12]}")
                    if "2937" in codes:
                        print(f"  -> 2937 VORHANDEN: {codes['2937']}")
                    else:
                        print("  -> 2937 NICHT vorhanden!")
                        # Neuhausen per Name suchen
                        for c, n in codes.items():
                            if "euhausen" in n:
                                print(f"     Aber Neuhausen unter Code {c}: {n}")
                # Mietzins-Zeilen?
                miet = [z for z in zeilen
                        if "miet" in " ".join(str(v) for v in z.values()).lower()]
                print(f"  Zeilen mit 'miet': {len(miet)}")
                for z in miet[:3]:
                    print(f"    {dict(list(z.items())[:8])}")
    except Exception as e:
        print(f"  FEHLER: {e}")

    # 2) Indikator-Codes aus der Struktur: welcher steht fuer den Mietzins?
    print("\n--- Suche Mietzins-Indikator in den Codelisten ---")
    miet_codes = {}
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(struktur_url, headers=HEADERS, timeout=120)
        root = ET.fromstring(r.content)
        ns = {"s": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure",
              "c": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"}
        raum_treffer = {}
        for cl in root.findall(".//s:Codelist", ns):
            clid = cl.attrib.get("id", "")
            for code in cl.findall("s:Code", ns):
                cid = code.attrib.get("id", "")
                namen = [(nm.attrib.get(
                    "{http://www.w3.org/XML/1998/namespace}lang", ""),
                    nm.text or "") for nm in code.findall("c:Name", ns)]
                deutsch = ""
                for lang, txt_n in namen:
                    if lang in ("de", "de-CH"):
                        deutsch = txt_n
                        break
                if not deutsch and namen:
                    deutsch = namen[0][1]
                dl = deutsch.casefold()
                if "miet" in dl or "loyer" in dl:
                    miet_codes[cid] = (clid, deutsch)
                if cid == "2937":
                    raum_treffer[clid] = deutsch
        print(f"  Mietbezogene Codes gefunden: {len(miet_codes)}")
        for cid, (clid, name) in list(miet_codes.items())[:10]:
            print(f"    {cid} ({clid}): {name[:70]}")
        if raum_treffer:
            for clid, name in raum_treffer.items():
                print(f"  Code 2937 in {clid}: {name}")
        else:
            print("  Code 2937 in keiner Codeliste gefunden!")
    except Exception as e:
        print(f"  FEHLER: {e}")

    # 3) Gezielt pruefen: hat Neuhausen Werte fuer diese Indikatoren?
    if miet_codes:
        print("\n--- Werte fuer Neuhausen (2937) je Miet-Indikator ---")
        for cid in list(miet_codes)[:6]:
            url = (f"{SDMX_BASIS}/{SDMX_CITYSTAT}/{cid}.2937..A"
                   f"?dimensionAtObservation=AllDimensions&format=csvfile")
            try:
                kopf = dict(HEADERS)
                kopf["Accept"] = "application/vnd.sdmx.data+csv; charset=utf-8"
                r = requests.get(url, headers=kopf, timeout=90)
                if r.status_code >= 400 or len(r.content) < 100:
                    print(f"  {cid}: HTTP {r.status_code}, keine Daten")
                    continue
                import csv as _csv
                import io as _io
                zeilen = list(_csv.DictReader(
                    _io.StringIO(r.content.decode("utf-8-sig", "replace"))))
                werte = [(z.get("TIME_PERIOD"), z.get("OBS_VALUE"))
                         for z in zeilen if z.get("OBS_VALUE")]
                print(f"  {cid} ({miet_codes[cid][1][:40]}): "
                      f"{len(werte)} Werte")
                for jahr, wert in sorted(werte)[-6:]:
                    print(f"       {jahr}: {wert}")
            except Exception as e:
                print(f"  {cid}: FEHLER {str(e)[:50]}")

    print("\n===== Ende Mietzins-Diagnose =====")


def diagnose_leerwohnung():
    """Testet die Leerwohnungsdaten aus der SDMX-API.
    Aufruf: python neuhausen_daten.py --diagnose-leerwohnung"""
    print("===== Leerwohnungs-Diagnose (SDMX-API data.stats.swiss) =====\n")
    print("--- Leerwohnungen (CH1.LWZ) ---")
    try:
        reihe, url, extra = _leerwohnungsziffer()
        print(f"URL: {url}")
        print(f"Anzahl-Reihe: {len(extra['anzahl_reihe'])} Jahre, "
              f"{extra['anzahl_reihe'][0]} ... {extra['anzahl_reihe'][-1]}")
        print(f"Bestand vorhanden: {'ja' if extra['ist_ziffer'] else 'nein'}"
              f" ({len(extra['bestand_reihe'])} Jahre)")
        if extra["ist_ziffer"]:
            print(f"Ziffer-Reihe (%): {reihe[0]} ... {reihe[-1]}")
        print(f"Zimmerverteilung {extra.get('neuestes_zimmer_jahr')}: "
              f"{extra['zimmer_verteilung']}")

        # Klartext dazu, ob die Karte "Leerstand nach Wohnungsgroesse"
        # erzeugt wird und welche Variante.
        print("\n--- Karte 'Leerstand nach Wohnungsgroesse' ---")
        vert = extra.get("zimmer_verteilung") or {}
        quote = extra.get("quote_je_zimmer") or {}
        nz = extra.get("neuestes_zimmer_jahr")
        print(f"  Neuestes Gesamtjahr:      {extra.get('neuestes_jahr')}")
        print(f"  Neuestes Zimmerdatenjahr: {nz}")
        print(f"  Zimmerkategorien mit Daten: {len(vert)}")
        print(f"  Kategorien mit Quote:       {len(quote)}")
        quote_aktuell = {k: v[nz] for k, v in quote.items() if nz in v}
        if len(quote_aktuell) >= 3:
            print(f"  -> KARTE MIT QUOTE wird erzeugt "
                  f"({len(quote_aktuell)} Kategorien)")
            for k in sorted(quote_aktuell):
                print(f"       {k}: {quote_aktuell[k]}%")
        elif vert and sum(vert.values()) > 0:
            print(f"  -> KARTE MIT ANZAHL wird erzeugt "
                  f"({len(vert)} Kategorien, ohne Quote)")
            for k, v in vert.items():
                print(f"       {k}: {v}")
        else:
            print("  -> KEINE KARTE! Grund: Zimmerverteilung ist leer.")
            print("     Pruefen: liefert die API WOHN_ANZAHL-Zeilen mit "
                  "LEERWOHN_TYP='_T'?")
    except Exception as e:
        print(f"FEHLGESCHLAGEN: {e}")

    print("\n--- Gesamtwohnungsbestand (automatische Quellensuche) ---")
    try:
        bestand, df = _leerwohnung_bestand("2937")
        if bestand:
            js = sorted(bestand.items())
            print(f"  Quelle gefunden: {df}")
            print(f"  Bestand: {len(js)} Jahre, {js[0]} ... {js[-1]}")
            # Beispiel-Ziffer fuer das neueste gemeinsame Jahr
            try:
                reihe, _, extra = _leerwohnungsziffer()
                if extra.get("ist_ziffer"):
                    print(f"  Berechnete Ziffer (neuestes Jahr): {reihe[-1]}")
            except Exception:
                pass
        else:
            print("  Keine gemeindefaehige Bestand-Quelle gefunden.")
    except Exception as e:
        print(f"  Bestand-FEHLER: {e}")
    print("\n===== Ende Leerwohnungs-Diagnose =====")



def baue_kennzahlen() -> None:
    # Zwischenspeicher: hoechstens einmal pro Woche neu abfragen
    if KENNZAHLEN_AUSGABE.exists():
        try:
            roh = KENNZAHLEN_AUSGABE.read_text(encoding="utf-8")
            alt = json.loads(roh[roh.find("{"):roh.rfind("}") + 1])
            heute = int(datetime.now(_ZEITZONE).strftime("%Y%m%d")
                        if _ZEITZONE else datetime.now().strftime("%Y%m%d"))
            if (alt.get("naechste_pruefung", 0) > heute
                    and alt.get("bereiche")
                    and alt.get("version", 1) == KENNZAHLEN_VERSION):
                print("  Kennzahlen: Zwischenspeicher aktuell "
                      f"(naechste Pruefung ab {alt['naechste_pruefung']})")
                return
        except Exception:
            pass

    bereiche = []
    fehler = []

    def karte(bereich_titel, name, einheit, reihe, quelle, quelle_url,
              hinweis="", extra=None):
        for b in bereiche:
            if b["titel"] == bereich_titel:
                ziel = b
                break
        else:
            ziel = {"titel": bereich_titel, "karten": []}
            bereiche.append(ziel)
        eintrag = {
            "name": name, "einheit": einheit,
            "reihe": [list(p) for p in reihe],
            "quelle": quelle, "quelleUrl": quelle_url, "hinweis": hinweis,
        }
        if extra:
            eintrag.update(extra)
        ziel["karten"].append(eintrag)

    # --- Bevoelkerung (BFS, demografische Bilanz je Gemeinde) ---
    # Der Wuerfel schluesselt nach Staatsangehoerigkeit (Einzellaender + Total)
    # und Geschlecht auf. Wir erzwingen ueberall das Total, sonst werden
    # Kategorien addiert und die Zahl zu hoch (frueher 11'834 statt ~10'500).
    BEV_FEST = {
        "Staatsangehörigkeit (Kategorie)": ["staatsangehörigkeit (kategorie) - total"],
        "Geschlecht": ["geschlecht - total"],
        "Demografische Komponente": ["bestand am 31. dezember"],
    }
    try:
        reihe, url = _stattab_reihe(
            "px-x-0102020000_201", [], fest=BEV_FEST)
        karte("Bevölkerung", "Ständige Wohnbevölkerung", "Personen",
              reihe, "BFS, STAT-TAB", url)
        print(f"  Kennzahlen: Bevoelkerung {reihe[0][0]}\u2013{reihe[-1][0]} "
              f"({len(reihe)} Werte, aktuell {reihe[-1][1]})")
    except Exception as e:
        fehler.append(f"Bevoelkerung: {e}")

    # --- Beschaeftigung (BFS, STATENT je Gemeinde) ---
    for name, begriff in (("Beschäftigte", "beschäftigte"),
                          ("Arbeitsstätten", "arbeitsstätten")):
        try:
            reihe, url = _stattab_reihe("px-x-0602010000_102", [begriff])
            karte("Beschäftigung", name, name, reihe, "BFS, STAT-TAB", url)
            print(f"  Kennzahlen: {name} {reihe[0][0]}\u2013{reihe[-1][0]} "
                  f"({len(reihe)} Werte)")
        except Exception as e:
            fehler.append(f"{name}: {e}")

    # --- Ausländer:innenanteil (BFS, demografische Bilanz) ---
    # Derselbe Wuerfel wie die Bevoelkerung: er kennt die Kategorie "Ausland"
    # direkt. Anteil = Ausland / Gesamt, beide "Bestand am 31. Dezember",
    # Geschlecht-Total. Ergibt die amtlichen ~46 % (Gemeinde-PDF: 45.9 %).
    try:
        gem = {"Geschlecht": ["geschlecht - total"],
               "Demografische Komponente": ["bestand am 31. dezember"]}
        sak = "Staatsangehörigkeit (Kategorie)"
        ausland, url = _stattab_reihe(
            "px-x-0102020000_201", [],
            fest={**gem, sak: ["ausland"]})
        gesamt, _ = _stattab_reihe(
            "px-x-0102020000_201", [],
            fest={**gem, sak: ["staatsangehörigkeit (kategorie) - total"]})
        tot = dict(gesamt)
        reihe = []
        for jahr, aus in ausland:
            g = tot.get(jahr)
            if g and aus is not None:
                reihe.append((jahr, round(aus / g * 100, 1)))
        if reihe:
            karte("Bevölkerung", "Ausländer:innenanteil", "%",
                  reihe, "BFS, STAT-TAB", url)
            print(f"  Kennzahlen: Auslaenderanteil {reihe[0][0]}\u2013"
                  f"{reihe[-1][0]} ({len(reihe)} Werte, aktuell {reihe[-1][1]}%)")
    except Exception as e:
        fehler.append(f"Ausländer:innenanteil: {e}")

    # --- Wohnen (BFS: Neubau) ---
    # Hinweis: Die Leerwohnungsziffer (Wuerfel _101) wird weiter unten ueber
    # einen direkten, robusten Abruf geholt.
    try:
        # Aktueller Wuerfel _107 (bis 2023). Der alte _103 endete 2012.
        reihe, url = _stattab_reihe(
            "px-x-0904030000_107", [],
            fest={"Gebäudetyp": ["gebäudetyp - alle", "gebäudetyp - total"]})
        karte("Wohnen", "Neu erstellte Wohnungen", "Wohnungen",
              reihe, "BFS, STAT-TAB", url,
              extra={"jahresereignis": True})
        print(f"  Kennzahlen: Neubau-Wohnungen {reihe[0][0]}\u2013"
              f"{reihe[-1][0]} ({len(reihe)} Werte, aktuell {reihe[-1][1]})")
    except Exception as e:
        fehler.append(f"Neu erstellte Wohnungen: {e}")

    # --- Leerwohnungsziffer (BFS Leerwohnungszaehlung, Vollerhebung) ---
    # Robuster Direktabruf: probiert mehrere URL-Formen, verkraftet den
    # BFS-Platzhalter "..." (Daten nicht verfuegbar) und die verschachtelte
    # Jahrescodierung. Fuer kleine Gemeinden schwankt die Ziffer stark,
    # daher der Hinweis.
    try:
        reihe, url, extra = _leerwohnungsziffer()
        if reihe:
            ist_ziffer = extra.get("ist_ziffer")
            einheit = "%" if ist_ziffer else "Wohnungen"
            name = "Leerwohnungsziffer" if ist_ziffer else "Leer stehende Wohnungen"
            hinweis = ("Anteil leer stehender Wohnungen am Gesamtbestand "
                       "(Stichtag 1. Juni). Anzahl aus der Leerwohnungszählung, "
                       "Bestand aus der Gebäude- und Wohnungsstatistik. Bei "
                       "kleinen Gemeinden von Jahr zu Jahr stark schwankend."
                       if ist_ziffer else
                       "Anzahl leer stehender, am Markt angebotener Wohnungen "
                       "(Stichtag 1. Juni). Bei kleinen Gemeinden stark "
                       "schwankend.")
            gen = extra.get("genaeherte_jahre") or []
            if ist_ziffer and gen:
                hinweis += (f" Für {len(gen)} ältere Jahrgänge liegt kein "
                            f"exakter Vorjahresbestand vor; dort wurde der "
                            f"zeitlich nächstgelegene Bestand verwendet.")
            karte("Wohnen", name, einheit, reihe,
                  "BFS, Leerwohnungszählung (data.stats.swiss)", url,
                  hinweis=hinweis)
            print(f"  Kennzahlen: {name} {reihe[0][0]}\u2013"
                  f"{reihe[-1][0]} ({len(reihe)} Werte, aktuell {reihe[-1][1]}"
                  f"{'%' if ist_ziffer else ''})")

            # Gesamtwohnungsbestand als eigene Kennzahl
            bestand = extra.get("bestand_reihe") or []
            if len(bestand) >= 3:
                karte("Wohnen", "Wohnungsbestand", "Wohnungen",
                      bestand, "BFS, Gebäude- und Wohnungsstatistik",
                      url, hinweis="Gesamtzahl aller Wohnungen in der Gemeinde "
                                   "(Jahresende), inklusive der neu erstellten. "
                                   "Zeigt das Gesamtwachstum des Wohnraums.")
                print(f"  Kennzahlen: Wohnungsbestand {bestand[0][0]}\u2013"
                      f"{bestand[-1][0]} (aktuell {bestand[-1][1]})")

            # Leerstand nach Zimmerzahl (aufsteigend geordnet).
            # Bevorzugt die Leerstandsquote je Groesse (zeigt echten Mangel),
            # sonst die Anzahl leer stehender Wohnungen.
            verteilung = extra.get("zimmer_verteilung") or {}
            quote = extra.get("quote_je_zimmer") or {}
            # Das Jahr der Zimmerdaten kann aelter sein als das Gesamtjahr,
            # weil die Gemeinde-Meldung keine Aufschluesselung enthaelt.
            neuestes = (extra.get("neuestes_zimmer_jahr")
                        or extra.get("neuestes_jahr", ""))
            quote_aktuell = {}
            for label, jahre_q in quote.items():
                if neuestes in jahre_q:
                    quote_aktuell[label] = jahre_q[neuestes]
            # Falls fuer dieses Jahr keine Quote vorliegt (Bestand nach
            # Zimmerzahl hinkt evtl. nach), das neueste Jahr nehmen, fuer
            # das ueberhaupt Quotendaten existieren.
            if len(quote_aktuell) < 3 and quote:
                alle_q_jahre = set()
                for jahre_q in quote.values():
                    alle_q_jahre.update(jahre_q.keys())
                if alle_q_jahre:
                    ersatz = max(alle_q_jahre)
                    kandidat = {l: jq[ersatz] for l, jq in quote.items()
                                if ersatz in jq}
                    if len(kandidat) >= 3:
                        quote_aktuell = kandidat
                        neuestes = ersatz
            reihenfolge = ["1 Zimmer", "2 Zimmer", "3 Zimmer",
                           "4 Zimmer", "5 Zimmer", "6+ Zimmer"]
            if len(quote_aktuell) >= 3:
                reihe_v = [(l, quote_aktuell[l]) for l in reihenfolge
                           if l in quote_aktuell]
                karte("Wohnen", "Leerstand nach Wohnungsgrösse", "%",
                      reihe_v, "BFS, Leerwohnungszählung und GWS "
                               "(data.stats.swiss)", url,
                      hinweis="Anteil leer stehender Wohnungen am Bestand "
                              "der jeweiligen Grösse. Zeigt, bei welcher "
                              "Wohnungsgrösse anteilig am meisten leer steht.",
                      extra={"typ": "verteilung",
                             "verlauf_je_kategorie": quote,
                             "verlauf_einheit": "%",
                             "anteil_ist_wert": True,
                             "stand": neuestes})
                print(f"  Kennzahlen: Leerstandsquote nach Zimmerzahl "
                      f"({len(reihe_v)} Kategorien, Stand {neuestes})")
            elif verteilung and sum(verteilung.values()) > 0:
                reihe_v = [(k, v) for k, v in verteilung.items()]
                vj = extra.get("zimmer_verteilung_jahre") or {}
                karte("Wohnen", "Leerstand nach Wohnungsgrösse", "Wohnungen",
                      reihe_v, "BFS, Leerwohnungszählung (data.stats.swiss)",
                      url, hinweis="Anzahl leer stehender Wohnungen je "
                                   "Wohnungsgrösse am Stichtag 1. Juni.",
                      extra={"typ": "verteilung", "verlauf_je_kategorie": vj,
                             "stand": neuestes})
                print(f"  Kennzahlen: Leerstand nach Zimmerzahl "
                      f"({len(verteilung)} Kategorien, Stand {neuestes}, "
                      f"ohne Quote)")
    except Exception as e:
        fehler.append(f"Leerwohnungsziffer: {e}")

    # --- Mietpreise: Datenlage erklaeren statt Zahlen erfinden ---
    # Fuer Neuhausen publiziert das BFS keine Mietpreise, weil die Gemeinde
    # unter der Schwelle von 15'000 Einwohnenden der Strukturerhebung liegt.
    # Statt fremde Portalzahlen zu uebernehmen (rechtlich heikel, methodisch
    # uneinheitlich), erklaert diese Karte die Lage und nennt belegte Fakten
    # mit Quelle. Die Angaben sind statisch, da sie sich selten aendern.
    try:
        karte("Wohnen", "Mietpreise", "", [],
              "Verschiedene, je Angabe ausgewiesen", "",
              hinweis="Stand der Angaben: Juli 2026. Die Datenlücke bei den "
                      "Mietpreisen betrifft alle Schweizer Gemeinden unter "
                      "15'000 Einwohnenden.",
              extra={
                  "typ": "hinweis",
                  "kernaussage": "Für Neuhausen werden keine amtlichen "
                                 "Mietpreise erhoben.",
                  "punkte": [
                      {"text": "Die Mietpreise des Bundes stammen aus der "
                               "Strukturerhebung. Diese liefert Ergebnisse nur "
                               "für die Schweiz, die Grossregionen, die Kantone "
                               "und für Gemeinden ab 15'000 Einwohnenden. "
                               "Neuhausen zählt 11'834 Einwohnende und liegt "
                               "damit unter dieser Schwelle.",
                       "quelle": "BFS, Strukturerhebung",
                       "url": "https://www.bfs.admin.ch/bfs/de/home/statistiken/"
                              "bevoelkerung/erhebungen/volkszaehlung/"
                              "vier-kernelemente/strukturerhebung.html"},
                      {"text": "Ein indirekter Hinweis auf den Mietdruck ist "
                               "der Leerstand: Die Leerwohnungsziffer in "
                               "Neuhausen sank von 2,64 Prozent im Jahr 2020 "
                               "auf 1,07 Prozent im Jahr 2026. Ein sinkender "
                               "Leerstand deutet auf einen sich anspannenden "
                               "Wohnungsmarkt hin.",
                       "quelle": "BFS, Leerwohnungszählung",
                       "url": "https://www.bfs.admin.ch/bfs/de/home/statistiken/"
                              "bau-wohnungswesen/wohnungen/leerwohnungen.html"},
                      {"text": "Für den Kanton Schaffhausen berichteten die "
                               "Schaffhauser Nachrichten im April 2024, die "
                               "Angebotsmieten seien innert eines Jahres um "
                               "12,6 Prozent gestiegen, der höchste Wert aller "
                               "Kantone. Angebotsmieten sind die Preise "
                               "ausgeschriebener Wohnungen, nicht die Mieten "
                               "bestehender Mietverhältnisse.",
                       "quelle": "Schaffhauser Nachrichten, 19.04.2024",
                       "url": "https://www.shn.ch/region/kanton/2024-04-19/"
                              "in-keinem-anderen-kanton-steigen-die-mietpreise-"
                              "so-stark-wie-in"},
                      {"text": "Kommerzielle Immobilienportale weisen für "
                               "Neuhausen eigene Schätzwerte aus. Diese beruhen "
                               "auf Inseraten, folgen je Anbieter "
                               "unterschiedlichen Methoden und weichen "
                               "voneinander ab. Sie sind keine amtliche "
                               "Statistik und werden hier deshalb nicht "
                               "übernommen.",
                       "quelle": ""},
                  ],
              })
        print("  Kennzahlen: Mietpreise (Hinweiskarte mit Quellen)")
    except Exception as e:
        fehler.append(f"Mietpreis-Hinweis: {e}")

    # --- Stimmberechtigte im Verhaeltnis zur Bevoelkerung (BFS) ---
    # Wichtig: Stimmberechtigt = Schweizer ab 18. Die Quote widerspiegelt
    # daher auch Kinder- und Auslaenderanteil; das wird auf der Karte erklaert.
    try:
        stimm, url = _stattab_reihe(
            "px-x-1702020000_101", ["wahlberechtigte", "stimmberechtigte"])
        try:
            bev_reihe, _ = _stattab_reihe(
                "px-x-0102020000_201", ["bestand am 31. dezember"])
            bev = dict(bev_reihe)
        except Exception:
            bev = {}
        quote = []
        absolut = []
        for jahr, anzahl in stimm:
            if anzahl:
                absolut.append((jahr, int(anzahl)))
            # passendes Bevoelkerungsjahr suchen (gleiches oder naechstfrueheres)
            kandidaten = [j for j in bev if int(j) <= int(jahr)]
            if anzahl and kandidaten:
                bez = max(kandidaten, key=lambda j: int(j))
                if bev[bez]:
                    quote.append((jahr, round(anzahl / bev[bez] * 100, 1)))
        if absolut:
            karte("Bevölkerung", "Stimmberechtigte", "Personen",
                  absolut, "BFS, STAT-TAB (Nationalratswahlen)", url,
                  "Schweizer:innen ab 18 Jahren")
        if quote:
            karte("Bevölkerung", "Anteil Stimmberechtigte", "%",
                  quote, "BFS, STAT-TAB", url,
                  "Anteil an der Gesamtbevölkerung. Stimmberechtigt sind nur "
                  "Schweizer:innen ab 18 Jahren; der Wert widerspiegelt daher "
                  "auch den Anteil Minderjähriger und den Ausländer:innenanteil "
                  "(in Neuhausen rund 46 %).")
            print(f"  Kennzahlen: Anteil Stimmberechtigte {quote[0][0]}\\u2013"
                  f"{quote[-1][0]} ({len(quote)} Werte)")
    except Exception as e:
        fehler.append(f"Stimmberechtigte: {e}")

    # --- Finanzen (aus den Gemeinde-Jahresrechnungen, HRM2 ab 2020) ---
    # Nutzt den Finanz-Extraktor; jede Kennzahl ist durch Kontrollsummen
    # abgesichert. Enthaelt auch die Steuerfuesse als echte Zeitreihe.
    try:
        import finanz_extraktor as fx
        fin = fx.baue_finanz_zeitreihen()
        anzahl = 0
        for sch, kz in fin["kennzahlen"].items():
            reihe = kz["reihe"]  # [[jahr, wert, beurteilung], ...]
            if not reihe:
                continue
            letzte_beurteilung = reihe[-1][2] if len(reihe[-1]) > 2 else ""
            jahr = reihe[-1][0]
            pdf_url = fin.get("quellen", {}).get(
                jahr, "https://neuhausen.ch/finanzkennzahlen")
            karte("Finanzen", kz["name"], kz["einheit"],
                  [(p[0], p[1]) for p in reihe],
                  "Gemeinde Neuhausen, Jahresrechnung " + jahr, pdf_url,
                  extra={
                      "erklaerung": kz.get("erklaerung", ""),
                      "wasBedeutet": kz.get("was_bedeutet", ""),
                      "beurteilung": letzte_beurteilung,
                      "beurteilungen": [
                          [p[0], p[2] if len(p) > 2 else ""] for p in reihe],
                      "zonen": kz.get("zonen", []),
                  })
            anzahl += 1
        if anzahl:
            print(f"  Kennzahlen: Finanzen {anzahl} Kennzahlen aus "
                  f"{len(fin['jahre'])} Jahrgaengen ({', '.join(fin['jahre'])})")
    except Exception as e:
        fehler.append(f"Finanzen: {e}")

    for f in fehler:
        print(f"    Kennzahl nicht abrufbar: {f}", file=sys.stderr)

    bfs_karten = sum(len(b["karten"]) for b in bereiche
                     if b["titel"] not in ("Steuern", "Finanzen"))
    if bfs_karten == 0 and KENNZAHLEN_AUSGABE.exists():
        print("  Kennzahlen: BFS nicht erreichbar, bestehende Datei bleibt",
              file=sys.stderr)
        return

    jetzt = datetime.now(_ZEITZONE) if _ZEITZONE else datetime.now()
    from datetime import timedelta

    # Karten je Bereich in eine sinnvolle Lesereihenfolge bringen.
    # Nicht aufgefuehrte Karten behalten ihre urspruengliche Reihenfolge und
    # kommen ans Ende des jeweiligen Bereichs.
    KARTEN_REIHENFOLGE = {
        "Wohnen": [
            "Leerwohnungsziffer",
            "Leer stehende Wohnungen",
            "Leerstand nach Wohnungsgrösse",
            "Wohnungsbestand",
            "Neu erstellte Wohnungen",
            "Mietpreise",
        ],
    }
    for bereich in bereiche:
        wunsch = KARTEN_REIHENFOLGE.get(bereich["titel"])
        if not wunsch:
            continue
        def _rang(karte, _w=wunsch):
            name = karte.get("name", "")
            return _w.index(name) if name in _w else len(_w)
        bereich["karten"] = sorted(bereich["karten"], key=_rang)

    obj = {
        "version": KENNZAHLEN_VERSION,
        "stand": jetzt.strftime("%d.%m.%Y %H:%M"),
        "naechste_pruefung": int((jetzt + timedelta(
            days=KENNZAHLEN_PRUEFTAKT_TAGE)).strftime("%Y%m%d")),
        "bereiche": bereiche,
    }
    KENNZAHLEN_AUSGABE.write_text(
        "window.NEUHAUSEN_KENNZAHLEN = "
        + json.dumps(obj, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"  Kennzahlen: {sum(len(b['karten']) for b in bereiche)} Karten "
          f"in {len(bereiche)} Bereichen geschrieben")


def diagnose_stattab():
    """Schreibt die echten Dimensionen und Werttexte der Kennzahlen-Wuerfel
    ins Protokoll. Aufruf: python neuhausen_daten.py --diagnose-stattab"""
    wuerfel = [
        ("Bevoelkerung", "px-x-0102020000_201"),
        ("Auslaenderanteil", "px-x-0102010000_104"),
        ("Leerwohnungen", "px-x-0902020300_101"),
        ("Neubau (aktuell)", "px-x-0904030000_107"),
    ]
    for name, cube in wuerfel:
        print(f"\n===== {name}: {cube} =====")
        try:
            basis = f"{STATTAB_BASIS}/{cube}/{cube}.px"
            r = requests.get(basis, headers=HEADERS, timeout=60)
            if r.status_code >= 400:
                r = requests.get(f"{STATTAB_BASIS}/{cube}.px",
                                 headers=HEADERS, timeout=60)
            r.raise_for_status()
            meta = r.json()
            for var in meta.get("variables", []):
                code = var["code"]
                texte = var.get("valueTexts", [])
                werte = var.get("values", [])
                ist_zeit = var.get("time") or code.lower() in ("jahr", "periode")
                if ist_zeit or len(texte) > 25:
                    # Zeit und Riesenlisten (Laender, Gemeinden) nur gekuerzt
                    proben = list(zip(werte[:4], texte[:4]))
                    print(f"  [{code}] ({len(texte)} Werte, gekuerzt): {proben} ...")
                else:
                    print(f"  [{code}] ({len(texte)} Werte):")
                    for w, t in zip(werte, texte):
                        print(f"       {w!r} = {t!r}")
        except Exception as e:
            print(f"  FEHLER: {e}")
    print("\n===== Ende Struktur-Diagnose =====")

    # --- Sonde 1: echte Bevoelkerungsabfrage (warum 11834 statt 11848?) ---
    print("\n===== SONDE Bevoelkerung 2024 =====")
    try:
        basis = f"{STATTAB_BASIS}/px-x-0102020000_201/px-x-0102020000_201.px"
        meta = requests.get(basis, headers=HEADERS, timeout=60).json()
        # Neuhausen-Code finden
        reg_code = None
        for var in meta["variables"]:
            if "gemeinde" in var["code"].lower():
                for w, t in zip(var["values"], var["valueTexts"]):
                    if "neuhausen am rheinfall" in t.lower():
                        reg_code = (var["code"], w)
            if var["code"] == "Staatsangehörigkeit (Kategorie)":
                sak_var = var
            if var["code"] == "Geschlecht":
                ge_var = var
            if var["code"] == "Demografische Komponente":
                dk_var = var
        # Total/Ausland/Schweiz-Codes und Bestand-31.12-Code ausgeben
        def zeig(var, label):
            print(f"  {label}:")
            for w, t in zip(var["values"], var["valueTexts"]):
                print(f"       code {w!r} = {t!r}")
        zeig(sak_var, "Staatsangehörigkeit (Kategorie)")
        print(f"  Neuhausen-Region: {reg_code}")
        # Abfrage bauen: Total, Geschlecht-Total, Bestand 31.12, Jahr 2024
        def code_fuer(var, begriff):
            for w, t in zip(var["values"], var["valueTexts"]):
                if begriff in t.lower():
                    return w
            return None
        q = [
            {"code": reg_code[0], "selection": {"filter": "item", "values": [reg_code[1]]}},
            {"code": "Staatsangehörigkeit (Kategorie)", "selection": {"filter": "item",
             "values": [code_fuer(sak_var, "total")]}},
            {"code": "Geschlecht", "selection": {"filter": "item",
             "values": [code_fuer(ge_var, "total")]}},
            {"code": "Demografische Komponente", "selection": {"filter": "item",
             "values": [code_fuer(dk_var, "bestand am 31. dezember")]}},
        ]
        # Jahr 2024
        for var in meta["variables"]:
            if var.get("time"):
                q.append({"code": var["code"], "selection": {"filter": "item",
                          "values": [var["values"][-1]]}})
                print(f"  Jahr (letztes): {var['values'][-1]} = {var['valueTexts'][-1]}")
        ant = requests.post(basis, json={"query": q,
              "response": {"format": "json-stat2"}}, headers=HEADERS, timeout=60)
        print(f"  Antwort-Status: {ant.status_code}")
        js = ant.json()
        print(f"  value-Array: {js.get('value')}")
        print(f"  -> erwartet 11848 laut Gemeinde-PDF")
    except Exception as e:
        print(f"  SONDE-FEHLER: {e}")

    # --- Sonde 2: Leerwohnungs-Wuerfel ueber mehrere URL-Formen ---
    print("\n===== SONDE Leerwohnungen URL-Formen =====")
    for form in (f"{STATTAB_BASIS}/px-x-0902020300_101/px-x-0902020300_101.px",
                 f"{STATTAB_BASIS}/px-x-0902020300_101.px",
                 f"{STATTAB_BASIS}/px-x-0902020300_103/px-x-0902020300_103.px"):
        try:
            r = requests.get(form, headers=HEADERS, timeout=40)
            print(f"  {form}\n     -> Status {r.status_code}")
            if r.status_code < 400:
                m = r.json()
                for var in m.get("variables", [])[:6]:
                    print(f"       [{var['code']}] {len(var.get('valueTexts', []))} Werte")
        except Exception as e:
            print(f"  {form}\n     -> FEHLER {e}")
    print("\n===== Ende Sonden =====")


def vergleichsgemeinden(min_ew=9500, max_ew=18000):
    """Ermittelt alle Schweizer Gemeinden mit einer staendigen Wohnbevoelkerung
    im angegebenen Bereich (Standard 9500-18000), aus demselben BFS-Wuerfel
    wie die Neuhausen-Bevoelkerung. Gibt eine sortierte, amtliche Liste aus.
    Aufruf: python neuhausen_daten.py --vergleichsgemeinden"""
    print(f"===== Vergleichsgemeinden {min_ew}-{max_ew} Einwohner =====")
    try:
        basis = f"{STATTAB_BASIS}/px-x-0102020000_201/px-x-0102020000_201.px"
        meta = requests.get(basis, headers=HEADERS, timeout=60).json()
    except Exception as e:
        print(f"FEHLER beim Laden der Wuerfel-Struktur: {e}")
        return

    # Dimensionen bestimmen
    reg_var = sak_var = ge_var = dk_var = zeit_var = None
    for var in meta["variables"]:
        code = var["code"]
        if "gemeinde" in code.lower():
            reg_var = var
        elif code == "Staatsangehörigkeit (Kategorie)":
            sak_var = var
        elif code == "Geschlecht":
            ge_var = var
        elif code == "Demografische Komponente":
            dk_var = var
        if var.get("time"):
            zeit_var = var

    def code_fuer(var, begriff):
        for w, t in zip(var["values"], var["valueTexts"]):
            if begriff in t.lower():
                return w
        return None

    tot_sak = code_fuer(sak_var, "total")
    tot_ge = code_fuer(ge_var, "total")
    bestand = code_fuer(dk_var, "bestand am 31. dezember")
    jahr_code = zeit_var["values"][-1]
    jahr_text = zeit_var["valueTexts"][-1]

    # Nur echte Gemeinden (Code beginnt mit "......" im valueText) auswaehlen.
    # Die Wuerfel-Struktur nutzt Praefixe: "......XXXX Gemeindename".
    gemeinde_codes = []
    gemeinde_namen = {}
    for w, t in zip(reg_var["values"], reg_var["valueTexts"]):
        if t.strip().startswith("......"):
            name = t.strip().lstrip(".").strip()
            # Fuehrende BFS-Nummer abtrennen (z. B. "2937 Neuhausen...")
            m = re.match(r"^(\d+)\s+(.*)$", name)
            if m:
                gemeinde_namen[w] = (m.group(1), m.group(2))
            else:
                gemeinde_namen[w] = ("", name)
            gemeinde_codes.append(w)

    print(f"Jahr: {jahr_text}, Gemeinden gesamt: {len(gemeinde_codes)}")
    print(f"Frage Bevoelkerung ab (das dauert einen Moment)...\n")

    # In Bloecken abfragen (alle Gemeinden auf einmal via filter "item").
    ergebnisse = []
    BLOCK = 300
    for i in range(0, len(gemeinde_codes), BLOCK):
        teil = gemeinde_codes[i:i + BLOCK]
        q = [
            {"code": reg_var["code"], "selection": {"filter": "item", "values": teil}},
            {"code": "Staatsangehörigkeit (Kategorie)",
             "selection": {"filter": "item", "values": [tot_sak]}},
            {"code": "Geschlecht", "selection": {"filter": "item", "values": [tot_ge]}},
            {"code": "Demografische Komponente",
             "selection": {"filter": "item", "values": [bestand]}},
            {"code": zeit_var["code"], "selection": {"filter": "item", "values": [jahr_code]}},
        ]
        try:
            r = requests.post(basis, json={"query": q,
                              "response": {"format": "json-stat2"}},
                              headers=HEADERS, timeout=120)
            r.raise_for_status()
            js = r.json()
            werte = js.get("value", [])
            # Reihenfolge der Region-Dimension entspricht der Abfrage-Reihenfolge
            dim = js["dimension"][reg_var["code"]]["category"]["index"]
            # index: code -> position
            pos_zu_code = {p: c for c, p in dim.items()}
            for pos in range(len(werte)):
                w = werte[pos]
                if w is None:
                    continue
                code = pos_zu_code.get(pos)
                if code in gemeinde_namen:
                    nr, name = gemeinde_namen[code]
                    if min_ew <= w <= max_ew:
                        ergebnisse.append((w, name, nr))
        except Exception as e:
            print(f"  Block {i}-{i+len(teil)}: FEHLER {e}")

    ergebnisse.sort(reverse=True)
    print(f"===== {len(ergebnisse)} Gemeinden im Bereich "
          f"{min_ew}-{max_ew} (Stand {jahr_text}) =====\n")
    print(f"{'Einwohner':>10}  {'BFS-Nr':>7}  Gemeinde")
    print("-" * 50)
    for ew, name, nr in ergebnisse:
        marker = "  <-- Neuhausen" if "neuhausen am rheinfall" in name.lower() else ""
        print(f"{ew:>10}  {nr:>7}  {name}{marker}")
    print(f"\n===== Ende Liste ({len(ergebnisse)} Gemeinden) =====")


def diagnose_wohnen_umwelt():
    """Prueft Kandidaten-Wuerfel fuer Wohnen/Umwelt auf drei Dinge:
    1) Ist der Wuerfel erreichbar? 2) Wie ist seine Struktur (Dimensionen)?
    3) Sind Neuhausen UND Kanton SH UND Schweiz als Vergleichswerte abrufbar?
    Aufruf: python neuhausen_daten.py --diagnose-wohnen"""
    # Kandidaten-Wuerfel (aus BFS-Recherche). Nummern koennen sich aendern;
    # die Diagnose zeigt bei Fehler, welche nicht erreichbar sind.
    kandidaten = [
        ("Wohnungsbestand nach Zimmerzahl", "px-x-0902020200_102"),
        ("Bewohnte Wohnungen (Bewohnertyp Miete/Eigentum)", "px-x-0903020000_123"),
        ("Neubau nach Zimmerzahl", "px-x-0904030000_105"),
        ("Arealstatistik Bodennutzung (Kandidat)", "px-x-0202020000_101"),
    ]
    for name, cube in kandidaten:
        print(f"\n{'=' * 60}")
        print(f"{name}: {cube}")
        print("=" * 60)
        # 1) Struktur laden (mit URL-Fallback wie bei den anderen Wuerfeln)
        meta = None
        for form in (f"{STATTAB_BASIS}/{cube}/{cube}.px",
                     f"{STATTAB_BASIS}/{cube}.px"):
            try:
                r = requests.get(form, headers=HEADERS, timeout=40)
                if r.status_code < 400:
                    meta = r.json()
                    break
            except Exception:
                continue
        if not meta:
            print("  NICHT ERREICHBAR (alle URL-Formen HTTP-Fehler)")
            continue

        # 2) Dimensionen zeigen (gekuerzt bei grossen Listen)
        reg_var = None
        zeit_var = None
        for var in meta["variables"]:
            code = var["code"]
            texte = var.get("valueTexts", [])
            ist_region = ("gemeinde" in code.lower() or "kanton" in code.lower()
                          or "region" in code.lower())
            if ist_region:
                reg_var = var
            if var.get("time"):
                zeit_var = var
            if ist_region or len(texte) > 20:
                print(f"  [{code}] ({len(texte)} Werte) Beispiele: "
                      f"{list(zip(var['values'][:3], texte[:3]))}")
            else:
                print(f"  [{code}] ({len(texte)} Werte):")
                for w, t in zip(var["values"], texte):
                    print(f"       {w!r} = {t!r}")

        # 3) Vergleichswerte: Neuhausen, Kanton SH, Schweiz suchen
        if reg_var:
            print("  --- Vergleichsregionen im Wuerfel vorhanden? ---")
            def suche(begriff):
                for w, t in zip(reg_var["values"], reg_var["valueTexts"]):
                    if begriff in t.lower():
                        return (w, t.strip())
                return None
            for label, begriff in (("Neuhausen", "neuhausen am rheinfall"),
                                    ("Kanton Schaffhausen", "schaffhausen"),
                                    ("Schweiz", "schweiz")):
                fund = suche(begriff)
                zeichen = "ja" if fund else "NEIN"
                extra = f" -> {fund}" if fund else ""
                print(f"       {label}: {zeichen}{extra}")
        if zeit_var:
            print(f"  Neuestes Jahr: {zeit_var['valueTexts'][-1]}")
    print(f"\n{'=' * 60}\nEnde Wohnen/Umwelt-Diagnose\n{'=' * 60}")
    print("Hinweis: 'Vergleichsregionen vorhanden' zeigt, ob spaeter ein "
          "Vergleich Neuhausen/Kanton/Schweiz moeglich ist.")


def main():
    if "--diagnose-mietzins" in sys.argv:
        diagnose_mietzins()
        return

    if "--diagnose-leerwohnung" in sys.argv:
        diagnose_leerwohnung()
        return

    if "--diagnose-stattab" in sys.argv:
        diagnose_stattab()
        return

    if "--diagnose-wohnen" in sys.argv:
        diagnose_wohnen_umwelt()
        return

    if "--vergleichsgemeinden" in sys.argv:
        vergleichsgemeinden()
        return

    vorstoesse = []
    berichte = []
    beschluesse = []
    aktuelles = []
    presse = []
    fehler = []

    for quelle in VORSTOSS_QUELLEN:
        try:
            html = hole(quelle["url"])
            teil = parse_vorstoesse(html, quelle)
            vorstoesse.extend(teil)
            print(f"  {quelle['art']:16} {len(teil):>4} Eintraege")
        except Exception as e:
            fehler.append(f"{quelle['art']}: {e}")
            print(f"  {quelle['art']:16} FEHLER: {e}", file=sys.stderr)

    try:
        html = hole(BA_URL)
        berichte = parse_berichte(html)
        print(f"  Berichte&Antraege {len(berichte):>3} Eintraege")
    except Exception as e:
        fehler.append(f"Berichte & Antraege: {e}")
        print(f"  Berichte&Antraege FEHLER: {e}", file=sys.stderr)

    try:
        html = hole(BP_URL)
        beschluesse = parse_beschluesse(html)
        print(f"  Beschl.&Protokolle {len(beschluesse):>2} Eintraege")
    except Exception as e:
        fehler.append(f"Beschluesse & Protokolle: {e}")
        print(f"  Beschl.&Protokolle FEHLER: {e}", file=sys.stderr)

    try:
        html = hole(AKTUELLES_URL)
        aktuelles = parse_aktuelles(html)
        print(f"  Aktuelles         {len(aktuelles):>3} Eintraege")
    except Exception as e:
        fehler.append(f"Aktuelles: {e}")
        print(f"  Aktuelles         FEHLER: {e}", file=sys.stderr)

    try:
        presse, presse_fehler = baue_presse()
        for pf in presse_fehler:
            print(f"    Presse-Quelle nicht erreichbar: {pf}", file=sys.stderr)
        if presse_fehler and not presse:
            fehler.append("Presse: keine Quelle erreichbar")
    except Exception as e:
        print(f"  Presse FEHLER: {e}", file=sys.stderr)

    if "--ohne-kennzahlen" not in sys.argv:
        try:
            baue_kennzahlen()
        except Exception as e:
            print(f"  Kennzahlen FEHLER: {e}", file=sys.stderr)
    else:
        print("Kennzahlen uebersprungen (--ohne-kennzahlen)")

    daten = {
        "erzeugt": datetime.now(_ZEITZONE).strftime("%d.%m.%Y %H:%M"),
        "fehler": fehler,
        "vorstoesse": vorstoesse,
        "berichte": berichte,
        "beschluesse": beschluesse,
        "aktuelles": aktuelles,
        "presse": presse,
    }

    if UNBEKANNTE_PERSONEN:
        print("\nNicht zugeordnete Personen (bitte in FRAKTIONEN ergaenzen):")
        for name in sorted(UNBEKANNTE_PERSONEN):
            print(f"    \"{name}\": \"?\",")
        print()

    js = "window.NEUHAUSEN_DATEN = " + json.dumps(daten, ensure_ascii=False) + ";\n"
    AUSGABE.write_text(js, encoding="utf-8")

    feed = baue_feed(vorstoesse, berichte, beschluesse, aktuelles, presse)
    FEED_AUSGABE.write_text(feed, encoding="utf-8")

    if "--ohne-volltext" not in sys.argv:
        try:
            baue_volltext(vorstoesse, berichte, beschluesse)
        except Exception as e:
            print(f"Volltext-Erfassung fehlgeschlagen: {e}", file=sys.stderr)
    else:
        print("Volltext uebersprungen (--ohne-volltext)")

    print(f"Geschrieben: {AUSGABE}  "
          f"({len(vorstoesse)} Vorstoesse, {len(berichte)} Berichte, "
          f"{len(beschluesse)} Beschluesse, {len(aktuelles)} Aktuelles, "
          f"{len(presse)} Presse)")
    print(f"Geschrieben: {FEED_AUSGABE}")

    if fehler and not vorstoesse and not berichte:
        sys.exit(1)


if __name__ == "__main__":
    main()
