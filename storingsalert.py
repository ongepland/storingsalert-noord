#!/usr/bin/env python3
"""
Storingsalert trein Arriva (v6)

Haalt elke run de actieve verstoringen op uit de NS Disruptions API (InfoPlus),
filtert op Arriva en plaatst via Teams-werkstromen (webhooks) een kaart bij:
  [NIEUW]        nieuwe melding
  [WEER ACTIEF]  dezelfde melding komt binnen 24 uur na afmelden terug
  [UPDATE]       reizigersrelevante wijziging (impact, prognose >= 30 min, vervangend vervoer, advies)
  [OPGELOST]     InfoPlus meldt 2 controles op rij "voorbij"
  [AFGEMELD]     melding verdwenen zonder dat [OPGELOST] al gemeld was
  [FOUT]/[HERSTELD]  NS-API onbereikbaar / weer bereikbaar (1x)

Elke kaart gaat naar het kanaal 'Landelijk' (TEAMS_WEBHOOK_URL) en daarnaast naar
het kanaal van de regio (Noord / Oost / Limburg), via TEAMS_WEBHOOK_<REGIO>.
Calamiteiten (landelijke NS-berichten) worden altijd gemeld; de kaart toont of en waar ze
Arriva raken.

Elke wijziging komt in data/logboek.csv, ook als er geen kaart voor geplaatst is
(kolom 'geplaatst'). Basis voor latere analyses (o.a. vergelijking met ASL).

Instellingen (GitHub Secrets):
  NS_API_KEY, TEAMS_WEBHOOK_URL (Alle), TEAMS_WEBHOOK_NOORD, TEAMS_WEBHOOK_OOST, TEAMS_WEBHOOK_LIMBURG
Optioneel: TEST_MODE=1, DRY_RUN=1, FIXTURE=pad, NS_API_URL
"""

import csv
import gzip
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

VERSIE = "2026-11-26 v6"
API_URL = os.environ.get("NS_API_URL", "https://gateway.apiportal.ns.nl/disruptions/v3?isActive=true")
TYPES = {"DISRUPTION", "CALAMITY"}
TZ = ZoneInfo("Europe/Amsterdam")
AFMELD_NA_KEER = {"DISRUPTION": 3, "CALAMITY": 12}   # controles weg -> afgemeld (15 / 60 min)
NAAM_ALLE = "Landelijk"                         # kanaalnaam van de totale stroom
VOORBIJ_NA_KEER = 2      # 2 controles "voorbij" -> [OPGELOST]
PROGNOSE_DREMPEL = 30    # minuten
WEER_ACTIEF_UREN = 24    # zelfde melding binnen zoveel uur terug -> [WEER ACTIEF]

BASE = Path(__file__).resolve().parent
STATIONS_FILE = BASE / "stations.txt"
STATE_FILE = BASE / "data" / "state.json"
LOG_FILE = BASE / "data" / "logboek.csv"

LOG_KOLOMMEN = [
    "tijdstip", "gebeurtenis", "geplaatst", "id", "type", "stromen", "regio", "titel", "reden",
    "impact", "situatie", "verwachting", "prognose", "vervangend_vervoer", "advies", "fase",
    "getroffen", "infoplus_start", "infoplus_publicatie", "eerst_gezien", "duur_minuten",
]

# Vervoerderslabels -> concessie (aanvulling als stations geen uitsluitsel geven)
LABEL_REGIO = {"rrreis": "Achterhoek", "blauwnet": "Vechtdallijnen"}


# ---------- hulpfuncties ----------

def nu():
    return datetime.now(TZ)


def norm(tekst):
    t = unicodedata.normalize("NFKD", tekst or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


def tijd(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d-%m %H:%M")
    except (TypeError, ValueError):
        return ""


def iso_ns(waarde):
    """NS-tijden zoals 2026-11-12T05:25:00+0100 -> datetime-string die fromisoformat snapt."""
    if not waarde:
        return ""
    w = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(waarde))
    try:
        return datetime.fromisoformat(w).isoformat()
    except ValueError:
        return ""


# ---------- stations en stromen ----------

def laad_stations():
    """Leest stations.txt: filter-codes/namen, regio per code/naam, knooppunten, stromen."""
    cfg = {"filter_codes": set(), "filter_namen": set(), "regio_code": {}, "regio_naam": {},
           "knoop_code": set(), "filter_regios": set(), "stroom": {}, "tekstnamen": {}}
    regio = None
    for regel in STATIONS_FILE.read_text(encoding="utf-8").splitlines():
        r = regel.split("#", 1)[0].strip()
        if not r:
            continue
        sleutel = r.split("=", 1)[0].strip().lower()
        if sleutel == "filter_regios":
            cfg["filter_regios"] = {norm(x) for x in r.split("=", 1)[1].split(",") if x.strip()}
            continue
        if sleutel == "stromen":
            for paar in r.split("=", 1)[1].split(","):
                if ":" in paar:
                    conc, stroom = paar.split(":", 1)
                    cfg["stroom"][conc.strip()] = stroom.strip()
            continue
        if r.startswith("[") and r.endswith("]"):
            regio = r[1:-1].strip()
            continue
        if not regio:
            continue
        delen = r.split(None, 1)
        code = norm(delen[0])
        naam = delen[1].strip() if len(delen) > 1 else ""
        knoop = naam.lower().endswith(" knooppunt") or naam.lower() == "knooppunt"
        if knoop:
            naam = naam[: -len("knooppunt")].strip()
            cfg["knoop_code"].add(code)
        n = norm(naam)
        cfg["regio_code"].setdefault(code, set()).add(regio)
        if n:
            cfg["regio_naam"].setdefault(n, set()).add(regio)
            # voor tekstherkenning in calamiteiten: naam zonder toevoeging als "(Ostfriesl)"
            kort = re.sub(r"\s*\(.*\)$", "", naam)
            if len(kort) >= 4:
                info = cfg["tekstnamen"].setdefault(kort, {"regios": set(), "knoop": knoop})
                info["regios"].add(regio)
                info["knoop"] = info["knoop"] and knoop
        if norm(regio) in cfg["filter_regios"] and not knoop:
            cfg["filter_codes"].add(code)
            if n:
                cfg["filter_namen"].add(n)
    return cfg


def zoek(obj, sleutel):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == sleutel:
                yield v
            yield from zoek(v, sleutel)
    elif isinstance(obj, list):
        for item in obj:
            yield from zoek(item, sleutel)


def stationlijst(obj):
    """Platte lijst (code, naam) uit een (geneste) stations-structuur, volgorde behouden."""
    uit, stapel = [], [obj]
    while stapel:
        item = stapel.pop(0)
        if isinstance(item, list):
            stapel = list(item) + stapel
        elif isinstance(item, dict) and item.get("name"):
            uit.append((item.get("stationCode", ""), item["name"]))
    return uit


def stations_van(m):
    gevonden = {}
    for st in zoek(m.get("publicationSections", []), "stations"):
        for c, n in stationlijst(st):
            gevonden[n] = c
    return [(c, n) for n, c in gevonden.items()]


def getroffen(m):
    """Het echt getroffen deel ('consequence'), als 'Van – Tot'."""
    delen = []
    for ps in m.get("publicationSections", []) or []:
        sec = ((ps or {}).get("consequence") or {}).get("section") or {}
        lijst = stationlijst(sec.get("stations", []))
        if lijst:
            stuk = f"{lijst[0][1]} – {lijst[-1][1]}" if len(lijst) > 1 else lijst[0][1]
            if stuk not in delen:
                delen.append(stuk)
    return "; ".join(delen)


def vervoerders(m):
    ops = set()
    for lijst in zoek(m.get("publicationSections", []), "operators"):
        if isinstance(lijst, list):
            ops.update(str(o) for o in lijst)
    return ops


def tekst_van(m):
    ts = m.get("timespans") or []
    delen = [m.get("title") or "", m.get("description") or ""]
    delen += [(t.get("situation") or {}).get("label", "") for t in ts]
    return " ".join(d for d in delen if d)


# Stationsnamen die ook een gewoon woord zijn; niet gebruiken bij herkenning in vrije tekst
GEWONE_WOORDEN = {"Echt", "Leer", "Dalen", "Goor"}


def stations_in_tekst(tekst, cfg):
    """Stationsnamen (met hoofdletter, als heel woord) die in een vrije tekst voorkomen."""
    gevonden = {}
    for naam, info in cfg["tekstnamen"].items():
        if naam in GEWONE_WOORDEN:
            continue
        if re.search(r"(?<![\w-])" + re.escape(naam) + r"(?![\w-])", tekst):
            gevonden[naam] = info
    # langere namen gaan voor (bv. 'Maastricht Randwyck' telt niet ook als 'Maastricht')
    for kort in list(gevonden):
        if any(kort != lang and kort in lang for lang in gevonden):
            del gevonden[kort]
    return gevonden


def analyseer_calamiteit(m, cfg):
    """Calamiteiten worden altijd gemeld. Deze functie bepaalt of en waar ze Arriva raken.
    Geeft dict: betreft (ja / mogelijk / niet herkend), herkend (tekst), regios, alle_regios."""
    tekst = tekst_van(m)
    t = norm(tekst)
    gevonden = stations_in_tekst(tekst, cfg)
    eigen = sorted(n for n, i in gevonden.items() if not i["knoop"])
    score = {}
    for naam, info in gevonden.items():
        for r in info["regios"]:
            score[r] = score.get(r, 0) + (0.5 if info["knoop"] else 1.0)
    regios = sorted(r for r, s in score.items() if s == max(score.values())) if score else []
    if eigen or len(gevonden) >= 2:
        return {"betreft": "ja", "herkend": ", ".join(sorted(gevonden)), "regios": regios, "alle_regios": False}
    woorden = [w for w in ("arriva", "streekvervoer") if w in t]
    if woorden:
        return {"betreft": "mogelijk, alle regio's", "herkend": "tekst noemt " + " en ".join(woorden),
                "regios": [], "alle_regios": True}
    if gevonden:
        return {"betreft": "mogelijk (alleen overstapstation)", "herkend": ", ".join(sorted(gevonden)),
                "regios": [], "alle_regios": False}
    return {"betreft": "niet herkend (landelijk bericht)", "herkend": "", "regios": [], "alle_regios": False}


def is_arriva(m, cfg):
    if m.get("type") == "CALAMITY":
        return True, "calamiteit (wordt altijd gemeld)"
    if any("arriva" in o.lower() for o in vervoerders(m)):
        return True, "label Arriva"
    geraakt = sorted(n for c, n in stations_van(m)
                     if norm(c) in cfg["filter_codes"] or norm(n) in cfg["filter_namen"])
    if geraakt:
        return True, "station(s): " + ", ".join(geraakt)
    return False, ""


def eerste_link(m):
    """Een eventuele 'meer informatie'-link uit het NS-bericht (als NS die meestuurt)."""
    for sleutel in ("url", "link", "infoUrl"):
        for v in zoek(m, sleutel):
            if isinstance(v, str) and v.startswith("http"):
                return v
            if isinstance(v, dict) and str(v.get("uri", v.get("href", ""))).startswith("http"):
                return v.get("uri") or v.get("href")
    return ""


def bepaal_regios(m, cfg):
    """Concessie(s) met de meeste stations (knooppunten tellen half); anders via label of tekst."""
    score = {}
    for c, n in stations_van(m):
        regs = cfg["regio_code"].get(norm(c)) or cfg["regio_naam"].get(norm(n)) or set()
        gewicht = 0.5 if norm(c) in cfg["knoop_code"] else 1.0
        for r in regs:
            score[r] = score.get(r, 0) + gewicht
    if not score and m.get("type") == "CALAMITY":
        return analyseer_calamiteit(m, cfg)["regios"]
    if score:
        top = max(score.values())
        return sorted(r for r, s in score.items() if s == top)
    for o in vervoerders(m):
        for sleutel, regio in LABEL_REGIO.items():
            if sleutel in o.lower():
                return [regio]
    return []


def stromen_van(regios, cfg):
    uit = []
    for r in regios:
        s = cfg["stroom"].get(r)
        if s and s not in uit:
            uit.append(s)
    return uit


# ---------- inhoud en betekenis ----------

def inhoud(m):
    ts = m.get("timespans") or []
    situatie = [(t.get("situation") or {}).get("label", "") for t in ts]
    alt = [(t.get("alternativeTransport") or {}).get("label", "") for t in ts]
    advies = [a for t in ts for a in (t.get("advices") or [])]
    oorzaak = ""
    for t in ts:
        oorzaak = (t.get("cause") or {}).get("label", "") or oorzaak
    return {
        "titel": (m.get("title") or "").rstrip("."),
        "fase": (m.get("phase") or {}).get("label", ""),
        "situatie": " ".join(s for s in situatie if s) or m.get("description", ""),
        "verwachting": (m.get("expectedDuration") or {}).get("description", ""),
        "vervangend_vervoer": "; ".join(a for a in alt if a),
        "advies": " ".join(advies),
        "oorzaak": oorzaak,
    }


def impact(inh):
    s = norm(inh["situatie"] + " " + inh["fase"])
    if "voorbij" in s:
        return "voorbij"
    if re.search(r"geen (\w+ )?(treinen|sprinters|stoptreinen|sneltreinen|intercity)", s) or "geen treinverkeer" in s:
        return "geen treinen"
    if "minder" in s:
        return "minder treinen"
    if "vertraging" in s:
        return "vertraging"
    return "verstoring"


def prognose(inh):
    """Prognose in minuten na middernacht, uit 'Dit duurt tot ongeveer 8:15 uur.'"""
    m = re.search(r"(\d{1,2})[:.](\d{2})", inh["verwachting"])
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def vervoer_soort(tekst):
    """'er rijden (ook/nog) stopbussen' -> 'stopbussen'. Alleen de soort telt, niet de formulering."""
    t = norm(tekst)
    if not t or re.search(r"geen (\w+ )?(bussen|vervangend)", t):
        return ""
    soorten = []
    if "stopbus" in t:
        soorten.append("stopbussen")
    if "snelbus" in t:
        soorten.append("snelbussen")
    if "taxi" in t:
        soorten.append("taxi's")
    if not soorten and "bus" in t:
        soorten.append("bussen")
    return " en ".join(soorten) or "ander vervangend vervoer"


def kern(inh):
    """Het reizigersrelevante deel waarop we updates baseren."""
    return {"impact": impact(inh), "prognose": prognose(inh),
            "vervangend_vervoer": vervoer_soort(inh.get("vervangend_vervoer", "")),
            "advies": inh.get("advies", "")}


def hhmm(minuten):
    return f"{minuten // 60}:{minuten % 60:02d}"


def wezenlijk_verschil(oud, nieuw):
    """Lijst met (veld, oud, nieuw) die voor de reiziger relevant zijn."""
    v = []
    if oud.get("impact") != nieuw["impact"]:
        v.append(("Impact", oud.get("impact") or "(onbekend)", nieuw["impact"]))
    po, pn = oud.get("prognose"), nieuw["prognose"]
    if po is not None and pn is not None:
        d = abs(pn - po)
        d = min(d, 1440 - d)
        if d >= PROGNOSE_DREMPEL:
            v.append(("Prognose", hhmm(po), hhmm(pn)))
    elif po is None and pn is not None:
        v.append(("Prognose", "(geen)", hhmm(pn)))
    vo = vervoer_soort(oud.get("vervangend_vervoer", "")) if "vervangend_vervoer" in oud else ""
    # records uit v5 bewaarden de ruwe tekst; normaliseer ook die
    if vo != nieuw["vervangend_vervoer"]:
        v.append(("Vervangend vervoer", vo or "(geen)", nieuw["vervangend_vervoer"] or "(geen)"))
    if oud.get("advies", "") != nieuw["advies"]:
        v.append(("Advies", oud.get("advies") or "(geen)", nieuw["advies"] or "(geen)"))
    return v


# ---------- opslag ----------

def laad_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"actief": {}, "fout": False}


def bewaar_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["maand"] = nu().strftime("%Y-%m")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def schrijf_log(regels):
    if not regels:
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    nieuw = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_KOLOMMEN, delimiter=";")
        if nieuw:
            w.writeheader()
        for r in regels:
            w.writerow({k: r.get(k, "") for k in LOG_KOLOMMEN})


def logregel(moment, gebeurtenis, geplaatst, mid, rec, inh, duur=""):
    p = prognose(inh) if inh.get("verwachting") else None
    return {
        "tijdstip": moment.isoformat(timespec="seconds"), "gebeurtenis": gebeurtenis,
        "geplaatst": "ja" if geplaatst else "nee", "id": mid, "type": rec.get("type", ""),
        "stromen": ", ".join(rec.get("stromen", [])), "regio": rec.get("regio", ""),
        "titel": inh.get("titel", ""),
        "reden": rec.get("reden", "") + (f" · betreft Arriva: {rec['betreft']}" if rec.get("betreft") else ""),
        "impact": impact(inh) if inh.get("situatie") or inh.get("fase") else "",
        "situatie": inh.get("situatie", ""), "verwachting": inh.get("verwachting", ""),
        "prognose": hhmm(p) if p is not None else "",
        "vervangend_vervoer": inh.get("vervangend_vervoer", ""), "advies": inh.get("advies", ""),
        "fase": inh.get("fase", ""), "getroffen": rec.get("getroffen", ""),
        "infoplus_start": rec.get("infoplus_start", ""), "infoplus_publicatie": rec.get("infoplus_publicatie", ""),
        "eerst_gezien": rec.get("eerst_gezien", ""), "duur_minuten": duur,
    }


# ---------- bron ----------

def haal_op():
    fixture = os.environ.get("FIXTURE")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))
    key = os.environ.get("NS_API_KEY", "")
    if not key:
        raise RuntimeError("NS_API_KEY ontbreekt")
    req = urllib.request.Request(API_URL, headers={
        "Ocp-Apim-Subscription-Key": key, "Cache-Control": "no-cache",
        "Accept": "application/json", "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        ruw = r.read()
    while ruw[:2] == b"\x1f\x8b":
        ruw = gzip.decompress(ruw)
    data = json.loads(ruw.decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Onverwacht antwoord van de NS-API (geen lijst)")
    return data


# ---------- berichten ----------

KLEUR = {"NIEUW": "Attention", "WEER ACTIEF": "Attention", "UPDATE": "Warning", "OPGELOST": "Good",
         "AFGEMELD": "Good", "FOUT": "Attention", "HERSTELD": "Good", "TEST": "Accent"}


def bericht(soort, titel, feiten, intro=None, stromen=None):
    return {"soort": soort, "titel": titel, "intro": intro or [], "stromen": stromen or [],
            "feiten": [(k, v) for k, v in feiten if v]}


def label_stromen(rec):
    if rec.get("alle_regios"):
        return "Landelijk"
    return " / ".join(rec.get("stromen") or []) or ("Landelijk" if rec.get("type") == "CALAMITY" else "Overig")


def kaart_melding(soort, m, rec, inh, intro=None):
    calamiteit = m.get("type") == "CALAMITY"
    titel = (("Calamiteit · " if calamiteit else "") + f"{label_stromen(rec)} · {inh['titel']}"
             + (f": {inh['oorzaak']}" if inh["oorzaak"] else ""))
    ops = ", ".join(sorted(vervoerders(m)))
    if calamiteit:
        feiten = [
            ("Bericht", inh["situatie"]),
            ("Verwachte duur", inh["verwachting"]),
            ("Bron", "Landelijk NS-bericht (calamiteit in InfoPlus)"),
            ("Vervoerder", ops or "niet vermeld in InfoPlus"),
            ("Betreft Arriva", rec.get("betreft", "")),
            ("Herkend aan", rec.get("herkend", "")),
            ("Regio", " / ".join(rec.get("stromen") or []) if not rec.get("alle_regios") else "alle regio's"),
            ("Concessie", rec.get("regio", "") if rec.get("regio") != " / ".join(rec.get("stromen") or []) else ""),
            ("Meer informatie", rec.get("link", "")),
        ]
    else:
        feiten = [
            ("Situatie", inh["situatie"]),
            ("Verwachte duur", inh["verwachting"]),
            ("Vervangend vervoer", inh["vervangend_vervoer"]),
            ("Advies", inh["advies"]),
            ("Getroffen", rec.get("getroffen", "")),
            ("Concessie", rec.get("regio", "")),
            ("Vervoerder (InfoPlus)", ops),
            ("Waarom", rec.get("reden", "")),
            ("Start in InfoPlus", tijd(rec.get("infoplus_start"))),
        ]
    feiten += [("Eerst gezien", tijd(rec.get("eerst_gezien"))), ("Melding-id", str(m.get("id", "")))]
    return bericht(soort, titel, feiten, intro, rec.get("stromen"))


def kaart(b, kanaal=""):
    body = [{"type": "TextBlock", "text": f"[{b['soort']}] {b['titel']}", "weight": "Bolder",
             "size": "Medium", "color": KLEUR.get(b["soort"], "Default"), "wrap": True}]
    for regel in b["intro"]:
        body.append({"type": "TextBlock", "text": regel, "wrap": True, "spacing": "Small"})
    if b["feiten"]:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in b["feiten"]]})
    voet = f"Storingsalert trein Arriva · versie {VERSIE} · bron: InfoPlus via NS API"
    if kanaal:
        voet += f" · kanaal: {kanaal}"
    body.append({"type": "TextBlock", "text": voet, "isSubtle": True, "size": "Small", "wrap": True})
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body, "msteams": {"width": "Full"}},
    }]}


def webhook(stroom):
    if stroom == "Alle":
        return os.environ.get("TEAMS_WEBHOOK_URL", "")
    return os.environ.get("TEAMS_WEBHOOK_" + norm(stroom).upper().replace(" ", "_"), "")


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if not 200 <= r.status < 300:
                raise RuntimeError(f"Teams antwoordde met status {r.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Teams weigerde het bericht (status {e.code}).") from e


REGIO_FOUTEN = []


def stuur(b, alleen_alle=False):
    """Altijd eerst 'Alle' (fout = run stopt, volgende run probeert opnieuw).
    Daarna het regiokanaal; een fout daar wordt gemeld maar stopt de run niet."""
    doelen = ["Alle"] + ([] if alleen_alle else [s for s in b.get("stromen", []) if s != "Alle"])
    if os.environ.get("DRY_RUN") == "1":
        print("=" * 70)
        print(f"[{b['soort']}] {b['titel']}   -> {', '.join(doelen)}")
        for r in b["intro"]:
            print("  ", r)
        for k, v in b["feiten"]:
            print(f"   {k}: {v}")
        return
    url = webhook("Alle")
    if not url:
        raise RuntimeError("TEAMS_WEBHOOK_URL ontbreekt")
    post(url, kaart(b, NAAM_ALLE))
    for stroom in doelen[1:]:
        url = webhook(stroom)
        if not url:
            print(f"Let op: geen webhook voor stroom '{stroom}' (secret TEAMS_WEBHOOK_{norm(stroom).upper()})")
            continue
        try:
            post(url, kaart(b, stroom))
        except (OSError, RuntimeError) as e:
            REGIO_FOUTEN.append(f"{stroom}: {e}")
            print(f"Let op: plaatsen in kanaal {stroom} mislukt: {e}")


# ---------- hoofdlogica ----------

def regio_velden(m, cfg):
    """Regio, stromen en (bij calamiteiten) de duiding of het Arriva raakt."""
    if m.get("type") == "CALAMITY":
        a = analyseer_calamiteit(m, cfg)
        stromen = sorted(set(cfg["stroom"].values())) if a["alle_regios"] else stromen_van(a["regios"], cfg)
        return {"regio": " / ".join(a["regios"]), "stromen": stromen, "alle_regios": a["alle_regios"],
                "betreft": a["betreft"], "herkend": a["herkend"], "link": eerste_link(m)}
    regios = bepaal_regios(m, cfg)
    return {"regio": " / ".join(regios), "stromen": stromen_van(regios, cfg)}


def nieuw_record(m, inh, reden, cfg, moment):
    rec = {
        "inhoud": inh, "geplaatst": kern(inh), "reden": reden, "type": m.get("type", ""),
        "getroffen": getroffen(m),
        "infoplus_start": iso_ns(m.get("start")), "infoplus_publicatie": iso_ns(m.get("releaseTime")),
        "eerst_gezien": moment.isoformat(), "laatst_gezien": moment.isoformat(),
        "afwezig": 0, "voorbij": 0, "opgelost": False,
    }
    rec.update(regio_velden(m, cfg))
    return rec


def main():
    moment = nu()
    cfg = laad_stations()
    state = laad_state()
    state.setdefault("actief", {})
    state.setdefault("recent", {})
    uit, logs = [], []

    # recent afgemelde meldingen ouder dan WEER_ACTIEF_UREN vergeten
    for mid, r in list(state["recent"].items()):
        if isinstance(r, str):
            r = state["recent"][mid] = {"afgemeld": r, "eerst_gezien": ""}
        try:
            if moment - datetime.fromisoformat(r["afgemeld"]) > timedelta(hours=WEER_ACTIEF_UREN):
                del state["recent"][mid]
        except (ValueError, KeyError):
            del state["recent"][mid]

    try:
        data = haal_op()
    except (OSError, RuntimeError, ValueError) as e:
        print("Ophalen mislukt:", e)
        if not state.get("fout"):
            stuur(bericht("FOUT", "Storingsalert kan de NS-API niet bereiken", [
                ("Tijdstip", moment.strftime("%d-%m %H:%M")), ("Foutmelding", str(e)),
                ("Gevolg", "Tot herstel worden er geen storingen gemeld.")]), alleen_alle=True)
            state["fout"] = True
            state["fout_sinds"] = moment.isoformat()
            bewaar_state(state)
        return 0

    herstel = None
    if state.get("fout"):
        herstel = bericht("HERSTELD", "Storingsalert werkt weer", [
            ("Storing sinds", tijd(state.get("fout_sinds"))), ("Hersteld", moment.strftime("%d-%m %H:%M"))])
        state["fout"] = False
        state.pop("fout_sinds", None)

    meldingen = [m for m in data if m.get("type") in TYPES]

    if os.environ.get("TEST_MODE") == "1":
        treffers = [m for m in meldingen if is_arriva(m, cfg)[0]]
        per_stroom = {}
        for m in treffers:
            for s in stromen_van(bepaal_regios(m, cfg), cfg) or ["Overig"]:
                per_stroom[s] = per_stroom.get(s, 0) + 1
        verdeling = ", ".join(f"{s}: {n}" for s, n in sorted(per_stroom.items())) or "geen"
        alle_stromen = sorted(set(cfg["stroom"].values()))
        for s in ["Alle"] + alle_stromen:
            status = "ingesteld" if webhook(s) else "NIET ingesteld"
            print(f"Webhook {s}: {status}")
        intro = ["**Testbericht**: de koppeling NS → Teams werkt voor dit kanaal.",
                 f"Actieve meldingen landelijk: {len(data)} · ongepland/calamiteit: {len(meldingen)}"
                 f" · waarvan Arriva: {len(treffers)} ({verdeling})"]
        b = bericht("TEST", "Storingsalert trein Arriva", [
            ("Kanalen", ", ".join([NAAM_ALLE] + alle_stromen))], intro, alle_stromen)
        stuur(b)
        return 0 if not REGIO_FOUTEN else 2

    gezien = set()
    for m in meldingen:
        mid = str(m.get("id"))
        inh = inhoud(m)
        rec = state["actief"].get(mid)

        if rec is None:
            ok, reden = is_arriva(m, cfg)
            if not ok:
                continue
            rec = nieuw_record(m, inh, reden, cfg, moment)
            state["actief"][mid] = rec
            gezien.add(mid)
            if mid in state["recent"]:
                vorig = state["recent"].pop(mid)
                soort = "WEER ACTIEF"
                if vorig.get("eerst_gezien"):
                    rec["eerst_gezien"] = vorig["eerst_gezien"]
                intro = [f"Deze melding verdween om {tijd(vorig.get('laatst_gezien') or vorig['afgemeld'])} "
                         f"uit InfoPlus en staat sinds {moment:%H:%M} weer actief."]
            else:
                soort, intro = "NIEUW", None
            uit.append(kaart_melding(soort, m, rec, inh, intro))
            logs.append(logregel(moment, soort, True, mid, rec, inh))
            continue

        # bestaande melding (ook records uit v4/v5)
        gezien.add(mid)
        rec["afwezig"] = 0
        rec["laatst_gezien"] = moment.isoformat()
        rec.setdefault("geplaatst", kern(rec.get("inhoud", inh)))
        rec.setdefault("voorbij", 0)
        rec.setdefault("opgelost", False)
        if "stromen" not in rec or (rec.get("type") == "CALAMITY" and "betreft" not in rec):
            rec.update(regio_velden(m, cfg))
        rec["getroffen"] = getroffen(m) or rec.get("getroffen", "")
        rec.setdefault("infoplus_start", iso_ns(m.get("start")))
        rec["infoplus_publicatie"] = iso_ns(m.get("releaseTime")) or rec.get("infoplus_publicatie", "")
        k = kern(inh)
        tekst_gewijzigd = inh != rec.get("inhoud")
        rec["inhoud"] = inh

        if k["impact"] == "voorbij":
            rec["voorbij"] += 1
            if rec["voorbij"] >= VOORBIJ_NA_KEER and not rec["opgelost"]:
                rec["opgelost"] = True
                rec["geplaatst"] = k
                uit.append(kaart_melding("OPGELOST", m, rec, inh))
                logs.append(logregel(moment, "OPGELOST", True, mid, rec, inh))
            elif tekst_gewijzigd:
                logs.append(logregel(moment, "WIJZIGING", False, mid, rec, inh))
            continue

        rec["voorbij"] = 0
        verschil = wezenlijk_verschil(rec["geplaatst"], k)
        if rec["opgelost"]:
            verschil = [("Status", "opgelost", "storing weer actief")] + verschil
            rec["opgelost"] = False
        if verschil:
            intro = ["**Gewijzigd:**"] + [f"• {veld}: was _{oud}_ → nu **{nieuw}**" for veld, oud, nieuw in verschil]
            uit.append(kaart_melding("UPDATE", m, rec, inh, intro))
            rec["geplaatst"] = k
            rec["laatst_geplaatst"] = moment.isoformat()
            logs.append(logregel(moment, "UPDATE", True, mid, rec, inh))
        elif tekst_gewijzigd:
            logs.append(logregel(moment, "WIJZIGING", False, mid, rec, inh))

    for mid in list(state["actief"]):
        if mid in gezien:
            continue
        rec = state["actief"][mid]
        rec["afwezig"] = rec.get("afwezig", 0) + 1
        if rec["afwezig"] >= AFMELD_NA_KEER.get(rec.get("type"), 3):
            # tijden: start = start in InfoPlus (of eerst gezien), einde = laatste keer gezien
            start = rec.get("infoplus_start") or rec["eerst_gezien"]
            einde = rec.get("laatst_gezien") or moment.isoformat()
            try:
                duur = int((datetime.fromisoformat(einde) - datetime.fromisoformat(start)).total_seconds() // 60)
            except ValueError:
                duur = ""
            inh = rec.get("inhoud", {})
            plaats = not rec.get("opgelost")
            if plaats:
                cal = "Calamiteit · " if rec.get("type") == "CALAMITY" else ""
                uit.append(bericht("AFGEMELD", f"{cal}{label_stromen(rec)} · {inh.get('titel', '')}", [
                    ("Laatste situatie", inh.get("situatie", "")),
                    ("Start in InfoPlus", tijd(rec.get("infoplus_start"))),
                    ("Eerst gezien", tijd(rec.get("eerst_gezien"))),
                    ("Laatst in InfoPlus", tijd(einde)),
                    ("Duur", f"{duur // 60} uur {duur % 60} min" if duur != "" else ""),
                    ("Melding-id", mid)], stromen=rec.get("stromen")))
            logs.append(logregel(moment, "AFGEMELD", plaats, mid, rec, inh, duur))
            state["recent"][mid] = {"afgemeld": moment.isoformat(), "laatst_gezien": einde,
                                    "eerst_gezien": rec.get("eerst_gezien", "")}
            del state["actief"][mid]

    if herstel:
        stuur(herstel, alleen_alle=True)
    for b in uit:
        stuur(b)
    schrijf_log(logs)
    bewaar_state(state)
    print(f"{moment:%d-%m %H:%M} | {len(data)} meldingen | {len(state['actief'])} actief Arriva | {len(uit)} kaart(en)")
    if REGIO_FOUTEN:
        print("Regiokanalen met fouten:", "; ".join(REGIO_FOUTEN))
        return 2   # status is opgeslagen; rode run zorgt voor een GitHub-mail
    return 0


if __name__ == "__main__":
    sys.exit(main())
