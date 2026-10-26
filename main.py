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
TEST_QUEEN = os.getenv("TEST_QUEEN", "false").lower() in ("1", "true", "yes")
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
    response = session.get(PLAYLIST_URL, timeout=(8, 30))
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    log.info(
        "DIAGNOSE: HTTP=%s, Bytes=%s, Song-Zeitmarker=%s",
        response.status_code,
        len(response.text),
        len(re.findall(r"Gespielte Uhrzeit\s*\d{1,2}:\d{2}", page_text, re.I)),
    )

    rows = []

    # Text zwischen zwei Zeitmarkern als einzelnen Song-Eintrag lesen.
    pattern = re.compile(
        r"Gespielte Uhrzeit\s*(\d{1,2}:\d{2})\s*(.*?)"
        r"(?=Gespielte Uhrzeit|Infos|Nutzungsbedingungen|$)",
        re.I,
    )

    for match in pattern.finditer(page_text):
        time_text = match.group(1)
        entry = match.group(2).strip()

        # Titel und Interpret anhand der Jahreszahl trennen.
        # Titel und Interpret anhand des Jahres in Klammern trennen.
        song_match = re.match(
            r"(.+?)\s+\(((?:19|20)\d{2})\)\s+(.+?)\s*$",
            entry,
        )

        if not song_match:
            log.warning(
                "Song-Eintrag konnte nicht zerlegt werden: %s",
                entry[:160],
            )
            continue

        title = clean_title(
            song_match.group(1).strip() + " (" + song_match.group(2) + ")"
        )
        artist = song_match.group(3).strip()

        if plausible_title(title) and artist:
            rows.append({
                "time": time_text,
                "title": title,
                "artist": artist,
            })

    if not rows:
        log.error("DIAGNOSE: Keine Songs zerlegt. Seitentext: %s", page_text[:1600])
        raise RuntimeError("Keine Song-Einträge erkannt.")

    rows.sort(
        key=lambda row: (
            int(row["time"].split(":")[0]) * 60
            + int(row["time"].split(":")[1])
        ),
        reverse=True,
    )

    log.info("Playlist erkannt: %s", rows[:3])
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
    log.info(
        "Queen Watch gestartet; URL=%s; Poll=%ss; DRY_RUN=%s; max song age=%sm",
        PLAYLIST_URL, POLL_SECONDS, DRY_RUN, MAX_SONG_AGE_MINUTES
    )

    if TEST_QUEEN:
        test_row = {
            "time": datetime.now(BERLIN).strftime("%H:%M"),
            "title": "Queen Watch Test",
            "artist": "Queen",
        }
        log.info("TEST: Simuliere einen aktuellen Queen-Titel.")
        handle_queen_row(test_row)
        log.info("TEST: Queen-Test abgeschlossen.")

    while True:
        try:
            rows = fetch_recent_rows()
            log.info(
                "Song-Historie gelesen: %d Einträge; neuester=%s – %s",
                len(rows), rows[0]["time"], rows[0]["title"]
            )
            for row in rows:
                if row["artist"].strip().casefold() == "queen":
                    handle_queen_row(row)
                    break
        except Exception:
            log.exception(
                "Überwachungsrunde fehlgeschlagen; nächster Versuch in %s Sekunden.",
                POLL_SECONDS
            )
        time.sleep(POLL_SECONDS)
