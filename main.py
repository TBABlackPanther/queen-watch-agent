import os
import re
import time
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

PLAYLIST_URL = os.getenv("PLAYLIST_URL", "https://www.oldie-antenne.de/programm/song-suche?channel=live")
POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "15")))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_API_VERSION = os.getenv("META_API_VERSION", "v23.0")
RECIPIENT = os.getenv("WHATSAPP_RECIPIENT", "")
DB_PATH = os.getenv("DB_PATH", "/var/data/queen_watch.sqlite3")
MAX_SONG_AGE_MINUTES = max(1, int(os.getenv("MAX_SONG_AGE_MINUTES", "6")))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("queen-watch")

session = requests.Session()
session.headers.update({
    "User-Agent": "QueenWatch/1.1 (playlist monitoring)",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
})
BERLIN = ZoneInfo("Europe/Berlin")


def db_init():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS sent (
            fingerprint TEXT PRIMARY KEY, title TEXT NOT NULL, played_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL, status TEXT NOT NULL
        )""")


def fetch_recent_rows():
    response = session.get(PLAYLIST_URL, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    lines = [re.sub(r"\s+", " ", x).strip() for x in soup.get_text("\n", strip=True).splitlines()]
    lines = [x for x in lines if x]
    if len(lines) < 10:
        raise RuntimeError("Die Song-Suche enthält zu wenig lesbaren Text; möglicherweise hat sich die Seite geändert.")

    # The public page lists recent entries as: played time, title, artist.
    rows = []
    for i, line in enumerate(lines):
        match = re.search(r"Gespielte Uhrzeit\s+(\d{1,2}:\d{2})", line, re.I)
        if not match:
            continue
        time_text = match.group(1)
        title = lines[i + 1] if i + 1 < len(lines) else ""
        artist = lines[i + 2] if i + 2 < len(lines) else ""
        if not title or not artist:
            continue
        title = clean_title(title)
        artist = re.sub(r"\s+", " ", artist).strip()
        if plausible_title(title):
            rows.append({"time": time_text, "title": title, "artist": artist})
    if not rows:
        raise RuntimeError("Keine Song-Einträge mit 'Gespielte Uhrzeit' erkannt; Parser muss geprüft werden.")
    return rows


def clean_title(value):
    value = re.sub(r"\s+", " ", value).strip(" -–—:|·")
    # Artwork captions often append the release year; keep only the song name.
    value = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", value)
    return value[:180].strip()


def plausible_title(value):
    return 1 < len(value) <= 180 and not re.fullmatch(
        r"(Queen|Song-Suche|Live|Startseite|OLDIE ANTENNE|Gespielte Uhrzeit)", value, re.I
    )


def played_datetime(time_text, now=None):
    now = now or datetime.now(BERLIN)
    hour, minute = map(int, time_text.split(":"))
    played = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # A listed time slightly in the future is likely from the previous day around midnight.
    if played > now + timedelta(minutes=2):
        played -= timedelta(days=1)
    return played


def fingerprint(title, played_at):
    normalized = re.sub(r"[^a-z0-9]+", "", title.casefold())
    return f"{normalized}:{played_at.strftime('%Y%m%d-%H%M')}"


def send_whatsapp(message):
    if DRY_RUN:
        log.info("DRY_RUN: Nachricht würde gesendet werden: %s", message)
        return "dry-run"
    if not META_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        raise RuntimeError("META_ACCESS_TOKEN und META_PHONE_NUMBER_ID müssen als Render Environment Variables gesetzt sein.")
    to = re.sub(r"\D", "", RECIPIENT)
    if not to or to.startswith("0"):
        raise RuntimeError("WHATSAPP_RECIPIENT muss eine bestätigte WhatsApp-Empfängernummer im internationalen Format ohne + sein, z. B. 49... .")
    url = f"https://graph.facebook.com/{META_API_VERSION}/{META_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    response = requests.post(url, headers={
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }, json=payload, timeout=20)
    if not response.ok:
        raise RuntimeError(f"Meta WhatsApp API Fehler {response.status_code}: {response.text[:400]}")
    return response.json()


def handle_queen_row(row, now=None):
    if row["artist"].strip().casefold() != "queen":
        return
    now = now or datetime.now(BERLIN)
    played_at = played_datetime(row["time"], now)
    age = (now - played_at).total_seconds() / 60
    if age < -1 or age > MAX_SONG_AGE_MINUTES:
        log.info("Queen-Eintrag ist nicht frisch genug (%s, %.1f Minuten alt); keine Meldung.", row["time"], age)
        return
    fp = fingerprint(row["title"], played_at)
    with sqlite3.connect(DB_PATH) as con:
        if con.execute("SELECT 1 FROM sent WHERE fingerprint=?", (fp,)).fetchone():
            log.info("Titel bereits gemeldet: Queen - %s (%s)", row["title"], row["time"])
            return
    message = f"Jetzt läuft gerade Queen - {row['title']}"
    result = send_whatsapp(message)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO sent VALUES (?, ?, ?, ?, ?)", (
            fp, row["title"], played_at.isoformat(), datetime.now(timezone.utc).isoformat(),
            "dry-run" if result == "dry-run" else "sent",
        ))
    log.info("Queen-Titel verarbeitet: %s (%s), Status=%s", row["title"], row["time"], result if isinstance(result, str) else "accepted by Meta API")


def main():
    db_init()
    log.info("Queen Watch gestartet; URL=%s; Poll=%ss; DRY_RUN=%s; max song age=%sm", PLAYLIST_URL, POLL_SECONDS, DRY_RUN, MAX_SONG_AGE_MINUTES)
    while True:
        try:
            rows = fetch_recent_rows()
            log.info("Song-Historie gelesen: %d Einträge; neuester=%s – %s", len(rows), rows[0]["time"], rows[0]["title"])
            # Only alert for recent Queen rows, not old historical plays from earlier in the day.
            for row in rows:
                if row["artist"].strip().casefold() == "queen":
                    handle_queen_row(row)
                    break
        except Exception:
            log.exception("Überwachungsrunde fehlgeschlagen; nächster Versuch in %s Sekunden.", POLL_SECONDS)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
