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
    """Seite A8 Finanzkennzahlen: je Kennzahl (rechnung, vorjahr) in %."""
    kennzahlen = {}
    muster = {
        "nettoverschuldungsquotient": r"Nettoverschuldungsquotient",
        "selbstfinanzierungsgrad": r"Selbstfinanzierungsgrad",
        "zinsbelastungsanteil": r"Zinsbelastungsanteil",
        "selbstfinanzierungsanteil": r"Selbstfinanzierungsanteil",
        "kapitaldienstanteil": r"Kapitaldienstanteil",
        "bruttoverschuldungsanteil": r"Bruttoverschuldungsanteil",
        "investitionsanteil": r"Investitionsanteil",
    }
    for text, zahlen in zeilen:
        z = text.strip()
        for schluessel, m in muster.items():
            if re.match(m, z):
                proz = re.findall(r"(-?\d+(?:\.\d+)?)\s*%", z)
                if len(proz) >= 2:
                    kennzahlen[schluessel] = [float(proz[0]), float(proz[1])]
        if re.match(r"Nettoschuld I pro Einwohner", z):
            if len(zahlen) >= 2:
                kennzahlen["nettoschuld_pro_kopf"] = zahlen[:2]
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
    # Probe 1: Gesamtaufwand + Gesamtertrag = Ergebnis (Vorzeichen beachten)
    if "gesamtaufwand" in uebersicht and "gesamtertrag" in uebersicht \
            and "ergebnis_er" in uebersicht:
        auf = uebersicht["gesamtaufwand"][0]
        ert = uebersicht["gesamtertrag"][0]
        erg = uebersicht["ergebnis_er"][0]
        summe = auf + ert
        ok = abs(summe - erg) <= 2  # Rundungstoleranz
        proben.append(("Aufwand+Ertrag=Ergebnis (Erfolgsrechnung)", ok,
                       f"{auf} + {ert} = {summe}, ausgewiesen {erg}"))
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
    "2024": "https://neuhausen.ch/fileupload/Jahresrechnung 2024 genehmigte Version.pdf",
    "2025": "https://neuhausen.ch/fileupload/Jahresrechnung 2025.pdf",
}


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
        print(f"\n  Ergebnis {jahr}: {len(a8)}/8 A8-Kennzahlen, "
              f"{sum(1 for _, ok, _ in proben if ok)}/{len(proben)} Proben ok")
    print(f"\n{'=' * 60}\nEnde Finanz-Diagnose\n{'=' * 60}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnose":
        diagnose()
    else:
        ergebnis = verarbeite(sys.argv[1])
        print(json.dumps(ergebnis, ensure_ascii=False, indent=2))
