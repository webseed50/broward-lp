#!/usr/bin/env python3
"""Enrich LP records with BCPA property data (address, value, homestead)."""
import json, os, re, sqlite3, sys, time, urllib.request

DB = "data/broward.db"
BASE = "https://web.bcpa.net/BcpaClient/search.aspx"
HDRS = {"Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Mozilla/5.0 (public-records research)"}
MAX = int(os.environ.get("ENRICH_MAX", "250"))
HOA = re.compile(r'\b(ASSOCIATION|ASSN|CONDOMINIUM|HOA|BANK|MORTGAGE|LLC|INC|TRUST|CORP)\b')

def post(method, payload):
    req = urllib.request.Request(f"{BASE}/{method}",
        data=json.dumps(payload).encode(), headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def name_search(name):
    return post("GetData", {"value": name, "cities": "", "orderBy": "NAME",
        "pageNumber": "1", "pageCount": "10", "arrayOfValues": "",
        "selectedFromList": "false", "totalCount": "Y"})

def pick(d, *keys):
    if not isinstance(d, dict): return ""
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, "", "null"): return str(v).strip()
    return ""

def flatten(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            got = flatten(v)
            if got: return got
    return obj if isinstance(obj, list) and obj and isinstance(obj[0], dict) else None

def main():
    db = sqlite3.connect(DB)
    db.executescript("""CREATE TABLE IF NOT EXISTS prop(
        cfn TEXT PRIMARY KEY, matched_name TEXT, folio TEXT, situs TEXT,
        city TEXT, use_code TEXT, just_value TEXT, homestead TEXT,
        last_sale TEXT, match_note TEXT, fetched_at TEXT);""")
    todo = db.execute("""SELECT l.cfn,
        (SELECT p.name FROM party p WHERE p.cfn=l.cfn AND p.role='R'
         AND instr(p.name,',')>0 ORDER BY p.seq LIMIT 1)
        FROM lp l WHERE l.cfn NOT IN (SELECT cfn FROM prop)
        ORDER BY substr(l.rec_date,7)||substr(l.rec_date,1,2)||substr(l.rec_date,4,2) DESC
        LIMIT ?""", (MAX,)).fetchall()
    print(f"to enrich: {len(todo)}")
    dumped = done = 0
    os.makedirs("data/logs", exist_ok=True)
    for cfn, owner in todo:
        time.sleep(0.5)
        if not owner or HOA.search(owner.upper()):
            db.execute("INSERT OR REPLACE INTO prop VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                       (cfn, owner or "", "", "", "", "", "", "", "", "no-individual-owner"))
            continue
        try:
            resp = name_search(owner)
        except Exception as e:
            print(f"{cfn}: API FAILED {e}"); break
        if not dumped:
            json.dump(resp, open("data/logs/bcpa_sample.json", "w"), indent=1)
            dumped = 1
        rows = flatten(resp) or []
        target = owner.split(",")[0].strip().upper()
        best = None
        for r in rows:
            nm = pick(r, "ownerName1", "ownerName", "name", "owner").upper()
            if target and target in nm: best = r; break
        if best is None and rows: best = rows[0]
        if best:
            db.execute("INSERT OR REPLACE INTO prop VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (cfn, owner,
                 pick(best, "folioNumber", "folio", "parcelId"),
                 pick(best, "situsAddress1", "situsAddress", "situs", "address"),
                 pick(best, "situsCity", "city"),
                 pick(best, "useCode", "use"),
                 pick(best, "sohValue", "justValue", "just"),
                 pick(best, "homestead", "exemption"),
                 pick(best, "saleDate1", "lastSaleDate", "saleDate"),
                 "matched" if target in pick(best, "ownerName1", "ownerName", "name", "owner").upper() else "first-result"))
        else:
            db.execute("INSERT OR REPLACE INTO prop VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                       (cfn, owner, "", "", "", "", "", "", "", "no-match"))
        done += 1
    db.commit()
    n = db.execute("SELECT COUNT(*), SUM(situs!='') FROM prop").fetchone()
    print(f"enriched this run: {done}; prop table: {n[0]} rows, {n[1]} with address")
    return 0

if __name__ == "__main__":
    sys.exit(main())
