#!/usr/bin/env python3
"""Broward LP pipeline: SFTP daily window + optional yearly backfill -> SQLite -> CSV."""
import csv, os, sqlite3, sys
import paramiko

HOST, PORT, USER, PW = "BCFTP.Broward.org", 22, "crpublic", "crpublic"
DAILY_DIR = "Official_Records_Download"
YEARLY_DIR = "OR_Yearly_Exports"
DB, OUT = "data/broward.db", "data/lis_pendens.csv"

def connect():
    tr = paramiko.Transport((HOST, PORT))
    tr.connect(username=USER, password=PW)
    return tr, paramiko.SFTPClient.from_transport(tr)

def parse_doc(line):
    f = line.rstrip("\r\n").split("|")
    if len(f) < 19 or f[4] != "LP":
        return None
    return dict(cfn=f[0], rec_date=f[2], rec_time=f[3], doc_type=f[4],
                consideration=f[5], legal=f[9], folio=f[10], pages=f[13],
                case_no=f[18].strip())

def case_bucket(case_no):
    p = case_no[:4].upper()
    if p.startswith("CACE"): return "circuit-civil"
    if p.startswith("DVCE") or p.startswith("FMCE"): return "family-divorce"
    if p.startswith("PRC") or p.startswith("PR-C"): return "probate"
    if p.startswith("CO"): return "county-civil"
    return "other" if case_no else "none"

def ensure_db():
    os.makedirs("data", exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS lp(
      cfn TEXT PRIMARY KEY, rec_date TEXT, rec_time TEXT, case_no TEXT,
      case_bucket TEXT, legal TEXT, folio TEXT, pages TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS party(
      cfn TEXT, name TEXT, role TEXT, seq TEXT,
      PRIMARY KEY(cfn,name,role,seq));
    CREATE INDEX IF NOT EXISTS lp_date ON lp(rec_date);
    """)
    return db

def ingest(db, sftp, folder, doc_file, nme_file, source):
    lps = {}
    with sftp.open(f"{folder}/{doc_file}") as fh:
        for line in fh.read().decode("utf-8", "replace").splitlines():
            r = parse_doc(line)
            if r: lps[r["cfn"]] = r
    if not lps:
        print(f"{doc_file}: no LP rows"); return 0
    for r in lps.values():
        db.execute("""INSERT INTO lp VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(cfn) DO UPDATE SET rec_date=excluded.rec_date,
          case_no=excluded.case_no, case_bucket=excluded.case_bucket""",
          (r["cfn"], r["rec_date"], r["rec_time"], r["case_no"],
           case_bucket(r["case_no"]), r["legal"], r["folio"], r["pages"], source))
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
    total = 0
    names = sftp.listdir(DAILY_DIR)
    days = sorted(n[:10] for n in names if n.endswith("doc-ver.txt"))
    for d in days:
        try:
            total += ingest(db, sftp, DAILY_DIR, f"{d}doc-ver.txt",
                            f"{d}nme-ver.txt", f"daily:{d}")
        except Exception as e:
            print(f"{d}: FAILED {e}")
    for cy in [c for c in os.environ.get("BACKFILL_YEARS", "").split(",") if c.strip()]:
        cy = cy.strip()
        try:
            total += ingest(db, sftp, YEARLY_DIR, f"{cy}doc-rec.txt",
                            f"{cy}nme-rec.txt", f"yearly:{cy}")
        except Exception as e:
            print(f"{cy}: FAILED {e}")
    tr.close()
    db.commit()
    rows = db.execute("""SELECT l.rec_date,l.cfn,l.case_no,l.case_bucket,
        group_concat(CASE WHEN p.role='D' THEN p.name END, '; '),
        group_concat(CASE WHEN p.role='R' THEN p.name END, '; '),
        l.legal,l.folio FROM lp l LEFT JOIN party p ON p.cfn=l.cfn
        GROUP BY l.cfn ORDER BY l.rec_date DESC, l.cfn DESC""").fetchall()
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rec_date","cfn","case_no","case_bucket",
                    "plaintiffs_D","defendants_R","legal","folio"])
        w.writerows(rows)
    print(f"db total: {db.execute('SELECT COUNT(*) FROM lp').fetchone()[0]} LP records; csv rows: {len(rows)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
