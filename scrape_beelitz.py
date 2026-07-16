#!/usr/bin/env python3
"""
Erstellt aus dem EventON-Kalender der Stadt Beelitz eine abonnierbare ICS-Datei.

Die Seite wird mit einem echten Browser geladen, weil die Monatsdaten per
JavaScript/AJAX nachgeladen werden. Es werden standardisierte EventON-
Datenattribute bevorzugt; sichtbarer Text dient nur als Fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SOURCE_URL = "https://beelitz.de/veranstaltungen/"
OUTPUT = Path("public/beelitz.ics")
MONTHS_AHEAD = 18
TZ = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class Event:
    uid_source: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    location: str = ""
    description: str = ""
    url: str = SOURCE_URL


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_epoch(value: str | None) -> datetime | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group(0))
    if abs(number) > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, timezone.utc).astimezone(TZ)
    except (OverflowError, OSError, ValueError):
        return None


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold(line: str, limit: int = 73) -> list[str]:
    if len(line) <= limit:
        return [line]
    result: list[str] = []
    first = True
    rest = line
    while rest:
        width = limit if first else limit - 1
        chunk, rest = rest[:width], rest[width:]
        result.append(chunk if first else " " + chunk)
        first = False
    return result


def render_ics(events: list[Event]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Beelitz Calendar Sync//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Veranstaltungen Beelitz",
        "X-WR-CALDESC:Automatisch aus dem Veranstaltungskalender der Stadt Beelitz",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
        f"SOURCE:{SOURCE_URL}",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Berlin",
        "X-LIC-LOCATION:Europe/Berlin",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for event in sorted(events, key=lambda item: (item.start, item.title.casefold())):
        digest = hashlib.sha256(event.uid_source.encode("utf-8")).hexdigest()[:28]
        lines.extend(["BEGIN:VEVENT", f"UID:{digest}@beelitz-calendar"])
        # Konstant, damit die Datei nur bei echten Inhaltsänderungen geändert wird.
        lines.append("DTSTAMP:19700101T000000Z")

        if event.all_day:
            start_date = event.start.date()
            end_date = event.end.date()
            if end_date <= start_date:
                end_date = start_date + timedelta(days=1)
            # EventON verwendet bei Ganztagsterminen häufig 23:59 als Ende.
            if event.end.time().hour >= 23:
                end_date += timedelta(days=1)
            lines.append(f"DTSTART;VALUE=DATE:{start_date:%Y%m%d}")
            lines.append(f"DTEND;VALUE=DATE:{end_date:%Y%m%d}")
        else:
            end = event.end if event.end > event.start else event.start + timedelta(hours=1)
            lines.append(f"DTSTART;TZID=Europe/Berlin:{event.start:%Y%m%dT%H%M%S}")
            lines.append(f"DTEND;TZID=Europe/Berlin:{end:%Y%m%dT%H%M%S}")

        lines.append(f"SUMMARY:{ics_escape(event.title)}")
        if event.location:
            lines.append(f"LOCATION:{ics_escape(event.location)}")

        description = event.description
        source_note = f"Quelle: {event.url}"
        description = f"{description}\n\n{source_note}" if description else source_note
        lines.append(f"DESCRIPTION:{ics_escape(description)}")
        lines.append(f"URL:{event.url}")
        lines.extend(["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"])

    lines.append("END:VCALENDAR")
    output: list[str] = []
    for line in lines:
        output.extend(fold(line))
    return "\r\n".join(output) + "\r\n"


async def extract_events(page) -> list[Event]:
    locator = page.locator(".ajde_evcal_calendar .eventon_list_event")
    count = await locator.count()
    result: list[Event] = []

    for index in range(count):
        element = locator.nth(index)
        raw = await element.evaluate(
            """(e) => {
                const attr = (...names) => {
                    for (const name of names) {
                        const value = e.getAttribute(name);
                        if (value !== null && value !== "") return value;
                    }
                    return "";
                };
                const text = (...selectors) => {
                    for (const selector of selectors) {
                        const node = e.querySelector(selector);
                        if (node && node.textContent) return node.textContent;
                    }
                    return "";
                };
                const link = e.querySelector("a[href]");
                return {
                    id: attr("data-event_id", "data-event-id") || e.id || "",
                    ri: attr("data-ri", "data-repeat-instance") || "0",
                    start: attr("data-s", "data-start", "data-start-time"),
                    end: attr("data-e", "data-end", "data-end-time"),
                    allDay: attr("data-allday", "data-all-day", "data-all_day"),
                    classes: e.className || "",
                    title: text(
                        ".evcal_event_title",
                        ".evcal_desc2",
                        "[itemprop='name']",
                        "h3",
                        "h2"
                    ),
                    location: text(
                        ".evo_location_name",
                        ".event_location_name",
                        ".evo_location",
                        "[itemprop='location']"
                    ),
                    description: text(
                        ".eventon_full_description",
                        ".event_description",
                        ".evo_eventcard .eventon_desc_in",
                        ".evo_eventcard"
                    ),
                    href: link ? link.href : ""
                };
            }"""
        )

        title = clean(raw.get("title"))
        start = parse_epoch(raw.get("start"))
        end = parse_epoch(raw.get("end"))

        if not title or start is None:
            continue
        if end is None:
            end = start + timedelta(hours=1)

        all_day_value = clean(raw.get("allDay")).lower()
        classes = clean(raw.get("classes")).lower()
        all_day = (
            all_day_value in {"1", "yes", "true", "y"}
            or "all_day" in classes
            or "allday" in classes
        )

        uid_source = "|".join(
            [
                clean(raw.get("id")),
                clean(raw.get("ri")),
                start.isoformat(),
                title,
            ]
        )
        result.append(
            Event(
                uid_source=uid_source,
                title=title,
                start=start,
                end=end,
                all_day=all_day,
                location=clean(raw.get("location")),
                description=clean(raw.get("description"))[:2500],
                url=clean(raw.get("href")) or SOURCE_URL,
            )
        )
    return result


async def click_next_month(page) -> bool:
    calendar = page.locator(".ajde_evcal_calendar").first
    title_selector = ".evo_month_title, .evcal_month_line p, .evo_month_title_text"
    title = clean(await calendar.locator(title_selector).first.text_content()) if await calendar.locator(title_selector).count() else ""
    first_start = ""
    first_event = calendar.locator(".eventon_list_event").first
    if await first_event.count():
        first_start = await first_event.get_attribute("data-s") or ""

    candidates = [
        ".evcal_btn_next",
        ".evo_month_next",
        "[data-dir='next']",
        "[data-direction='next']",
        "button[aria-label*='next' i]",
        "a[aria-label*='next' i]",
        "button[title*='next' i]",
        "a[title*='next' i]",
        "button[aria-label*='weiter' i]",
        "a[aria-label*='weiter' i]",
    ]

    button = None
    for selector in candidates:
        candidate = calendar.locator(selector).first
        if await candidate.count() and await candidate.is_visible():
            button = candidate
            break
    if button is None:
        return False

    await button.click(force=True)
    try:
        await page.wait_for_function(
            """({title, firstStart}) => {
                const cal = document.querySelector(".ajde_evcal_calendar");
                if (!cal) return false;
                const titleNode = cal.querySelector(".evo_month_title, .evcal_month_line p, .evo_month_title_text");
                const newTitle = titleNode ? (titleNode.textContent || "").trim() : "";
                const first = cal.querySelector(".eventon_list_event");
                const newStart = first ? (first.getAttribute("data-s") || "") : "";
                return (newTitle && newTitle !== title) || (newStart && newStart !== firstStart);
            }""",
            arg={"title": title, "firstStart": first_start},
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(2500)
    return True


async def main() -> None:
    all_events: dict[str, Event] = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1440, "height": 1100},
        )
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)

        # Cookie-Banner nur schließen, wenn er die Bedienung blockiert.
        for selector in [
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Nur notwendige')",
            ".borlabs-cookie-btn-accept-all",
        ]:
            button = page.locator(selector).first
            if await button.count() and await button.is_visible():
                try:
                    await button.click(timeout=2000)
                except Exception:
                    pass
                break

        try:
            await page.wait_for_selector(
                ".ajde_evcal_calendar .eventon_list_event",
                timeout=30000,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Der EventON-Kalender wurde nicht geladen.") from exc

        for month_index in range(MONTHS_AHEAD):
            await page.wait_for_timeout(800)
            for event in await extract_events(page):
                all_events[event.uid_source] = event

            if month_index == MONTHS_AHEAD - 1:
                break
            if not await click_next_month(page):
                break

        await browser.close()

    events = list(all_events.values())
    if not events:
        raise RuntimeError("Keine Veranstaltungen gefunden; bestehende ICS-Datei bleibt unverändert.")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    new_content = render_ics(events)
    old_content = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    if new_content != old_content:
        OUTPUT.write_text(new_content, encoding="utf-8", newline="")
        print(f"{len(events)} Veranstaltungen geschrieben: {OUTPUT}")
    else:
        print(f"Keine Änderung; {len(events)} Veranstaltungen vorhanden.")


if __name__ == "__main__":
    asyncio.run(main())
