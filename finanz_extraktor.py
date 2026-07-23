"""
Extraktor fuer die Jahresrechnungen der Gemeinde Neuhausen am Rheinfall.
ZWECK DIESER STUFE: strukturieren + selbst pruefen (Kontrollsummen), noch
KEINE Anzeige. Gibt strukturierte Daten als JSON aus und meldet, ob die
eingebauten Proben (Aufwand/Ertrag = Gesamtergebnis, Aktiven = Passiven)
aufgehen. So wissen wir, ob die Extraktion stimmt, statt zu raten.
"""
import re
import sys
import json


def _zahl(text):
    """'-90'114'936' -> -90114936.  Gibt None zurueck, wenn keine Zahl."""
    if text is None:
        return None
    t = text.strip().replace("\u2019", "").replace("'", "").replace("\u2013", "-")
    t = t.replace("\u2212", "-")  # Minuszeichen
    t = t.replace("\u00a0", "").replace("\u202f", "").replace("\u2009", "")  # schmale Leerzeichen
    # Klammern als negativ (kommt in manchen Rechnungen vor)
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1]
    t = t.replace(" ", "")
    if t in ("", "-", "\u2013"):
        return None
    if not re.match(r"^-?\d+(\.\d+)?$", t):
        return None
    v = float(t)
    if v == int(v):
        v = int(v)
    return -v if neg else v


# ---------------------------------------------------------------------------
# Wortkoordinaten-basierte Zeilenrekonstruktion.
# pdfplumber liefert je Wort Text und x-Position. Zahlenfragmente, die eng
# beieinanderstehen (kleine Luecke), gehoeren zu EINER Zahl; grosse Luecken
# trennen Spalten. So zerbrechen grosse Betraege nicht mehr.
# ---------------------------------------------------------------------------

def zeilen_aus_woertern(seite):
    """Gibt Liste von Zeilen zurueck; jede Zeile ist (text, [(zahl, x0), ...]).
    Zahlen werden aus benachbarten Ziffern-Fragmenten zusammengesetzt."""
    woerter = seite.extract_words(x_tolerance=1.5, y_tolerance=3,
                                  keep_blank_chars=False)
    # nach Zeile (oben-Koordinate, gerundet) gruppieren
    zeilen_map = {}
    for w in woerter:
        schluessel = round(w["top"] / 3)
        zeilen_map.setdefault(schluessel, []).append(w)

    ergebnis = []
    for schluessel in sorted(zeilen_map):
        ws = sorted(zeilen_map[schluessel], key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in ws)
        # Ziffern-Fragmente zu Zahlen zusammensetzen
        zahlen = _zahlen_aus_woertern(ws)
        ergebnis.append((text, zahlen))
    return ergebnis


def _ist_ziffernfragment(t):
    """Sieht aus wie ein (Teil-)Betrag: nur Ziffern, evtl. Vorzeichen/Punkt/
    Klammer/Prozent."""
    tt = t.strip().strip("()%")
    tt = tt.replace("\u2019", "").replace("'", "")
    tt = tt.replace("\u00a0", "").replace("\u202f", "").replace("\u2009", "")
    return bool(re.match(r"^-?\d+(\.\d+)?$", tt)) if tt else False


def _zahlen_aus_woertern(ws):
    """Aus den Woertern einer Zeile die Betraege rekonstruieren.
    Fragmente, deren Luecke klein ist (< breiten-basierter Schwelle),
    werden verkettet; grosse Luecken trennen Zahlen."""
    zahlen = []
    aktuell = None   # {"teile": [...], "x0": .., "x1": ..}
    for w in ws:
        t = w["text"]
        if _ist_ziffernfragment(t):
            if aktuell is None:
                aktuell = {"teile": [t], "x0": w["x0"], "x1": w["x1"]}
            else:
                luecke = w["x0"] - aktuell["x1"]
                # Zeichenbreite grob = Breite/Zeichen des bisherigen Blocks
                # Kleine Luecke (< ~4 pt) => selbe Zahl (Tausendertrennung),
                # sonst neue Zahl.
                if luecke < 4.0:
                    aktuell["teile"].append(t)
                    aktuell["x1"] = w["x1"]
                else:
                    zahlen.append(_verkette(aktuell))
                    aktuell = {"teile": [t], "x0": w["x0"], "x1": w["x1"]}
        else:
            if aktuell is not None:
                zahlen.append(_verkette(aktuell))
                aktuell = None
    if aktuell is not None:
        zahlen.append(_verkette(aktuell))
    return [z for z in zahlen if z is not None]


def _verkette(block):
    """Fragmentteile zu einer Zahl zusammensetzen: '90' '229' '384' -> 90229384.
    Vorzeichen nur am Anfang, Dezimalpunkt bleibt erhalten."""
    roh = "".join(block["teile"])
    return _zahl(roh)


# Zahl-Muster: nur ECHTE Tausendertrenner (Hochkomma, schmale Leerzeichen),
# NIE das normale Leerzeichen (das trennt separate Zahlen).
_TRENNER = "'\u2019\u00a0\u202f\u2009"
ZAHL = (r"-?\d{1,3}(?:[" + _TRENNER + r"]\d{3})+(?:\.\d+)?"
        r"|-?\d+(?:\.\d+)?")


def _zahlen_der_zeile(zeile):
    """Alle Zahlen einer reinen Textzeile (Rueckfallebene ohne Koordinaten)."""
    return [_zahl(m) for m in re.findall(ZAHL, zeile)]


def extrahiere_uebersicht(zeilen):
    """Seite 'Uebersicht': Erfolgsrechnung-Eckwerte, Steuerfuesse.
    `zeilen` ist Liste von (text, [zahlen]) aus zeilen_aus_woertern."""
    d = {}
    for text, zahlen in zeilen:
        z = text.strip()
        for schluessel, muster in (
            ("gesamtaufwand", r"^Gesamtaufwand\b"),
            ("gesamtertrag", r"^Gesamtertrag\b"),
            ("ergebnis_er", r"^Ertrags-.*Aufwand.berschuss"),
            ("operatives_ergebnis", r"^Operatives Ergebnis\b"),
        ):
            if re.search(muster, z) and zahlen:
                d[schluessel] = zahlen
        if re.search(r"Steuerfuss nat.rliche Personen", z):
            proz = re.findall(r"(\d+)\s*%", z)
            if proz:
                d["steuerfuss_np"] = [int(p) for p in proz]
        if re.search(r"Steuerfuss juristische Personen", z):
            proz = re.findall(r"(\d+)\s*%", z)
            if proz:
                d["steuerfuss_jp"] = [int(p) for p in proz]
    return d


def extrahiere_a8_kennzahlen(zeilen):
    """Seite A8 Finanzkennzahlen. Nimmt pro Kennzahl NUR den ersten
    (=aktuellen) Prozentwert. Die Zeitreihe wird spaeter aus mehreren
    Jahrgaengen zusammengesetzt; die im PDF mitgelieferten Vorjahreswerte
    werden bewusst ignoriert, weil im 2025er-PDF Zeilen ineinanderlaufen
    (mehrere Werte je Zeile) und eine Fehlzuordnung riskieren.
    Der jeweils erste Wert der Kennzahlenzeile ist verlaesslich der aktuelle."""
    kennzahlen = {}
    muster = {
        "nettoverschuldungsquotient": r"^Nettoverschuldungsquotient\b",
        "selbstfinanzierungsgrad": r"^Selbstfinanzierungsgrad\b",
        "zinsbelastungsanteil": r"^Zinsbelastungsanteil\b",
        "selbstfinanzierungsanteil": r"^Selbstfinanzierungsanteil\b",
        "kapitaldienstanteil": r"^Kapitaldienstanteil\b",
        "bruttoverschuldungsanteil": r"^Bruttoverschuldungsanteil\b",
        "investitionsanteil": r"^Investitionsanteil\b",
    }
    for text, zahlen in zeilen:
        z = text.strip()
        for schluessel, m in muster.items():
            if schluessel in kennzahlen:
                continue
            if re.match(m, z):
                # ersten Prozentwert der Zeile nehmen (= aktuelles Jahr)
                proz = re.search(r"(-?\d+(?:\.\d+)?)\s*%", z)
                if proz:
                    kennzahlen[schluessel] = float(proz.group(1))
        # Nettoschuld pro Kopf: erste Zahl der Zeile (Franken, kann negativ)
        if "nettoschuld_pro_kopf" not in kennzahlen \
                and re.match(r"^Nettoschuld I pro Einwohner", z) and zahlen:
            kennzahlen["nettoschuld_pro_kopf"] = zahlen[0]
    return kennzahlen


def extrahiere_bilanz_summen(zeilen):
    """Bilanz-Hauptsummen fuer die Kontrollprobe Aktiven = Passiven."""
    d = {}
    for text, zahlen in zeilen:
        z = text.strip()
        # Erste Zahl der Zeile ist der Bilanzwert. Ordnungsziffer (1, 2, 29)
        # ist KEINE eigene Zahl in den Koordinaten, weil sie mit Abstand vor
        # dem Betrag steht; zur Sicherheit filtern wir kleine Leitziffern.
        if re.match(r"^1 Aktiven\b", z) and zahlen:
            d["aktiven"] = _erster_betrag(zahlen)
        if re.match(r"^2 Passiven\b", z) and zahlen:
            d["passiven"] = _erster_betrag(zahlen)
        if re.match(r"^29 Eigenkapital\b", z) and zahlen:
            d["eigenkapital"] = _erster_betrag(zahlen)
    return d


def _erster_betrag(zahlen):
    """Erste 'echte' Zahl (> 999 oder < -999), um Leitziffern wie 1/2/29
    zu ueberspringen, falls sie doch als Zahl auftauchen."""
    for z in zahlen:
        if abs(z) > 999:
            return z
    return zahlen[0] if zahlen else None


def pruefe(uebersicht, bilanz):
    """Kontrollsummen. Gibt Liste (name, ok, detail) zurueck."""
    proben = []
    # Probe 1: |Ertrag| - |Aufwand| = Ergebnis. Die Uebersichtszeilen mischen
    # Vorzeichen aus mehreren Spalten, deshalb mit Betraegen rechnen.
    if "gesamtaufwand" in uebersicht and "gesamtertrag" in uebersicht \
            and "ergebnis_er" in uebersicht:
        auf = abs(uebersicht["gesamtaufwand"][0])
        ert = abs(uebersicht["gesamtertrag"][0])
        erg = uebersicht["ergebnis_er"][0]
        differenz = ert - auf
        ok = abs(differenz - erg) <= 2  # Rundungstoleranz
        proben.append(("Ertrag - Aufwand = Ergebnis (Erfolgsrechnung)", ok,
                       f"{ert} - {auf} = {differenz}, ausgewiesen {erg}"))
    # Probe 2: Aktiven + Passiven = 0 (Passiven negativ gefuehrt)
    if "aktiven" in bilanz and "passiven" in bilanz:
        a = bilanz["aktiven"]
        p = bilanz["passiven"]
        ok = abs(a + p) <= 2
        proben.append(("Aktiven = Passiven (Bilanz)", ok,
                       f"Aktiven {a}, Passiven {p}, Summe {a + p}"))
    return proben


def _alle_zeilen(pdf):
    """Alle Seiten -> flache Liste von (text, [zahlen]) mit Koordinaten."""
    zeilen = []
    for seite in pdf.pages:
        zeilen.extend(zeilen_aus_woertern(seite))
    return zeilen


def verarbeite(pfad):
    import pdfplumber
    with pdfplumber.open(pfad) as pdf:
        zeilen = _alle_zeilen(pdf)
        n_seiten = len(pdf.pages)

    uebersicht = extrahiere_uebersicht(zeilen)
    a8 = extrahiere_a8_kennzahlen(zeilen)
    bilanz = extrahiere_bilanz_summen(zeilen)
    proben = pruefe(uebersicht, bilanz)

    return {
        "seiten": n_seiten,
        "uebersicht": uebersicht,
        "a8_kennzahlen": a8,
        "bilanz_summen": bilanz,
        "proben": [{"name": n, "ok": ok, "detail": d} for n, ok, d in proben],
    }


JAHRESRECHNUNGEN = {
    "2025": "https://neuhausen.ch/fileupload/Jahresrechnung 2025.pdf",
    "2024": "https://neuhausen.ch/fileupload/Jahresrechnung 2024 genehmigte Version.pdf",
    "2023": "https://neuhausen.ch/fileupload/Jahresrechnung 2023 genehmigte Version.pdf",
    "2022": "https://neuhausen.ch/fileupload/Jahresrechnung 2022.pdf",
    "2021": "https://neuhausen.ch/fileupload/Jahresrechnung 2021.pdf",
    "2020": "https://neuhausen.ch/fileupload/Jahresrechnung 2020 durch ER genehmigt zwei.pdf",
}

# Ab diesem Jahr gilt HRM2 fuer die Gemeinden im Kanton Schaffhausen. Erst ab
# dann hat die Jahresrechnung die standardisierte A8-Finanzkennzahlenseite.
HRM2_AB_JAHR = 2020
FINANZ_SEITE = "https://neuhausen.ch/finanzkennzahlen"


def finde_jahresrechnungen(seiten_url=FINANZ_SEITE, fallback=None):
    """Liest die Finanzseite der Gemeinde und erkennt automatisch alle
    verlinkten Jahresrechnungen ab dem HRM2-Jahr. Neue Jahrgaenge werden so
    ohne Code-Aenderung aufgearbeitet.

    Nur echte Jahresrechnungen (nicht Geschaeftsbericht, Budget, Finanzplan)
    werden beruecksichtigt. Bei Fehlern faellt die Funktion auf die fest
    hinterlegte Liste zurueck, damit der Job nie an der Erkennung scheitert.

    Rueckgabe: dict {jahr(str): pdf_url}."""
    import urllib.request
    import urllib.parse
    import html as _html

    fallback = fallback if fallback is not None else dict(JAHRESRECHNUNGEN)
    try:
        req = urllib.request.Request(seiten_url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            roh = r.read().decode("utf-8", "replace")
    except Exception:
        return fallback

    # Alle Links zu PDFs unter /fileupload/ mit ihrem sichtbaren Linktext.
    # Muster deckt <a href="...">Text</a> ab.
    gefunden = {}
    muster = re.compile(
        r'href="([^"]*?/fileupload/[^"]*?\.pdf)"[^>]*>\s*([^<]*?)\s*</a>',
        re.IGNORECASE)
    for treffer in muster.finditer(roh):
        url_roh = _html.unescape(treffer.group(1))
        linktext = _html.unescape(treffer.group(2))
        # Nur echte Jahresrechnungen: Linktext beginnt mit "Jahresrechnung"
        # und NICHT Geschaeftsbericht/Budget/Finanzplan/Rechnung des GR usw.
        if not re.match(r"^\s*Jahresrechnung\b", linktext, re.IGNORECASE):
            continue
        # Jahr aus dem Linktext ziehen (verlaesslicher als aus dem Dateinamen)
        m = re.search(r"(20\d{2})", linktext)
        if not m:
            m = re.search(r"(20\d{2})", url_roh)
        if not m:
            continue
        jahr = m.group(1)
        if int(jahr) < HRM2_AB_JAHR:
            continue
        # Absolute URL sicherstellen
        if url_roh.startswith("http"):
            voll = url_roh
        else:
            voll = urllib.parse.urljoin(seiten_url, url_roh)
        # Nur den ersten Treffer je Jahr behalten (Seite listet neueste zuerst)
        if jahr not in gefunden:
            gefunden[jahr] = voll

    if not gefunden:
        return fallback
    # Feste Liste als Sicherheitsnetz einmischen: gefundene haben Vorrang,
    # fehlende Jahre aus dem Fallback ergaenzen.
    ergebnis = dict(fallback)
    ergebnis.update(gefunden)
    return ergebnis

# Die acht A8-Kennzahlen (fuer die Vollstaendigkeitspruefung)
A8_SCHLUESSEL = [
    "nettoverschuldungsquotient", "selbstfinanzierungsgrad",
    "zinsbelastungsanteil", "selbstfinanzierungsanteil",
    "kapitaldienstanteil", "bruttoverschuldungsanteil",
    "investitionsanteil", "nettoschuld_pro_kopf",
]

# Kennzahlen-Metadaten. Quellen: HRM2-Fachempfehlung 18 (Konferenz der
# kantonalen Aufsichtsstellen ueber die Gemeindefinanzen KKAG) sowie die
# Beurteilungsskalen der Jahresrechnung Neuhausen (Seite A8). Die Erklaerungen
# sind bewusst laienverstaendlich gehalten.
# Struktur je Kennzahl:
#   name, einheit, erklaerung (Tooltip), was_bedeutet (Werte-Einordnung),
#   skala: Liste (grenze_pruef, label) - wird der Reihe nach geprueft,
#          richtung "hoch_gut" oder "tief_gut" bestimmt die Bewertung.
KENNZAHL_META = {
    "nettoverschuldungsquotient": {
        "name": "Nettoverschuldungsquotient",
        "einheit": "%",
        "erklaerung": "Zeigt, welcher Anteil der Steuereinnahmen nötig wäre, "
                      "um die Nettoschulden vollständig abzuzahlen. Ein "
                      "negativer Wert bedeutet Nettovermögen statt Schulden.",
        "was_bedeutet": "Unter 100 % gilt als gut, 100–150 % als genügend, "
                        "über 150 % als schlecht. Je tiefer, desto besser.",
        "richtung": "tief_gut",
        "skala": [(100, "gut"), (150, "genügend"), (250, "schlecht"),
                  (None, "kritisch")],
    },
    "selbstfinanzierungsgrad": {
        "name": "Selbstfinanzierungsgrad",
        "einheit": "%",
        "erklaerung": "Zeigt, welcher Anteil der Investitionen die Gemeinde "
                      "aus eigenen Mitteln bezahlen kann, ohne neue Schulden "
                      "aufzunehmen.",
        "was_bedeutet": "Über 100 % ist ideal (Schuldenabbau möglich), "
                        "80–100 % gut, 50–80 % problematisch, unter 50 % "
                        "ungenügend. Schwankt von Jahr zu Jahr stark.",
        "richtung": "hoch_gut",
        "skala_hoch": [(100, "ideal"), (80, "gut"), (50, "problematisch"),
                       (None, "ungenügend")],
    },
    "zinsbelastungsanteil": {
        "name": "Zinsbelastungsanteil",
        "einheit": "%",
        "erklaerung": "Zeigt, welcher Anteil der laufenden Einnahmen für "
                      "Nettozinsen gebunden ist. Je tiefer, desto grösser der "
                      "finanzielle Spielraum.",
        "was_bedeutet": "0–4 % gilt als gut, 4–9 % als genügend, "
                        "über 9 % als schlecht.",
        "richtung": "tief_gut",
        "skala": [(4, "gut"), (9, "genügend"), (None, "schlecht")],
    },
    "selbstfinanzierungsanteil": {
        "name": "Selbstfinanzierungsanteil",
        "einheit": "%",
        "erklaerung": "Zeigt, welcher Anteil der Einnahmen für Investitionen "
                      "oder Schuldenabbau zur Verfügung steht. Ein Mass für "
                      "die finanzielle Leistungsfähigkeit.",
        "was_bedeutet": "Über 20 % gilt als gut, 10–20 % als mittel, "
                        "unter 10 % als schlecht. Je höher, desto besser.",
        "richtung": "hoch_gut",
        "skala_hoch": [(20, "gut"), (10, "mittel"), (None, "schlecht")],
    },
    "kapitaldienstanteil": {
        "name": "Kapitaldienstanteil",
        "einheit": "%",
        "erklaerung": "Zeigt, welcher Anteil der laufenden Einnahmen durch "
                      "Zinsen und Abschreibungen gebunden ist. Je tiefer, "
                      "desto tragbarer.",
        "was_bedeutet": "Bis 5 % geringe Belastung, 5–15 % tragbar, "
                        "über 15 % hohe Belastung.",
        "richtung": "tief_gut",
        "skala": [(5, "geringe Belastung"), (15, "tragbare Belastung"),
                  (None, "hohe Belastung")],
    },
    "bruttoverschuldungsanteil": {
        "name": "Bruttoverschuldungsanteil",
        "einheit": "%",
        "erklaerung": "Setzt die gesamten Schulden ins Verhältnis zu den "
                      "Einnahmen. Zeigt, ob die Verschuldung im Verhältnis "
                      "zur Wirtschaftskraft angemessen ist.",
        "was_bedeutet": "Unter 50 % sehr gut, 50–100 % gut, 100–150 % mittel, "
                        "150–200 % schlecht, über 200 % kritisch.",
        "richtung": "tief_gut",
        "skala": [(50, "sehr gut"), (100, "gut"), (150, "mittel"),
                  (200, "schlecht"), (None, "kritisch")],
    },
    "investitionsanteil": {
        "name": "Investitionsanteil",
        "einheit": "%",
        "erklaerung": "Zeigt, wie aktiv die Gemeinde investiert, gemessen am "
                      "Anteil der Investitionen an den Gesamtausgaben.",
        "was_bedeutet": "Unter 10 % schwache, 10–20 % mittlere, 20–30 % hohe, "
                        "über 30 % sehr hohe Investitionstätigkeit. Hinweis: "
                        "Die amtliche Statistik weist diese Kennzahl neutral "
                        "aus. Die Farbgebung hier ordnet eine höhere "
                        "Investitionstätigkeit als positiv ein, weil sie "
                        "Zukunftsvorsorge und öffentliche Leistungen bedeutet.",
        "richtung": "hoch_gut",
        "skala_hoch": [(30, "sehr hohe Investitionen"),
                       (20, "hohe Investitionen"),
                       (10, "mittlere Investitionen"),
                       (None, "schwache Investitionen")],
    },
    "nettoschuld_pro_kopf": {
        "name": "Nettoschuld pro Einwohner:in",
        "einheit": "CHF",
        "erklaerung": "Die Nettoschuld (Schulden minus Finanzvermögen) verteilt "
                      "auf alle Einwohner:innen. Ein negativer Wert bedeutet "
                      "Nettovermögen pro Kopf statt Schulden.",
        "was_bedeutet": "Unter 0 = Nettovermögen. 0–1'000 geringe, "
                        "1'000–2'500 mittlere, 2'500–5'000 hohe, über 5'000 "
                        "sehr hohe Verschuldung pro Kopf.",
        "richtung": "tief_gut",
        "skala": [(0, "Nettovermögen"), (1000, "geringe Verschuldung"),
                  (2500, "mittlere Verschuldung"), (5000, "hohe Verschuldung"),
                  (None, "sehr hohe Verschuldung")],
    },
}


def beurteile(schluessel, wert):
    """Amtliche Beurteilung eines Kennzahlenwerts (z. B. 'gut', 'ideal')."""
    meta = KENNZAHL_META.get(schluessel)
    if not meta or wert is None:
        return ""
    if meta["richtung"] == "hoch_gut":
        for grenze, label in meta["skala_hoch"]:
            if grenze is None or wert >= grenze:
                return label
    else:
        for grenze, label in meta["skala"]:
            if grenze is None or wert < grenze:
                return label
    return ""


# Ampel-Zuordnung der Labels (gleiche Logik wie im HTML/CSS).
_AMPEL = {
    "gruen": {"gut", "ideal", "sehr gut", "nettovermögen",
              "geringe belastung", "geringe verschuldung",
              "sehr hohe investitionen", "hohe investitionen"},
    "orange": {"genügend", "mittel", "tragbare belastung", "problematisch",
               "mittlere verschuldung", "hoch", "mittlere investitionen"},
    "rot": {"schlecht", "kritisch", "ungenügend", "hohe belastung",
            "hohe verschuldung", "sehr hohe verschuldung",
            "schwache investitionen"},
    "neutral": {"schwach", "sehr hoch"},
}


def _ampel_farbe(label):
    l = label.lower()
    for farbe, menge in _AMPEL.items():
        if l in menge:
            return farbe
    return "neutral"


def zonen_fuer(schluessel):
    """Erzeugt die farbigen Bewertungszonen einer Kennzahl als Liste von
    Baendern: [{"von": zahl|None, "bis": zahl|None, "farbe": "gruen"|...,
    "label": str}, ...]. None bedeutet unbegrenzt nach unten/oben.
    Nur fuer Kennzahlen mit bewertender Skala; sonst leere Liste."""
    meta = KENNZAHL_META.get(schluessel)
    if not meta:
        return []
    zonen = []
    if meta["richtung"] == "hoch_gut":
        # skala_hoch: (grenze, label), Wert >= grenze -> label.
        # Absteigende Grenzen (z. B. 100 ideal, 80 gut, 50 problematisch, None)
        eintraege = meta["skala_hoch"]
        # Grenzen absteigend; "vorherige" ist jeweils die obere Zonengrenze.
        vorherige = None
        for grenze, label in eintraege:
            # Zone: [grenze, vorherige)  (vorherige = obere Grenze oder None)
            zonen.append({"von": grenze, "bis": vorherige,
                          "farbe": _ampel_farbe(label), "label": label})
            vorherige = grenze
    else:
        # tief_gut oder neutral: (grenze, label), Wert < grenze -> label.
        # Aufsteigende Grenzen (z. B. 4 gut, 9 genügend, None schlecht)
        eintraege = meta["skala"]
        # Bei neutraler Kennzahl (Investitionsanteil) gibt es keine Wertung,
        # daher alle Zonen neutral einfaerben.
        neutral = meta["richtung"] == "neutral"
        untergrenze = None
        for grenze, label in eintraege:
            farbe = "neutral" if neutral else _ampel_farbe(label)
            zonen.append({"von": untergrenze, "bis": grenze,
                          "farbe": farbe, "label": label})
            untergrenze = grenze
    return zonen


def _lade_pdf(url):
    import urllib.request
    import urllib.parse
    # Leerzeichen im Pfad kodieren, Rest lassen
    teile = url.split("/fileupload/")
    sicher = teile[0] + "/fileupload/" + urllib.parse.quote(teile[1])
    req = urllib.request.Request(sicher, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def _zeilen_aus_bytes(roh):
    import pdfplumber
    import io
    with pdfplumber.open(io.BytesIO(roh)) as pdf:
        zeilen = _alle_zeilen(pdf)
        n = len(pdf.pages)
    return zeilen, n


def diagnose():
    """Laedt die echten PDFs 2024 und 2025, strukturiert sie und zeigt,
    ob die Kontrollsummen aufgehen. Aendert nichts."""
    for jahr, url in JAHRESRECHNUNGEN.items():
        print(f"\n{'=' * 60}")
        print(f"JAHRESRECHNUNG {jahr}")
        print(f"{'=' * 60}")
        try:
            roh = _lade_pdf(url)
            print(f"  PDF geladen: {len(roh):,} Bytes")
            zeilen, n_seiten = _zeilen_aus_bytes(roh)
            print(f"  Seiten: {n_seiten}, Zeilen: {len(zeilen)}")
        except Exception as e:
            print(f"  FEHLER beim Laden/Lesen: {e}")
            continue

        ueb = extrahiere_uebersicht(zeilen)
        a8 = extrahiere_a8_kennzahlen(zeilen)
        bil = extrahiere_bilanz_summen(zeilen)
        proben = pruefe(ueb, bil)

        print(f"\n  --- UEBERSICHT (Rechnung/Budget/Vorjahr) ---")
        for k, v in ueb.items():
            print(f"      {k}: {v}")
        print(f"\n  --- A8 FINANZKENNZAHLEN (Rechnung/Vorjahr) ---")
        for k, v in a8.items():
            print(f"      {k}: {v}")
        print(f"\n  --- BILANZ-SUMMEN ---")
        for k, v in bil.items():
            print(f"      {k}: {v}")
        print(f"\n  --- KONTROLLSUMMEN ---")
        if not proben:
            print("      keine Proben moeglich (Werte fehlen)")
        for name, ok, detail in proben:
            print(f"      [{'OK ' if ok else 'FEHLER'}] {name}: {detail}")
        # Rohzeilen der A8-Seite zeigen, falls Kennzahlen fehlen
        if len(a8) < 8:
            print(f"\n  --- A8-ROHZEILEN (zur Diagnose fehlender Kennzahlen) ---")
            for text, zahlen in zeilen:
                if re.search(r"Nettoverschuldung|Investitionsanteil|"
                             r"Zinsbelastung|Nettoschuld", text):
                    print(f"      TEXT: {text!r}")
                    print(f"      ZAHLEN: {zahlen}")
        print(f"\n  Ergebnis {jahr}: {len(a8)}/8 A8-Kennzahlen, "
              f"{sum(1 for _, ok, _ in proben if ok)}/{len(proben)} Proben ok")
    print(f"\n{'=' * 60}\nEnde Finanz-Diagnose\n{'=' * 60}")


def baue_finanz_zeitreihen(lade_funktion=None):
    """Laeuft ueber alle Jahrgaenge, extrahiert die geprueften Kennzahlen und
    baut die Zeitreihen fuer den Finanz-Bereich. Nur Jahre, deren
    Kontrollsummen aufgehen, fliessen ein (Qualitaetstor).
    `lade_funktion(url) -> bytes` ist injizierbar (fuer Tests).
    Gibt dict zurueck: {kennzahlen: {schluessel: {name, einheit, erklaerung,
    was_bedeutet, reihe: [[jahr, wert, beurteilung], ...], quelleUrl}}, jahre}."""
    lade = lade_funktion or _lade_pdf
    # Jahrgaenge automatisch von der Finanzseite erkennen (mit Fallback auf
    # die feste Liste). So werden neue Jahresrechnungen ohne Code-Aenderung
    # aufgearbeitet.
    jahrgaenge = finde_jahresrechnungen()
    pro_jahr = {}      # jahr -> extrahierte werte
    verwendet = []
    for jahr in sorted(jahrgaenge):
        try:
            roh = lade(jahrgaenge[jahr])
            zeilen, _ = _zeilen_aus_bytes(roh)
            ueb = extrahiere_uebersicht(zeilen)
            a8 = extrahiere_a8_kennzahlen(zeilen)
            bil = extrahiere_bilanz_summen(zeilen)
            proben = pruefe(ueb, bil)
            # Qualitaetstor: Bilanzprobe muss aufgehen
            bilanz_ok = any(ok for n, ok, _ in proben if "Bilanz" in n)
            if not bilanz_ok:
                continue
            pro_jahr[jahr] = {"a8": a8, "ueb": ueb}
            verwendet.append(jahr)
        except Exception:
            continue

    # Jahr-zu-PDF-Zuordnung der tatsaechlich verwendeten Jahrgaenge merken
    quellen = {j: jahrgaenge[j] for j in verwendet}

    kennzahlen = {}
    # 1) Die acht A8-Kennzahlen
    for sch in A8_SCHLUESSEL:
        meta = KENNZAHL_META[sch]
        reihe = []
        for jahr in sorted(pro_jahr):
            wert = pro_jahr[jahr]["a8"].get(sch)
            if wert is not None:
                reihe.append([jahr, wert, beurteile(sch, wert)])
        if reihe:
            kennzahlen[sch] = {
                "name": meta["name"], "einheit": meta["einheit"],
                "erklaerung": meta["erklaerung"],
                "was_bedeutet": meta["was_bedeutet"],
                "reihe": reihe,
                "zonen": zonen_fuer(sch),
            }

    # 2) Steuerfuesse (natuerliche + juristische Personen)
    for sch, idx, name in (("steuerfuss_np", 0, "Steuerfuss natürliche Personen"),
                           ("steuerfuss_jp", 1, "Steuerfuss juristische Personen")):
        reihe = []
        for jahr in sorted(pro_jahr):
            werte = pro_jahr[jahr]["ueb"].get(sch)
            if werte:
                reihe.append([jahr, werte[0], ""])
        if reihe:
            kennzahlen[sch] = {
                "name": name, "einheit": "%",
                "erklaerung": "Der Steuerfuss bestimmt, wie viel Gemeindesteuer "
                              "auf Basis der einfachen Kantonssteuer erhoben "
                              "wird. Ein tieferer Steuerfuss bedeutet tiefere "
                              "Steuern.",
                "was_bedeutet": "In Prozent der einfachen Staatssteuer. "
                                "Vergleichbar nur innerhalb des Kantons, da "
                                "jeder Kanton eigene Steuertarife kennt.",
                "reihe": reihe,
            }

    # 3) Ergebnis der Erfolgsrechnung (Ertrags-/Aufwandueberschuss)
    reihe = []
    for jahr in sorted(pro_jahr):
        erg = pro_jahr[jahr]["ueb"].get("ergebnis_er")
        if erg:
            reihe.append([jahr, erg[0], ""])
    if reihe:
        kennzahlen["ergebnis_er"] = {
            "name": "Ergebnis Erfolgsrechnung", "einheit": "CHF",
            "erklaerung": "Das Jahresergebnis der Gemeinde: Überschuss (positiv) "
                          "oder Fehlbetrag (negativ) aus allen Erträgen und "
                          "Aufwänden eines Jahres.",
            "was_bedeutet": "Ein positiver Wert bedeutet, dass die Gemeinde mehr "
                            "eingenommen als ausgegeben hat.",
            "reihe": reihe,
            # Jahresereignis: der Wert IST das Jahresergebnis, eine
            # "Veraenderung" waere die Veraenderung einer Veraenderung.
            "jahresereignis": True,
            # Zwei Zonen: Ueberschuss (gruen) ab 0, Fehlbetrag (rot) darunter.
            "zonen": [
                {"von": 0, "bis": None, "farbe": "gruen", "label": "Überschuss"},
                {"von": None, "bis": 0, "farbe": "rot", "label": "Fehlbetrag"},
            ],
        }

    return {"jahre": verwendet, "kennzahlen": kennzahlen, "quellen": quellen}


def diagnose_finanz_json(lade_funktion=None):
    """Gibt die fertige Finanz-Struktur als JSON aus (zur Kontrolle)."""
    import json
    daten = baue_finanz_zeitreihen(lade_funktion)
    print(json.dumps(daten, ensure_ascii=False, indent=2))


def diagnose_uebersicht():
    """Kompakte Bilanz ueber alle Jahrgaenge: pro Jahr eine Zeile mit
    Anzahl erkannter A8-Kennzahlen, Steuerfuss/Ergebnis vorhanden,
    Kontrollsummen. Zeigt, ab welchem Jahr die Erfassung wie vollstaendig ist."""
    print(f"\n{'=' * 74}")
    print(f"{'Jahr':<6}{'A8':>5}{'Steuerf.':>10}{'Ergebnis':>10}"
          f"{'Proben':>9}{'Bilanz-Aktiven':>18}   Status")
    print(f"{'-' * 74}")
    jahrgaenge = finde_jahresrechnungen()
    for jahr in sorted(jahrgaenge, reverse=True):
        url = jahrgaenge[jahr]
        try:
            roh = _lade_pdf(url)
            zeilen, _ = _zeilen_aus_bytes(roh)
            ueb = extrahiere_uebersicht(zeilen)
            a8 = extrahiere_a8_kennzahlen(zeilen)
            bil = extrahiere_bilanz_summen(zeilen)
            proben = pruefe(ueb, bil)
            n_a8 = sum(1 for s in A8_SCHLUESSEL if s in a8)
            steuer = "ja" if "steuerfuss_np" in ueb else "-"
            ergebnis = "ja" if "ergebnis_er" in ueb else "-"
            n_ok = sum(1 for _, ok, _ in proben if ok)
            aktiven = bil.get("aktiven", "-")
            status = "vollstaendig" if (n_a8 == 8 and n_ok == 2) else \
                     ("teilweise" if n_a8 > 0 else "keine A8")
            print(f"{jahr:<6}{n_a8:>3}/8{steuer:>10}{ergebnis:>10}"
                  f"{n_ok:>6}/2{str(aktiven):>18}   {status}")
        except Exception as e:
            print(f"{jahr:<6}{'FEHLER':>5}   {str(e)[:40]}")
    print(f"{'=' * 74}")
    print("Legende: A8 = erkannte Finanzkennzahlen (von 8), "
          "Proben = bestandene Kontrollsummen (von 2)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--uebersicht":
        diagnose_uebersicht()
    elif len(sys.argv) > 1 and sys.argv[1] == "--diagnose":
        diagnose()
    else:
        ergebnis = verarbeite(sys.argv[1])
        print(json.dumps(ergebnis, ensure_ascii=False, indent=2))
