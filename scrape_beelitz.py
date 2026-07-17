#!/usr/bin/env python3
"""
Erstellt aus dem EventON-Kalender der Stadt Beelitz eine abonnierbare ICS-Datei.

Die Seite wird mit einem echten Browser geladen, weil die Monatsdaten per
JavaScript/AJAX nachgeladen werden. Es werden standardisierte EventON-
Datenattribute bevorzugt; sichtbarer Text dient nur als Fallback.
"""

from __future__ import annotations

import asyncio
import html
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
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
    """Faltet ICS-Zeilen nach UTF-8-Bytes statt nach Unicode-Zeichen."""
    if len(line.encode("utf-8")) <= limit:
        return [line]
    result: list[str] = []
    first = True
    rest = line
    while rest:
        prefix = "" if first else " "
        budget = limit - len(prefix.encode("utf-8"))
        used = 0
        split_at = 0
        for index, character in enumerate(rest, start=1):
            size = len(character.encode("utf-8"))
            if used + size > budget:
                break
            used += size
            split_at = index
        if split_at == 0:
            split_at = 1
        chunk, rest = rest[:split_at], rest[split_at:]
        result.append(prefix + chunk)
        first = False
    return result


def ics_unescape(value: str) -> str:
    return (
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def clean_description(value: str) -> str:
    """Bereinigt HTML-Entitäten und einfache HTML-Reste aus EventON-Texten."""
    value = html.unescape(ics_unescape(value)).replace("\xa0", " ")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</(?:p|div)\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    cleaned_lines = [clean(line) for line in value.splitlines()]
    return "\n".join(line for line in cleaned_lines if line)


def unfold_ics(content: str) -> list[str]:
    result: list[str] = []
    for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def native_vevent_lines(
    content: str,
    event: Event,
    sync_note: str,
) -> list[str] | None:
    """Übernimmt das VEVENT aus EventON und stabilisiert UID, DTSTAMP und URL."""
    unfolded = unfold_ics(content)
    try:
        start_index = next(
            index for index, line in enumerate(unfolded) if line.strip() == "BEGIN:VEVENT"
        )
        end_index = next(
            index
            for index in range(start_index + 1, len(unfolded))
            if unfolded[index].strip() == "END:VEVENT"
        )
    except StopIteration:
        return None

    body = unfolded[start_index + 1 : end_index]
    kept: list[str] = []
    native_description = ""
    inside_alarm = False

    for line in body:
        marker = line.strip().upper()
        if marker == "BEGIN:VALARM":
            inside_alarm = True
            continue
        if inside_alarm:
            if marker == "END:VALARM":
                inside_alarm = False
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        property_name = name.split(";", 1)[0].upper()
        if property_name in {"UID", "DTSTAMP", "URL", "DESCRIPTION"}:
            if property_name == "DESCRIPTION":
                native_description = clean_description(value)
            continue
        if property_name == "LOCATION" and not clean_description(value):
            continue
        kept.append(line)

    digest = hashlib.sha256(event.uid_source.encode("utf-8")).hexdigest()[:28]
    source_note = f"Quelle: {event.url}\n{sync_note}"
    description = (
        f"{native_description.rstrip()}\n\n{source_note}"
        if native_description.strip()
        else source_note
    )

    result = [
        "BEGIN:VEVENT",
        f"UID:{digest}@beelitz-calendar",
        "DTSTAMP:19700101T000000Z",
        *kept,
        f"DESCRIPTION:{ics_escape(description)}",
        f"URL:{event.url}",
        "END:VEVENT",
    ]
    folded: list[str] = []
    for line in result:
        folded.extend(fold(line))
    return folded


def render_ics(
    events: list[Event],
    native_ics: dict[str, str] | None = None,
    sync_note: str = "",
) -> str:
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

    native_ics = native_ics or {}
    for event in sorted(events, key=lambda item: (item.start, item.title.casefold())):
        native_lines = (
            native_vevent_lines(native_ics[event.uid_source], event, sync_note)
            if event.uid_source in native_ics
            else None
        )
        if native_lines:
            lines.extend(native_lines)
            continue

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
        if sync_note:
            source_note += f"\n{sync_note}"
        description = f"{description}\n\n{source_note}" if description else source_note
        lines.append(f"DESCRIPTION:{ics_escape(description)}")
        lines.append(f"URL:{event.url}")
        lines.extend(["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"])

    lines.append("END:VCALENDAR")
    output: list[str] = []
    for line in lines:
        output.extend(fold(line))
    return "\r\n".join(output) + "\r\n"


def validate_ics(
    content: str,
    expected_event_count: int,
    previous_content: str = "",
) -> None:
    """Verhindert die Veröffentlichung einer leeren oder beschädigten ICS."""
    errors: list[str] = []
    if not content.startswith("BEGIN:VCALENDAR\r\n"):
        errors.append("VCALENDAR-Anfang fehlt")
    if not content.rstrip().endswith("END:VCALENDAR"):
        errors.append("VCALENDAR-Ende fehlt")
    if "BEGIN:VALARM" in content:
        errors.append("unerwartete VALARM-Einträge vorhanden")
    if re.search(r"^LOCATION:\s*$", content, re.MULTILINE):
        errors.append("leere LOCATION-Eigenschaften vorhanden")
    if "&nbsp;" in content.lower():
        errors.append("nicht bereinigte &nbsp;-Entitäten vorhanden")

    event_blocks = re.findall(
        r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT",
        content,
        flags=re.DOTALL,
    )
    if len(event_blocks) != expected_event_count:
        errors.append(
            f"VEVENT-Anzahl {len(event_blocks)} statt {expected_event_count}"
        )
    if expected_event_count < 10:
        errors.append(f"unplausibel wenige Veranstaltungen: {expected_event_count}")

    previous_count = previous_content.count("BEGIN:VEVENT")
    if previous_count >= 20 and expected_event_count < previous_count / 2:
        errors.append(
            f"starker Rückgang von {previous_count} auf {expected_event_count} Termine"
        )

    uids: list[str] = []
    for index, block in enumerate(event_blocks, start=1):
        lines = unfold_ics(block)
        properties = [line.split(":", 1)[0].split(";", 1)[0].upper() for line in lines if ":" in line]
        for required in ("UID", "DTSTART", "SUMMARY", "DESCRIPTION", "URL"):
            if properties.count(required) != 1:
                errors.append(
                    f"Termin {index}: {required} kommt {properties.count(required)}-mal vor"
                )
        uid_line = next((line for line in lines if line.startswith("UID:")), "")
        if uid_line:
            uids.append(uid_line[4:])
        url_line = next((line for line in lines if line.startswith("URL:")), "")
        if url_line and not url_line.startswith("URL:https://beelitz.de/events/"):
            errors.append(f"Termin {index}: unerwartete URL")
        description_line = next(
            (line for line in lines if line.startswith("DESCRIPTION:")), ""
        )
        if "Zuletzt mit beelitz.de abgeglichen:" not in description_line:
            errors.append(f"Termin {index}: Abgleichvermerk fehlt")

    if len(uids) != len(set(uids)):
        errors.append("doppelte UIDs vorhanden")

    long_lines = [
        number
        for number, line in enumerate(content.split("\r\n"), start=1)
        if len(line.encode("utf-8")) > 75
    ]
    if long_lines:
        errors.append(f"{len(long_lines)} ICS-Zeilen länger als 75 Byte")

    if errors:
        preview = "\n- ".join(errors[:20])
        raise RuntimeError(f"ICS-Validierung fehlgeschlagen:\n- {preview}")
    print(f"ICS-Validierung erfolgreich: {expected_event_count} Termine")


async def fetch_native_ics(request_context, events: list[Event]) -> dict[str, str]:
    """Lädt EventONs offizielle Einzel-ICS-Dateien mit begrenzter Parallelität."""
    semaphore = asyncio.Semaphore(8)
    export_pattern = re.compile(
        r'''href=["']([^"']*/export-events/[^"']+)["']''',
        re.IGNORECASE,
    )

    async def response_text(response) -> str:
        """Dekodiert auch Beelitz-Antworten mit falsch deklariertem Charset."""
        body = await response.body()
        content_type = response.headers.get("content-type", "")
        charset_match = re.search(
            r"charset\s*=\s*[\"']?([^;\s\"']+)",
            content_type,
            flags=re.IGNORECASE,
        )
        candidates: list[str] = []
        if charset_match:
            candidates.append(charset_match.group(1))
        candidates.extend(["utf-8-sig", "cp1252", "latin-1"])

        for encoding in dict.fromkeys(candidates):
            try:
                return body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace")

    async def fetch_one(event: Event) -> tuple[str, str | None]:
        async with semaphore:
            try:
                event_response = await request_context.get(event.url, timeout=30000)
                if not event_response.ok:
                    return event.uid_source, None
                event_html = await response_text(event_response)
                match = export_pattern.search(event_html)
                if not match:
                    return event.uid_source, None

                export_url = urljoin(event.url, html.unescape(match.group(1)))
                ics_response = await request_context.get(export_url, timeout=30000)
                content = await response_text(ics_response)
                # Die Beelitz-Installation liefert den gültigen EventON-
                # Kalenderdownload derzeit teilweise mit HTTP 400 aus.
                # Deshalb entscheidet der ICS-Inhalt, nicht response.ok.
                if "BEGIN:VEVENT" not in content or "END:VEVENT" not in content:
                    return event.uid_source, None
                return event.uid_source, content
            except Exception as exc:
                print(f"Native ICS nicht abrufbar für {event.url}: {exc}")
                return event.uid_source, None

    fetched = await asyncio.gather(*(fetch_one(event) for event in events))
    return {uid: content for uid, content in fetched if content is not None}


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
                    // EventON legt Zeit- und Wiederholungsdaten je nach
                    // Version nicht am äußeren Ereignis, sondern z. B. am
                    // inneren Auslöser (.evcal_list_a) ab.
                    for (const name of names) {
                        const node = e.querySelector(`[${name}]`);
                        if (!node) continue;
                        const value = node.getAttribute(name);
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
                const link = e.querySelector(
                    ".evo_event_schema a[itemprop='url'][href], a[itemprop='url'][href], .evcal_list_a[href], a[href]"
                );
                return {
                    id: attr("data-event_id", "data-event-id") || e.id || "",
                    ri: attr("data-ri", "data-repeat-instance") || "0",
                    start: attr("data-s", "data-start", "data-start-time"),
                    end: attr("data-e", "data-end", "data-end-time"),
                    timeRange: attr("data-time"),
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

        # Neuere EventON-Versionen speichern Start und Ende gemeinsam als
        # "START_EPOCH-END_EPOCH" im Attribut data-time.
        time_range = clean(raw.get("timeRange"))
        if time_range:
            range_match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", time_range)
            if range_match:
                if start is None:
                    start = parse_epoch(range_match.group(1))
                if end is None:
                    end = parse_epoch(range_match.group(2))

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

    # Ein eventuell noch vorhandener Borlabs-Hintergrund fängt echte
    # Mausereignisse ab. Nach dem Entfernen kann EventON seinen normalen
    # Click-Handler ausführen und den Folgemonat per AJAX laden.
    await page.evaluate(
        """() => {
            document.querySelector("#BorlabsDialogBackdrop")?.remove();
            document.querySelector("#BorlabsCookieBox")?.remove();
            document.documentElement.style.overflow = "";
            document.body.style.overflow = "";
        }"""
    )
    await button.click()
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
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Der Monatswechsel nach '{title or 'unbekannt'}' ist fehlgeschlagen."
        ) from exc
    return True


async def main() -> None:
    all_events: dict[str, Event] = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1440, "height": 1100},
        )
        page = await context.new_page()
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)

        # Cookie-Banner nur schließen, wenn er die Bedienung blockiert.
        for selector in [
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Nur notwendige')",
            ".borlabs-cookie-btn-accept-all",
            "[data-borlabs-cookie-accept-all]",
            ".brlbs-cmpnt-btn-accept-all",
        ]:
            button = page.locator(selector).first
            if await button.count() and await button.is_visible():
                try:
                    await button.click(timeout=2000)
                except Exception:
                    pass
                break

        # Der aktuelle Borlabs-Dialog kann trotz fehlendem sichtbaren Knopf
        # einen Backdrop behalten. Für das reine Lesen der öffentlichen
        # Veranstaltungsliste wird die blockierende Oberfläche entfernt.
        await page.evaluate(
            """() => {
                document.querySelector("#BorlabsDialogBackdrop")?.remove();
                document.querySelector("#BorlabsCookieBox")?.remove();
                document.documentElement.style.overflow = "";
                document.body.style.overflow = "";
            }"""
        )

        try:
            await page.wait_for_selector(
                ".ajde_evcal_calendar .eventon_list_event",
                timeout=30000,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Der EventON-Kalender wurde nicht geladen.") from exc

        for month_index in range(MONTHS_AHEAD):
            await page.wait_for_timeout(800)
            month_events = await extract_events(page)
            for event in month_events:
                all_events[event.uid_source] = event

            if month_index == MONTHS_AHEAD - 1:
                break
            if not await click_next_month(page):
                break

        events = list(all_events.values())
        if not events:
            raise RuntimeError("Keine Veranstaltungen gefunden; bestehende ICS-Datei bleibt unverändert.")

        native_ics = await fetch_native_ics(context.request, events)
        print(
            f"Native Beelitz-ICS geladen: {len(native_ics)}/{len(events)}; "
            f"Fallback: {len(events) - len(native_ics)}"
        )
        await browser.close()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sync_note = (
        "Zuletzt mit beelitz.de abgeglichen: "
        f"{datetime.now(TZ):%d.%m.%Y, %H:%M Uhr}"
    )
    new_content = render_ics(events, native_ics, sync_note)
    old_content = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
    validate_ics(new_content, len(events), old_content)
    if new_content != old_content:
        OUTPUT.write_text(new_content, encoding="utf-8", newline="")
        print(f"{len(events)} Veranstaltungen geschrieben: {OUTPUT}")
    else:
        print(f"Keine Änderung; {len(events)} Veranstaltungen vorhanden.")


if __name__ == "__main__":
    asyncio.run(main())
