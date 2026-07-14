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
import sys
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


def main():
    vorstoesse = []
    berichte = []
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

    daten = {
        "erzeugt": datetime.now(_ZEITZONE).strftime("%d.%m.%Y %H:%M"),
        "fehler": fehler,
        "vorstoesse": vorstoesse,
        "berichte": berichte,
    }

    if UNBEKANNTE_PERSONEN:
        print("\nNicht zugeordnete Personen (bitte in FRAKTIONEN ergaenzen):")
        for name in sorted(UNBEKANNTE_PERSONEN):
            print(f"    \"{name}\": \"?\",")
        print()

    js = "window.NEUHAUSEN_DATEN = " + json.dumps(daten, ensure_ascii=False) + ";\n"
    AUSGABE.write_text(js, encoding="utf-8")
    print(f"Geschrieben: {AUSGABE}  "
          f"({len(vorstoesse)} Vorstoesse, {len(berichte)} Berichte)")

    if fehler and not vorstoesse and not berichte:
        sys.exit(1)


if __name__ == "__main__":
    main()
