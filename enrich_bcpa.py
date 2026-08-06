#!/usr/bin/env python3
"""Enrich LP records with BCPA property data (address + folio via owner search)."""
import json, os, re, sqlite3, sys, time, urllib.request

DB = "data/broward.db"
BASE = "https://web.bcpa.net/BcpaClient/search.aspx"
HDRS = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "Mozilla/5.0"}
MAX = int(os.environ.get("ENRICH_MAX", "250"))
ENTITY = re.compile(r'\b(ASSOCIATION|ASSN|CONDOMINIUM|HOA|BANK|MORTGAGE|LLC|INC|TRUST|CORP)\b')

def name_search(name):
    payload = {"value": name, "cities": "", "orderBy": "NAME",
               "pageNumber": "1", "pageCount": "10", "arrayOfValues": "",
               "selectedFromList": "false", "totalCount": "Y"}
    req = urllib.request.Request(f"{BASE}/GetData",
        data=json.dumps(payload).encode(), headers=HDRS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["d"]

def main():
    db = sqlite3.connect(DB)
    db.executescript("""CREATE TABLE IF NOT EXISTS prop(
        cfn TEXT PRIMARY KEY, matched_name TEXT, folio TEXT, situs TEXT,
        city TEXT, use_code TEXT, just_value TEXT, homestead TEXT,
        last_sale TEXT, match_note TEXT, fetched_at TEXT);""")
    # retry rows from the broken first batch (no folio, no address)
    db.execute("DELETE FROM prop WHERE folio='' AND situs=''")
    db.commit()
    todo = db.execute("""SELECT l.cfn,
        (SELECT p.name FROM party p WHERE p.cfn=l.cfn AND p.role='R'
         AND instr(p.name,',')>0 ORDER BY p.seq LIMIT 1)
        FROM lp l WHERE l.cfn NOT IN (SELECT cfn FROM prop)
        ORDER BY substr(l.rec_date,7)||substr(l.rec_date,1,2)||substr(l.rec_date,4,2) DESC
        LIMIT ?""", (MAX,)).fetchall()
    print(f"to enrich: {len(todo)}")
    hits = 0
    for cfn, owner in todo:
        if not owner or ENTITY.search(owner.upper()):
            db.execute("INSERT OR REPLACE INTO prop VALUES(?,?,'','','','','','','','no-individual-owner',datetime('now'))",
                       (cfn, owner or ""))
            continue
        time.sleep(0.5)
        try:
            d = name_search(owner)
        except Exception as e:
            print(f"{cfn}: API FAILED {e}"); break
        rows = d.get("resultListk__BackingField") or []
        target = owner.upper().replace(" ", "")
        best = None
        for r in rows:
            nm = (r.get("ownerName1", "") + r.get("ownerName2", "")).upper().replace(" ", "")
            if target[:20] in nm: best = r; break
        note = "matched"
        if best is None and rows:
            best, note = rows[0], "first-result"
        if best:
            city = (best.get("siteAddress2") or "").split(",")[0].strip()
            db.execute("INSERT OR REPLACE INTO prop VALUES(?,?,?,?,?,'','','','',?,datetime('now'))",
                (cfn, owner, best.get("folioNumber", ""),
                 (best.get("siteAddress1") or "").strip(), city, note))
            hits += 1
        else:
            db.execute("INSERT OR REPLACE INTO prop VALUES(?,?,'','','','','','','','no-match',datetime('now'))",
                       (cfn, owner))
    db.commit()
    n = db.execute("SELECT COUNT(*), SUM(situs!='') FROM prop").fetchone()
    print(f"hits this run: {hits}; prop table: {n[0]} rows, {n[1]} with address")
    # merge into the lead CSV
    import csv
    src = list(csv.reader(open("data/lis_pendens.csv")))
    hdr, body = src[0], src[1:]
    if "property_address" not in hdr:
        hdr += ["property_address", "property_city", "bcpa_folio", "match_note"]
    pm = {r[0]: r[1:] for r in db.execute("SELECT cfn,situs,city,folio,match_note FROM prop")}
    out = []
    for row in body:
        cfn = row[1]
        row = row[:11] + list(pm.get(cfn, ("", "", "", "")))
        out.append(row)
    with open("data/lis_pendens.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(hdr); w.writerows(out)
    print("csv updated with property columns")
    return 0

if __name__ == "__main__":
    sys.exit(main())

