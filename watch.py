"""Watch a table (any CSV link) and report new rows by e-mail.

Every run:
  1. reads the source once,
  2. checks that the expected columns are still there,
  3. compares the row keys with the keys it has already seen,
  4. writes one e-mail with the new rows (or nothing, if there are none),
  5. appends one line to data/run-log.csv.

Without SMTP settings the e-mail is written to outbox/ as an .eml file
instead of being sent. With SMTP settings (environment variables, see
README) it is sent and a copy is kept in outbox/.
"""

import csv
import io
import json
import os
import smtplib
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USER_AGENT = "sheet-watch-sample/1.0 (portfolio project)"
RETRIES = 3
OUTBOX_KEEP = 30
RUN_LOG_FIELDS = ["run_at_utc", "status", "rows_in_source", "new_rows", "email", "note"]


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- reading

def load_config(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def fetch(url, retries=RETRIES, wait=5, opener=urllib.request.urlopen):
    """Read the source once. Network errors and server errors (5xx) are
    retried; a refusal from the source (4xx) is never retried."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener(req, timeout=30) as resp:
                return resp.read().decode("utf-8-sig")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise FetchError(f"source refused the request (HTTP {e.code}), not retried")
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(getattr(e, "reason", e))
        if attempt < retries:
            time.sleep(wait * attempt)
    raise FetchError(f"source not reachable after {retries} attempts ({last})")


def parse(text):
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    rows = [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]
    rows = [r for r in rows if any(r.values())]
    return header, rows


def missing_columns(header, required):
    return [c for c in required if c not in header]


# ------------------------------------------------------------------ state

def load_state(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def find_new(rows, key, seen):
    """Rows whose key has not been seen, in source order, each key once."""
    new, taken = [], set()
    for r in rows:
        k = r.get(key, "")
        if not k or k in seen or k in taken:
            continue
        taken.add(k)
        new.append(r)
    return new


def remember(seen, rows, key, now):
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in rows:
        k = r.get(key, "")
        if k and k not in seen:
            seen[k] = stamp
    return seen


def forget_old(seen, now, days):
    limit = now - timedelta(days=days)
    keep = {}
    for k, stamp in seen.items():
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if t >= limit:
            keep[k] = stamp
    return keep


# ------------------------------------------------------------------ e-mail

def _table(rows, columns):
    widths = [max([len(c)] + [len(r.get(c, "")) for r in rows]) for c in columns]
    def line(values):
        return "  ".join(v.ljust(w) for v, w in zip(values, widths)).rstrip()
    out = [line(columns)]
    out += [line([r.get(c, "") for c in columns]) for r in rows]
    return "\n".join(out)


def build_report(cfg, header, new_rows, now, last_run):
    src = cfg["source"]["name"]
    em = cfg["email"]
    msg = EmailMessage()
    word = "row" if len(new_rows) == 1 else "rows"
    msg["Subject"] = em["subject"].format(count=len(new_rows), rows=word, source=src)
    msg["To"] = em["to"]
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "sheet-watch@example.com"
    msg["Date"] = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    since = f"since the last check ({last_run})" if last_run else "since the last check"
    body = (
        f"{len(new_rows)} new {word} in {src} {since}.\n\n"
        f"{_table(new_rows, em['columns'])}\n\n"
        "The full rows are attached as new-rows.csv.\n"
        "Every run, with or without new rows, is listed in data/run-log.csv.\n"
    )
    msg.set_content(body, cte="8bit")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(new_rows)
    msg.add_attachment(buf.getvalue().encode("utf-8"), maintype="text", subtype="csv",
                       filename="new-rows.csv")
    return msg


def build_alert(cfg, reason, now):
    src = cfg["source"]["name"]
    msg = EmailMessage()
    msg["Subject"] = f"Check needed: {src}"
    msg["To"] = cfg["email"]["to"]
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "sheet-watch@example.com"
    msg["Date"] = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(
        f"The watch on {src} could not do its job this time.\n\n"
        f"Reason: {reason}\n\n"
        "Nothing was reported as new and the last good state was kept, so no row\n"
        "is lost or reported twice once the source is back to normal.\n"
        "This is not a quiet 'no changes' - something needs a look.\n",
        cte="8bit",
    )
    return msg


def write_outbox(msg, outbox, now, keep=OUTBOX_KEEP):
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / (now.strftime("%Y-%m-%dT%H%MZ") + ".eml")
    path.write_bytes(bytes(msg))
    files = sorted(outbox.glob("*.eml"))
    for old in files[:-keep]:
        old.unlink()
    return path


def send_if_configured(msg, env=os.environ):
    host = env.get("SMTP_HOST")
    user = env.get("SMTP_USER")
    password = env.get("SMTP_PASSWORD")
    if not (host and user and password):
        return False
    if env.get("MAIL_TO"):
        del msg["To"]
        msg["To"] = env["MAIL_TO"]
    with smtplib.SMTP(host, int(env.get("SMTP_PORT") or "587"), timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    return True


# --------------------------------------------------------------- run log

def append_run_log(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUN_LOG_FIELDS, lineterminator="\n")
        if new_file:
            w.writeheader()
        w.writerow(entry)


def read_run_log(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_summary(data, cfg, entries):
    ok = [e for e in entries if e["status"] in ("baseline", "ok")]
    failed = [e for e in entries if e["status"] not in ("baseline", "ok")]
    reported = sum(int(e["new_rows"] or 0) for e in entries)
    emails = [e for e in entries if e["email"] in ("written", "sent")]
    facts = {
        "source": cfg["source"]["name"],
        "first_run_utc": entries[0]["run_at_utc"] if entries else None,
        "last_run_utc": entries[-1]["run_at_utc"] if entries else None,
        "runs": len(entries),
        "runs_ok": len(ok),
        "runs_needing_a_check": len(failed),
        "new_rows_reported": reported,
        "emails": len(emails),
    }
    (data / "facts.json").write_text(json.dumps(facts, indent=1) + "\n", encoding="utf-8")
    last = entries[-1] if entries else {}
    lines = [
        f"LAST RUN - {cfg['source']['name']}",
        "",
        f"run at (UTC)     {last.get('run_at_utc', '-')}",
        f"status           {last.get('status', '-')}",
        f"rows in source   {last.get('rows_in_source', '-')}",
        f"new rows         {last.get('new_rows', '-')}",
        f"e-mail           {last.get('email', '-')}",
        f"note             {last.get('note', '') or '-'}",
        "",
        "SINCE THE FIRST RUN",
        f"runs             {facts['runs']} ({facts['runs_ok']} ok, {facts['runs_needing_a_check']} needing a check)",
        f"new rows         {facts['new_rows_reported']} reported, {facts['emails']} e-mail(s) written or sent",
        f"first run (UTC)  {facts['first_run_utc']}",
    ]
    (data / "last-run.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# -------------------------------------------------------------------- run

def run(cfg, data, outbox, now=None, fetcher=fetch):
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    state_path = data / "seen.json"
    log_path = data / "run-log.csv"
    entries = read_run_log(log_path)
    last_run = entries[-1]["run_at_utc"] if entries else None
    state = load_state(state_path)
    entry = {"run_at_utc": stamp, "status": "", "rows_in_source": "", "new_rows": 0,
             "email": "none", "note": ""}

    def alert(status, reason):
        msg = build_alert(cfg, reason, now)
        sent = send_if_configured(msg)
        write_outbox(msg, outbox, now)
        entry.update(status=status, email="sent" if sent else "written", note=reason)

    try:
        text = fetcher(cfg["source"]["url"])
    except FetchError as e:
        alert("fetch failed", str(e))
    else:
        header, rows = parse(text)
        entry["rows_in_source"] = len(rows)
        missing = missing_columns(header, cfg["source"]["required_columns"])
        key = cfg["source"]["key"]
        if missing:
            alert("source changed", "columns missing: " + ", ".join(missing))
        elif not rows and state:
            alert("source empty", "the source returned no rows at all")
        elif state is None:
            seen = remember({}, rows, key, now)
            save_state(state_path, {"seen": seen})
            entry.update(status="baseline",
                         note=f"first run: {len(rows)} existing {'row' if len(rows) == 1 else 'rows'} recorded, nothing sent")
        else:
            seen = state.get("seen", {})
            new_rows = find_new(rows, key, seen)
            if new_rows:
                msg = build_report(cfg, header, new_rows, now, last_run)
                sent = send_if_configured(msg)
                path = write_outbox(msg, outbox, now)
                entry["email"] = "sent" if sent else "written"
                entry["note"] = f"outbox/{path.name}"
            seen = remember(seen, rows, key, now)
            seen = forget_old(seen, now, cfg["state"]["remember_days"])
            save_state(state_path, {"seen": seen})
            entry.update(status="ok", new_rows=len(new_rows))

    append_run_log(log_path, entry)
    write_summary(data, cfg, read_run_log(log_path))
    return entry


def main():
    cfg = load_config(ROOT / "config.toml")
    entry = run(cfg, ROOT / "data", ROOT / "outbox")
    print(f"{entry['run_at_utc']}: {entry['status']}, {entry['rows_in_source']} rows in source, "
          f"{entry['new_rows']} new, e-mail {entry['email']}. {entry['note']}")
    return 0 if entry["status"] in ("baseline", "ok") else 1


if __name__ == "__main__":
    sys.exit(main())
