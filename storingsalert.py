#!/usr/bin/env python3
"""
Storingsalert trein - Regio Noord (MVP)

Haalt elke run de actieve verstoringen op uit de NS Disruptions API (InfoPlus),
filtert op Arriva-trajecten in Noord en plaatst via een Teams-werkstroom (webhook)
een bericht in het Teams-kanaal bij:
  [NIEUW]     een nieuwe melding
  [UPDATE]    een inhoudelijke wijziging
  [AFGEMELD]  een melding die verdwenen is
  [FOUT]      de NS-API is niet bereikbaar (en [HERSTELD] als het weer werkt)

Alles wordt vastgelegd in data/log.csv (basis voor latere vergelijking met ASL).

Instellingen via omgevingsvariabelen (in GitHub als 'secrets'):
  NS_API_KEY          sleutel van het NS API-portaal
  TEAMS_WEBHOOK_URL   URL van de Teams-werkstroom "Stuur webhookwaarschuwingen naar een kanaal"
Optioneel:
  TEST_MODE=1    stuur een [TEST]-bericht, verander niets aan de status
  DRY_RUN=1      niets versturen, alleen tonen (lokaal testen)
  FIXTURE=pad    lees een JSON-bestand in plaats van de API (lokaal testen)
"""

import csv
import gzip
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VERSIE = "2026-11-09 v4"
API_URL = os.environ.get("NS_API_URL", "https://gateway.apiportal.ns.nl/disruptions/v3?isActive=true")
TYPES = {"DISRUPTION", "CALAMITY"}
TZ = ZoneInfo("Europe/Amsterdam")
AFMELD_NA_KEER = 2  # pas afmelden als een melding 2 controles op rij weg is

BASE = Path(__file__).resolve().parent
STATIONS_FILE = BASE / "stations_noord.txt"
STATE_FILE = BASE / "data" / "state.json"
LOG_FILE = BASE / "data" / "log.csv"

LOG_KOLOMMEN = [
    "tijdstip", "gebeurtenis", "id", "type", "titel", "reden",
    "situatie", "verwachting", "vervangend_vervoer", "advies", "fase",
    "eerst_gezien", "duur_minuten",
]

VELD_NAMEN = {
    "titel": "Traject",
    "fase": "Fase",
    "situatie": "Situatie",
    "omschrijving": "Omschrijving",
    "verwachting": "Verwachte duur",
    "vervangend_vervoer": "Vervangend vervoer",
    "advies": "Advies",
    "einde": "Verwacht einde",
}


# ---------- hulpfuncties ----------

def nu():
    return datetime.now(TZ)


def norm(tekst):
    """Kleine letters, zonder accenten en dubbele spaties (Feanwâlden = feanwalden)."""
    t = unicodedata.normalize("NFKD", tekst or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


def tijd(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d-%m %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def laad_stations():
    """Leest regels 'CODE  Naam' onder [stations]. Geeft (codes, namen) terug, genormaliseerd."""
    codes, namen, sectie = set(), set(), None
    for regel in STATIONS_FILE.read_text(encoding="utf-8").splitlines():
        r = regel.split("#", 1)[0].strip()
        if not r:
            continue
        if r.lower() == "[stations]":
            sectie = "stations"
            continue
        if r.startswith("["):
            sectie = "anders"  # bijv. [knooppunten]: alleen ter documentatie
            continue
        if sectie == "stations":
            delen = r.split(None, 1)
            codes.add(norm(delen[0]))
            if len(delen) > 1:
                namen.add(norm(delen[1]))
    return codes, namen


def zoek(obj, sleutel):
    """Zoek recursief alle waarden van een sleutel (werkt met lijst-in-lijst)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == sleutel:
                yield v
            yield from zoek(v, sleutel)
    elif isinstance(obj, list):
        for item in obj:
            yield from zoek(item, sleutel)


def stations_van(melding):
    """Alle stations in de melding als lijst van (code, naam)."""
    gevonden = {}
    for st in zoek(melding.get("publicationSections", []), "stations"):
        lijst = st if isinstance(st, list) else [st]
        stapel = list(lijst)
        while stapel:
            item = stapel.pop()
            if isinstance(item, list):
                stapel.extend(item)
            elif isinstance(item, dict) and item.get("name"):
                gevonden[item.get("name")] = item.get("stationCode", "")
    return [(c, n) for n, c in gevonden.items()]


def stationsnamen(melding):
    return {n for _, n in stations_van(melding)}


def vervoerders(melding):
    ops = set()
    for lijst in zoek(melding.get("publicationSections", []), "operators"):
        if isinstance(lijst, list):
            ops.update(str(o) for o in lijst)
    return ops


def is_arriva_noord(melding, stations):
    if melding.get("type") == "CALAMITY":
        return True, "calamiteit (wordt altijd gemeld)"
    ops = vervoerders(melding)
    if any("arriva" in o.lower() for o in ops):
        return True, "label Arriva"
    codes, namen = stations
    geraakt = sorted(n for c, n in stations_van(melding)
                     if norm(c) in codes or norm(n) in namen)
    if geraakt:
        return True, "station(s): " + ", ".join(geraakt)
    return False, ""


def inhoud(m):
    """De velden waarop we wijzigingen detecteren."""
    ts = m.get("timespans") or []
    situatie = [(t.get("situation") or {}).get("label", "") for t in ts]
    alt = [(t.get("alternativeTransport") or {}).get("label", "") for t in ts]
    advies = [a for t in ts for a in (t.get("advices") or [])]
    return {
        "titel": m.get("title", ""),
        "fase": (m.get("phase") or {}).get("label", ""),
        "situatie": " ".join(s for s in situatie if s),
        "omschrijving": m.get("description", ""),  # bij calamiteiten
        "verwachting": (m.get("expectedDuration") or {}).get("description", ""),
        "vervangend_vervoer": "; ".join(a for a in alt if a),
        "advies": " ".join(advies),
        "einde": m.get("end", ""),
    }


# ---------- opslag ----------

def laad_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"actief": {}, "fout": False}


def bewaar_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 'maand' verandert 1x per maand: houdt de repository actief
    # (GitHub zet geplande taken uit na lange inactiviteit)
    state["maand"] = nu().strftime("%Y-%m")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")


def log(regels):
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


# ---------- bron ----------

def haal_op():
    fixture = os.environ.get("FIXTURE")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))
    key = os.environ.get("NS_API_KEY", "")
    if not key:
        raise RuntimeError("NS_API_KEY ontbreekt")
    req = urllib.request.Request(API_URL, headers={
        "Ocp-Apim-Subscription-Key": key,
        "Cache-Control": "no-cache",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        ruw = r.read()
    while ruw[:2] == b"\x1f\x8b":  # gecomprimeerd antwoord (gzip) uitpakken, ook dubbel
        ruw = gzip.decompress(ruw)
    data = json.loads(ruw.decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Onverwacht antwoord van de NS-API (geen lijst)")
    return data


# ---------- berichten (Teams Adaptive Card) ----------

KLEUR = {"NIEUW": "Attention", "UPDATE": "Warning", "AFGEMELD": "Good",
         "FOUT": "Attention", "HERSTELD": "Good", "TEST": "Accent"}


def bericht(soort, titel, feiten, intro=None):
    """Een bericht = dict met soort, titel, optionele introregels en feiten (naam, waarde)."""
    return {"soort": soort, "titel": titel, "intro": intro or [],
            "feiten": [(k, v) for k, v in feiten if v]}


def bericht_melding(soort, m, rec, inh, intro=None):
    titel = inh["titel"].rstrip(".") or "Onbekend traject"
    oorzaak = ""
    for t in m.get("timespans") or []:
        oorzaak = (t.get("cause") or {}).get("label", "") or oorzaak
    return bericht(soort, titel + (f": {oorzaak}" if oorzaak else ""), [
        ("Situatie", inh["situatie"] or inh["omschrijving"]),
        ("Verwachte duur", inh["verwachting"]),
        ("Vervangend vervoer", inh["vervangend_vervoer"]),
        ("Advies", inh["advies"]),
        ("Fase", inh["fase"]),
        ("Type", m.get("type", "")),
        ("Vervoerder (InfoPlus)", ", ".join(sorted(vervoerders(m)))),
        ("Waarom Arriva Noord", rec.get("reden", "")),
        ("Stations", ", ".join(sorted(stationsnamen(m)))),
        ("Eerst gezien", tijd(rec.get("eerst_gezien"))),
        ("Melding-id", str(m.get("id", ""))),
    ], intro)


def kaart(b):
    body = [{"type": "TextBlock", "text": f"[{b['soort']}] {b['titel']}", "weight": "Bolder",
             "size": "Medium", "color": KLEUR.get(b["soort"], "Default"), "wrap": True}]
    for regel in b["intro"]:
        body.append({"type": "TextBlock", "text": regel, "wrap": True, "spacing": "Small"})
    if b["feiten"]:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in b["feiten"]]})
    body.append({"type": "TextBlock", "text": f"Storingsalert trein Noord · versie {VERSIE} · bron: InfoPlus via NS API",
                 "isSubtle": True, "size": "Small", "wrap": True})
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body,
                    "msteams": {"width": "Full"}},
    }]}


def stuur(b):
    payload = kaart(b)
    if os.environ.get("DRY_RUN") == "1":
        print("=" * 70)
        print(f"[{b['soort']}] {b['titel']}")
        for r in b["intro"]:
            print("  ", r)
        for k, v in b["feiten"]:
            print(f"   {k}: {v}")
        return
    url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not url:
        raise RuntimeError("TEAMS_WEBHOOK_URL ontbreekt")
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if not 200 <= r.status < 300:
                raise RuntimeError(f"Teams antwoordde met status {r.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Teams weigerde het bericht (status {e.code}). "
                           "Bij 401/403: werkstroom accepteert geen externe afzenders.") from e


# ---------- hoofdlogica ----------

def main():
    moment = nu()
    stations = laad_stations()
    state = laad_state()
    state.setdefault("actief", {})
    uit = []    # te versturen berichten
    logs = []

    # 1. Ophalen, met foutafhandeling (1 foutbericht, niet elke 5 minuten)
    try:
        data = haal_op()
    except (OSError, RuntimeError, ValueError) as e:
        print("Ophalen mislukt:", e)
        if not state.get("fout"):
            stuur(bericht("FOUT", "Storingsalert kan de NS-API niet bereiken", [
                ("Tijdstip", moment.strftime("%d-%m %H:%M")),
                ("Foutmelding", str(e)),
                ("Gevolg", "Tot herstel worden er geen storingen gemeld."),
            ]))
            state["fout"] = True
            state["fout_sinds"] = moment.isoformat()
            bewaar_state(state)
        return 0

    if state.get("fout"):
        uit.append(bericht("HERSTELD", "Storingsalert werkt weer", [
            ("Storing sinds", tijd(state.get("fout_sinds"))),
            ("Hersteld", moment.strftime("%d-%m %H:%M")),
        ]))
        state["fout"] = False
        state.pop("fout_sinds", None)

    meldingen = [m for m in data if m.get("type") in TYPES]

    # 2. Testmodus: stuur 1 voorbeeld, wijzig niets
    if os.environ.get("TEST_MODE") == "1":
        treffers = [m for m in meldingen if is_arriva_noord(m, stations)[0]]
        intro = ["**Testbericht**: de koppeling NS → Teams werkt.",
                 f"Actieve meldingen landelijk: {len(data)} · ongepland/calamiteit: "
                 f"{len(meldingen)} · waarvan Arriva Noord: {len(treffers)}"]
        voorbeeld = (treffers or meldingen or data or [None])[0]
        if voorbeeld:
            ok, reden = is_arriva_noord(voorbeeld, stations)
            rec = {"reden": reden or "geen (voorbeeld buiten Noord)",
                   "eerst_gezien": moment.isoformat()}
            stuur(bericht_melding("TEST", voorbeeld, rec, inhoud(voorbeeld),
                                  intro + ["_Voorbeeld van een melding:_"]))
        else:
            stuur(bericht("TEST", "Storingsalert trein Noord", [], intro))
        return 0

    # 3. Nieuw en gewijzigd
    gezien = set()
    for m in meldingen:
        mid = str(m.get("id"))
        inh = inhoud(m)
        rec = state["actief"].get(mid)
        if rec:
            gezien.add(mid)
            rec["afwezig"] = 0
            if inh != rec["inhoud"]:
                gewijzigd = [k for k in inh if inh[k] != rec["inhoud"].get(k)]
                intro = ["**Gewijzigd:**"] + [
                    f"• {VELD_NAMEN.get(k, k)}: was _{rec['inhoud'].get(k, '') or '(leeg)'}_ "
                    f"→ nu **{inh[k] or '(leeg)'}**" for k in gewijzigd]
                uit.append(bericht_melding("UPDATE", m, rec, inh, intro))
                rec["inhoud"] = inh
                rec["laatst_gewijzigd"] = moment.isoformat()
                logs.append({"gebeurtenis": "UPDATE", "m": m, "rec": rec})
        else:
            ok, reden = is_arriva_noord(m, stations)
            if ok:
                rec = {"inhoud": inh, "reden": reden, "type": m.get("type", ""),
                       "eerst_gezien": moment.isoformat(),
                       "laatst_gewijzigd": moment.isoformat(), "afwezig": 0}
                state["actief"][mid] = rec
                gezien.add(mid)
                uit.append(bericht_melding("NIEUW", m, rec, inh))
                logs.append({"gebeurtenis": "NIEUW", "m": m, "rec": rec})

    # 4. Afgemeld (pas na AFMELD_NA_KEER controles afwezig)
    for mid in list(state["actief"]):
        if mid in gezien:
            continue
        rec = state["actief"][mid]
        rec["afwezig"] = rec.get("afwezig", 0) + 1
        if rec["afwezig"] >= AFMELD_NA_KEER:
            eerst = datetime.fromisoformat(rec["eerst_gezien"])
            duur = int((moment - eerst).total_seconds() // 60)
            inh = rec["inhoud"]
            uit.append(bericht("AFGEMELD", inh.get("titel", "").rstrip("."), [
                ("Laatste situatie", inh.get("situatie") or inh.get("omschrijving", "")),
                ("Eerst gezien", tijd(rec["eerst_gezien"])),
                ("Afgemeld (gezien)", moment.strftime("%d-%m %H:%M")),
                ("Duur volgens monitor", f"{duur // 60} uur {duur % 60} min"),
                ("Melding-id", mid),
            ]))
            logs.append({"gebeurtenis": "AFGEMELD", "m": {"id": mid, "type": rec.get("type")},
                         "rec": rec, "duur": duur})
            del state["actief"][mid]

    # 5. Versturen; pas daarna status en logboek opslaan
    #    (mislukt versturen = volgende run opnieuw proberen)
    for b in uit:
        stuur(b)

    log([{
        "tijdstip": moment.isoformat(timespec="seconds"),
        "gebeurtenis": r["gebeurtenis"],
        "id": str(r["m"].get("id", "")),
        "type": r["m"].get("type", "") or r["rec"].get("type", ""),
        "titel": r["rec"]["inhoud"].get("titel", ""),
        "reden": r["rec"].get("reden", ""),
        "situatie": r["rec"]["inhoud"].get("situatie", "") or r["rec"]["inhoud"].get("omschrijving", ""),
        "verwachting": r["rec"]["inhoud"].get("verwachting", ""),
        "vervangend_vervoer": r["rec"]["inhoud"].get("vervangend_vervoer", ""),
        "advies": r["rec"]["inhoud"].get("advies", ""),
        "fase": r["rec"]["inhoud"].get("fase", ""),
        "eerst_gezien": r["rec"].get("eerst_gezien", ""),
        "duur_minuten": r.get("duur", ""),
    } for r in logs])
    bewaar_state(state)
    print(f"{moment:%d-%m %H:%M} | {len(data)} meldingen | {len(state['actief'])} actief Arriva Noord | {len(uit)} bericht(en)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
