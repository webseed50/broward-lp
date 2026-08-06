#!/usr/bin/env python3
"""Phase 1: pull Broward's rolling 10-day index files, archive raw."""
import ftplib, json, sys
from datetime import datetime, timezone
from pathlib import Path

HOST, PORT, USER, PW = "BCFTP.Broward.org", 21, "crpublic", "crpublic"
RAW, LOGS = Path("data/raw"), Path("data/logs")
IMAGES = {".tif", ".tiff", ".pdf", ".jpg", ".jpeg", ".png", ".gif"}
MAX_BYTES = 75 * 1024 * 1024

def log(m):
    print(m, flush=True)

def walk(ftp, path="", depth=0):
    if depth > 3:
        return
    try:
        entries = [(n, f.get("type"), f.get("size"))
                   for n, f in ftp.mlsd(path or ".") if n not in (".", "..")]
    except Exception:
        try:
            names = ftp.nlst(path) if path else ftp.nlst()
        except Exception as e:
            log(f"cannot list {path or '/'}: {e}")
            return
        entries = [(n.rsplit("/", 1)[-1], None, None) for n in names
                   if n.rsplit("/", 1)[-1] not in (".", "..")]
    for name, kind, size in entries:
        full = f"{path}/{name}" if path else name
        if kind == "dir":
            log(f"dir  {full}/")
            yield from walk(ftp, full, depth + 1)
        elif kind == "file":
            yield full, int(size) if size else None
        else:
            here = ftp.pwd()
            try:
                ftp.cwd(full); ftp.cwd(here)
                log(f"dir  {full}/")
                yield from walk(ftp, full, depth + 1)
            except Exception:
                try:
                    size = ftp.size(full)
                except Exception:
                    size = None
                yield full, size

def main():
    RAW.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    ftp = ftplib.FTP()
    try:
        ftp.connect(HOST, PORT, timeout=90)
        ftp.login(USER, PW)
        ftp.set_pasv(True)
        log(f"connected: {ftp.getwelcome()}")
    except Exception as e:
        log(f"CONNECT FAILED: {e}")
        return 1
    seen, got, skip = [], [], []
    try:
        for remote, size in walk(ftp):
            seen.append({"path": remote, "size": size})
            if Path(remote).suffix.lower() in IMAGES:
                skip.append([remote, "image"]); continue
            if size and size > MAX_BYTES:
                skip.append([remote, f"too big {size}"]); continue
            dest = RAW / remote.lstrip("/")
            if dest.exists() and size and dest.stat().st_size == size:
                skip.append([remote, "have it"]); continue
            log(f"GET {remote} ({size})")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with dest.open("wb") as fh:
                    ftp.retrbinary(f"RETR {remote}", fh.write, 32768)
                got.append(remote)
            except Exception as e:
                log(f"FAILED {remote}: {e}")
                dest.unlink(missing_ok=True)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    manifest = {"run_utc": started.isoformat(), "seen": len(seen),
                "fetched": got, "skipped": skip, "inventory": seen}
    (LOGS / "latest-manifest.json").write_text(json.dumps(manifest, indent=2))
    (LOGS / f"manifest-{started:%Y%m%dT%H%M%SZ}.json").write_text(json.dumps(manifest, indent=2))
    log(f"done: saw {len(seen)}, fetched {len(got)}, skipped {len(skip)}")
    return 0 if seen else 1

if __name__ == "__main__":
    sys.exit(main())

