#!/usr/bin/env python3
"""Broward LP pipeline: SFTP -> SQLite -> enriched CSV (auto-classified leads)."""
import csv, os, re, sqlite3, sys
import paramiko

HOST, PORT, USER, PW = "BCFTP.Broward.org", 22, "crpublic", "crpublic"
DAILY_DIR, YEARLY_DIR = "Official_Records_Download", "OR_Yearly_Exports"
DB, OUT = "data/broward.db", "data/lis_pendens.csv"

BANK = re.compile(r'\b(BANK|TRUST|MORTGAGE|LENDING|LOAN|FINANC|CREDIT UNION|FUND|CAPITAL|SAVINGS|FEDERAL|NA\b|N\.A\.|FSB|WILMINGTON|DEUTSCHE|NATIONSTAR|FREEDOM|PENNYMAC|LAKEVIEW|ROCKET|CARRINGTON|SHELLPOINT|MIDFIRST)\b')
HOA = re.compile(r'\b(ASSOCIATION|ASSN|CONDOMINIUM|CONDO|HOMEOWNERS|HOA|MASTER|MAINTENANCE)\b')
CONSTR = re.compile(r'\b(CONSTRUCTION|ROOFING|SOLAR|RENOVATION|CONTRACTOR|PLUMBING|BUILDERS|RESTORATION|LIEN LIQUIDATORS|PAVING|ELECTRIC)\b')
GOV = re.compile(r'\b(UNITED STATES|STATE OF|COUNTY|CITY OF|DEPARTMENT|HUD|IRS)\b')

def ptype(s):
    s = (s or "").upper()
    if not s: return "unknown"
    if HOA.search(s): return "hoa-condo"
    if BANK.search(s): return "bank-lender"
    if CONSTR.search(s): return "contractor-lien"
    if GOV.search(s): return "government"
    return "individual-other"

def lead(bucket, pt):
    if bucket == "family-divorce": return "divorce"
    return {"bank-lender": "mortgage-foreclosure", "hoa-condo": "hoa-lien-foreclosure",
            "contractor-lien": "construction-lien", "government": "gov-action"}.get(pt, "other-civil")

def case_bucket(c):
    p = c[:4].upper()
    if p.startswith("CACE"): return "circuit-civil"
    if p.startswith("DVCE") or p.startswith("FMCE"): return "family-divorce"
    if p.startswith("PRC"): return "probate"
    if p.startswith("CO"): return "county-civil"
    return "other" if c else "none"

def connect():
    tr = paramiko.Transport((HOST, PORT))
    tr.connect(username=USER, password=PW)
    return tr, paramiko.SFTPClient.from_transport(tr)

def parse_doc(line):
    f = line.rstrip("\r\n").split("|")
    if len(f) < 19 or f[4] != "LP": return None
    return (f[0], f[2], f[3], f[18].strip(), case_bucket(f[18].strip()),
            f[9], f[10], f[13])

def ensure_db():
    os.makedirs("data", exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS lp(cfn TEXT PRIMARY KEY, rec_date TEXT,
      rec_time TEXT, case_no TEXT, case_bucket TEXT, legal TEXT, folio TEXT,
      pages TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS party(cfn TEXT, name TEXT, role TEXT, seq TEXT,
      PRIMARY KEY(cfn,name,role,seq));
    CREATE INDEX IF NOT EXISTS lp_date ON lp(rec_date);""")
    return db

def ingest(db, sftp, folder, doc_file, nme_file, source):
    lps = {}
    with sftp.open(f"{folder}/{doc_file}") as fh:
        for line in fh.read().decode("utf-8", "replace").splitlines():
            r = parse_doc(line)
            if r: lps[r[0]] = r
    if not lps:
        print(f"{doc_file}: no LP rows"); return 0
    for r in lps.values():
        db.execute("""INSERT INTO lp VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(cfn) DO UPDATE SET case_no=excluded.case_no""",
          (*r, source))
    n = 0
    with sftp.open(f"{folder}/{nme_file}") as fh:
        for line in fh.read().decode("utf-8", "replace").splitlines():
            f = line.rstrip("\r\n").split("|")
            if len(f) >= 4 and f[0] in lps:
                db.execute("INSERT OR IGNORE INTO party VALUES(?,?,?,?)",
                           (f[0], f[1], f[2], f[3])); n += 1
    print(f"{doc_file}: {len(lps)} LP docs, {n} party rows")
    return len(lps)

def main():
    tr, sftp = connect()
    db = ensure_db()
    for d in sorted(n[:10] for n in sftp.listdir(DAILY_DIR) if n.endswith("doc-ver.txt")):
        try: ingest(db, sftp, DAILY_DIR, f"{d}doc-ver.txt", f"{d}nme-ver.txt", f"daily:{d}")
        except Exception as e: print(f"{d}: FAILED {e}")
    for cy in [c.strip() for c in os.environ.get("BACKFILL_YEARS", "").split(",") if c.strip()]:
        try: ingest(db, sftp, YEARLY_DIR, f"{cy}doc-rec.txt", f"{cy}nme-rec.txt", f"yearly:{cy}")
        except Exception as e: print(f"{cy}: FAILED {e}")
    tr.close()
    db.commit()
    rows = db.execute("""SELECT l.rec_date,l.cfn,l.case_no,l.case_bucket,
        group_concat(CASE WHEN p.role='D' THEN p.name END,'; '),
        group_concat(CASE WHEN p.role='R' THEN p.name END,'; '),
        l.legal,l.folio FROM lp l LEFT JOIN party p ON p.cfn=l.cfn
        GROUP BY l.cfn
        ORDER BY substr(l.rec_date,7)||substr(l.rec_date,1,2)||substr(l.rec_date,4,2) DESC""").fetchall()
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rec_date","cfn","case_no","lead_type","plaintiff_type",
                    "likely_owners","plaintiffs","all_defendants","stacked_liens",
                    "legal","folio"])
        for d,cfn,case,bucket,pl,de,legal,folio in rows:
            pt = ptype(pl)
            owners = "; ".join(n.strip() for n in (de or "").split(";")
                               if "," in n and not HOA.search(n.upper()))
            stacked = "yes" if owners and re.search(r'ASSOCIATION|BANK|MORTGAGE', (de or "").upper()) else ""
            w.writerow([d, cfn, case, lead(bucket, pt), pt, owners, pl, de,
                        stacked, legal, folio])
    print(f"db total: {db.execute('SELECT COUNT(*) FROM lp').fetchone()[0]} LP records; csv rows: {len(rows)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
