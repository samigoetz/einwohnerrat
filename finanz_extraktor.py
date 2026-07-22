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


# Zahl-Muster im Fliesstext: 1'234'567 oder -1'234 oder 1'234.56
ZAHL = r"-?\d{1,3}(?:['\u2019]\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?"


def _zahlen_der_zeile(zeile):
    """Alle Zahlen einer Textzeile in Reihenfolge."""
    return [_zahl(m) for m in re.findall(ZAHL, zeile)]


def extrahiere_uebersicht(text):
    """Seite 'Uebersicht': Erfolgsrechnung-Eckwerte, Steuerfuesse.
    Rueckgabe je Kennzahl: (rechnung, budget, vorjahr) soweit vorhanden."""
    d = {}
    for zeile in text.splitlines():
        z = zeile.strip()
        for schluessel, muster in (
            ("gesamtaufwand", r"^Gesamtaufwand\b"),
            ("gesamtertrag", r"^Gesamtertrag\b"),
            ("ergebnis_er", r"^Ertrags-.*Aufwand.berschuss"),
            ("operatives_ergebnis", r"^Operatives Ergebnis\b"),
        ):
            if re.search(muster, z):
                zahlen = _zahlen_der_zeile(z)
                if zahlen:
                    d[schluessel] = zahlen
        # Steuerfuesse: "Steuerfuss natuerliche Personen 93% 93% 96%"
        if re.search(r"Steuerfuss nat.rliche Personen", z):
            proz = re.findall(r"(\d+)\s*%", z)
            if proz:
                d["steuerfuss_np"] = [int(p) for p in proz]
        if re.search(r"Steuerfuss juristische Personen", z):
            proz = re.findall(r"(\d+)\s*%", z)
            if proz:
                d["steuerfuss_jp"] = [int(p) for p in proz]
    return d


def extrahiere_a8_kennzahlen(text):
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
    for zeile in text.splitlines():
        z = zeile.strip()
        for schluessel, m in muster.items():
            if re.match(m, z):
                # erste zwei Prozentwerte der Zeile = Rechnung, Vorjahr
                proz = re.findall(r"(-?\d+(?:\.\d+)?)\s*%", z)
                if len(proz) >= 2:
                    kennzahlen[schluessel] = [float(proz[0]), float(proz[1])]
        # Nettoschuld pro Einwohner (Franken, kann negativ = Vermoegen)
        if re.match(r"Nettoschuld I pro Einwohner", z):
            zahlen = _zahlen_der_zeile(z)
            if len(zahlen) >= 2:
                kennzahlen["nettoschuld_pro_kopf"] = zahlen[:2]
    return kennzahlen


def extrahiere_bilanz_summen(text):
    """Bilanz-Hauptsummen fuer die Kontrollprobe Aktiven = Passiven."""
    d = {}
    for zeile in text.splitlines():
        z = zeile.strip()
        # Ordnungsziffer am Zeilenanfang entfernen, damit nicht "1" (aus
        # "1 Aktiven") faelschlich als Betrag gelesen wird.
        for schluessel, kopf in (("aktiven", r"^1 Aktiven\b"),
                                 ("passiven", r"^2 Passiven\b"),
                                 ("eigenkapital", r"^29 Eigenkapital\b")):
            if re.match(kopf, z):
                rest = re.sub(kopf, "", z).strip()
                zahlen = _zahlen_der_zeile(rest)
                if zahlen:
                    d[schluessel] = zahlen[0]
    return d


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


def verarbeite(pfad):
    import pdfplumber
    seiten_text = []
    with pdfplumber.open(pfad) as pdf:
        for seite in pdf.pages:
            seiten_text.append(seite.extract_text() or "")
    volltext = "\n".join(seiten_text)

    uebersicht = extrahiere_uebersicht(volltext)
    a8 = extrahiere_a8_kennzahlen(volltext)
    bilanz = extrahiere_bilanz_summen(volltext)
    proben = pruefe(uebersicht, bilanz)

    return {
        "seiten": len(seiten_text),
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


def _text_aus_pdf_bytes(roh):
    import pdfplumber
    import io
    seiten = []
    with pdfplumber.open(io.BytesIO(roh)) as pdf:
        for seite in pdf.pages:
            seiten.append(seite.extract_text() or "")
    return "\n".join(seiten), len(seiten)


def diagnose():
    """Laedt die echten PDFs 2024 und 2025, strukturiert sie und zeigt,
    ob die Kontrollsummen aufgehen. Aendert nichts."""
    import json
    for jahr, url in JAHRESRECHNUNGEN.items():
        print(f"\n{'=' * 60}")
        print(f"JAHRESRECHNUNG {jahr}")
        print(f"{'=' * 60}")
        try:
            roh = _lade_pdf(url)
            print(f"  PDF geladen: {len(roh):,} Bytes")
            volltext, n_seiten = _text_aus_pdf_bytes(roh)
            print(f"  Seiten: {n_seiten}, Textlaenge: {len(volltext):,} Zeichen")
        except Exception as e:
            print(f"  FEHLER beim Laden/Lesen: {e}")
            continue

        ueb = extrahiere_uebersicht(volltext)
        a8 = extrahiere_a8_kennzahlen(volltext)
        bil = extrahiere_bilanz_summen(volltext)
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
        a8_zahl = len(a8)
        print(f"\n  Ergebnis {jahr}: {a8_zahl}/8 A8-Kennzahlen, "
              f"{sum(1 for _, ok, _ in proben if ok)}/{len(proben)} Proben ok")
    print(f"\n{'=' * 60}\nEnde Finanz-Diagnose\n{'=' * 60}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnose":
        diagnose()
    else:
        ergebnis = verarbeite(sys.argv[1])
        print(json.dumps(ergebnis, ensure_ascii=False, indent=2))
