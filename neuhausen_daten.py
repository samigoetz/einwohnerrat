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
    # --- Ergaenzungen gemaess Angaben SP Neuhausen ---
    "Nicole Hinder": "AL",
    "Adrian Schüpbach": "SVP",
    "Randy Ruh": "GLP",
    "Sabina Tektas-Sorg": "SP",
    "Sara Jucker": "SVP",
    "Andreas Neuenschwander": "SVP",
    "Ruedi Meier": "SP",
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


def baue_feed(vorstoesse: list, berichte: list, beschluesse: list, aktuelles: list) -> str:
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


def main():
    vorstoesse = []
    berichte = []
    beschluesse = []
    aktuelles = []
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

    daten = {
        "erzeugt": datetime.now(_ZEITZONE).strftime("%d.%m.%Y %H:%M"),
        "fehler": fehler,
        "vorstoesse": vorstoesse,
        "berichte": berichte,
        "beschluesse": beschluesse,
        "aktuelles": aktuelles,
    }

    if UNBEKANNTE_PERSONEN:
        print("\nNicht zugeordnete Personen (bitte in FRAKTIONEN ergaenzen):")
        for name in sorted(UNBEKANNTE_PERSONEN):
            print(f"    \"{name}\": \"?\",")
        print()

    js = "window.NEUHAUSEN_DATEN = " + json.dumps(daten, ensure_ascii=False) + ";\n"
    AUSGABE.write_text(js, encoding="utf-8")

    feed = baue_feed(vorstoesse, berichte, beschluesse, aktuelles)
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
          f"{len(beschluesse)} Beschluesse, {len(aktuelles)} Aktuelles)")
    print(f"Geschrieben: {FEED_AUSGABE}")

    if fehler and not vorstoesse and not berichte:
        sys.exit(1)


if __name__ == "__main__":
    main()
