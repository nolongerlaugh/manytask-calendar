import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

MANYTASK_URL = "https://app.manytask.org/cpp-2026-spring/"
MANYTASK_SIGNUP_URL = "https://app.manytask.org/signup"
MANYTASK_LOGIN_URL = "https://app.manytask.org/login"
STATE_FILE = Path("manytask_state.json")
OUTPUT_ICS = Path("manytask.ics")
LOCAL_TZ = timezone(timedelta(hours=3))

MANYTASK_USERNAME = os.environ.get("MANYTASK_USERNAME", "")
MANYTASK_PASSWORD = os.environ.get("MANYTASK_PASSWORD", "")


@dataclass
class Event:
    uid: str
    dtstart: datetime
    dtend: datetime
    summary: str
    description: str
    url: str = ""


def stable_uid(*parts: str) -> str:
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{digest}@manytask.local"


def parse_percent(text: str) -> Optional[int]:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def parse_dt(date_text: str, time_text: str) -> datetime:
    dt = datetime.strptime(f"{date_text} {time_text}", "%d.%m.%Y %H:%M")
    return dt.replace(tzinfo=LOCAL_TZ)


def escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def format_dt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fold_ics_line(line: str, limit: int = 75) -> str:
    if len(line) <= limit:
        return line
    out = []
    while len(line) > limit:
        out.append(line[:limit])
        line = " " + line[limit:]
    out.append(line)
    return "\r\n".join(out)


def event_to_ics(event: Event) -> str:
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{format_dt_utc(now)}",
        f"DTSTART:{format_dt_utc(event.dtstart)}",
        f"DTEND:{format_dt_utc(event.dtend)}",
        f"SUMMARY:{escape_ics_text(event.summary)}",
        f"DESCRIPTION:{escape_ics_text(event.description)}",
        "STATUS:CONFIRMED",
    ]
    if event.url:
        lines.append(f"URL:{escape_ics_text(event.url)}")

    lines += [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Deadline reminder",
        "TRIGGER:-PT24H",
        "END:VALARM",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Deadline reminder",
        "TRIGGER:-PT2H",
        "END:VALARM",
        "END:VEVENT",
    ]
    return "\r\n".join(fold_ics_line(x) for x in lines)


def build_calendar(events: list[Event]) -> str:
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Custom//Manytask Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Manytask Deadlines",
        "X-WR-TIMEZONE:UTC",
    ]
    body.extend(event_to_ics(e) for e in sorted(events, key=lambda x: x.dtstart))
    body.append("END:VCALENDAR")
    body.append("")
    return "\r\n".join(body)


async def is_course_page(page: Page) -> bool:
    return await page.locator(".container-fluid.rounded.mt-lecture").count() > 0


async def is_course_list_page(page: Page) -> bool:
    return await page.locator('a.course-link[href="/cpp-2026-spring/"]').count() > 0


async def scrape_manytask(page: Page) -> list[Event]:
    await page.goto(MANYTASK_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    sections = page.locator(".container-fluid.rounded.mt-lecture")
    if await sections.count() == 0:
        raise RuntimeError(f"Manytask sections not found. Current URL: {page.url}")

    events: list[Event] = []

    for i in range(await sections.count()):
        section = sections.nth(i)

        section_title_loc = section.locator(".fs-2.mb-0").first
        section_title = (
            (await section_title_loc.inner_text()).strip()
            if await section_title_loc.count()
            else f"Section {i+1}"
        )

        task_name = section_title
        task_url = ""

        card = section.locator(".mt-task-card").first
        if await card.count():
            name_loc = card.locator(".mt-card__name").first
            link_loc = card.locator("a").first

            if await name_loc.count():
                task_name = (await name_loc.inner_text()).strip()
            if await link_loc.count():
                task_url = await link_loc.get_attribute("href") or ""

        deadlines = section.locator(".task-deadlines .task-deadline")

        for j in range(await deadlines.count()):
            dl = deadlines.nth(j)

            class_attr = await dl.get_attribute("class") or ""
            if "passed-deadline" in class_attr:
                continue

            status_loc = dl.locator(".task-deadline__status").first
            status = (await status_loc.inner_text()).strip() if await status_loc.count() else ""

            percent_loc = dl.locator(".deadline-percent").first
            percent_text = (await percent_loc.inner_text()).strip() if await percent_loc.count() else ""
            percent = parse_percent(percent_text)

            time_block = dl.locator(".task-deadline__deadline-time").first
            spans = time_block.locator("span span")

            values = []
            for k in range(await spans.count()):
                values.append((await spans.nth(k).inner_text()).strip())

            if len(values) < 2:
                continue

            dt = parse_dt(values[0], values[1])

            description_parts = [f"Section: {section_title}"]
            if task_name:
                description_parts.append(f"Task: {task_name}")
            if status:
                description_parts.append(f"Status: {status}")
            if percent is not None:
                description_parts.append(f"Percent: {percent}%")

            uid = stable_uid("manytask", section_title, task_name, dt.isoformat(), task_url)

            events.append(
                Event(
                    uid=uid,
                    dtstart=dt,
                    dtend=dt + timedelta(hours=1),
                    summary=f"[Manytask] {section_title}",
                    description="\n".join(description_parts),
                    url=task_url,
                )
            )

    return events


async def login_manytask(page: Page, context) -> None:
    # 1) Пробуем штатный manytask login flow
    await page.goto(MANYTASK_LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    Path("debug_login_target_page.html").write_text(await page.content(), encoding="utf-8")

    # 2) Если сразу попали в список курсов — просто открываем нужный курс
    if await is_course_list_page(page):
        await page.goto(MANYTASK_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        if await is_course_page(page):
            await context.storage_state(path=str(STATE_FILE))
            return

    # 3) Если сразу попали на сам курс — отлично
    if await is_course_page(page):
        await context.storage_state(path=str(STATE_FILE))
        return

    # 4) Если показали GitLab форму — логинимся
    user_input = page.locator(
        'input[name="username"], input[name="user[login]"], input[autocomplete="username"]'
    ).first
    pass_input = page.locator(
        'input[name="password"], input[name="user[password]"], input[type="password"]'
    ).first

    if await user_input.count() > 0 and await pass_input.count() > 0:
        if not MANYTASK_USERNAME or not MANYTASK_PASSWORD:
            raise RuntimeError("Missing MANYTASK_USERNAME or MANYTASK_PASSWORD")

        await user_input.fill(MANYTASK_USERNAME)
        await pass_input.fill(MANYTASK_PASSWORD)

        remember_me = page.locator(
            'input[type="checkbox"][name="remember_me"], input[type="checkbox"][name="user[remember_me]"]'
        ).first
        if await remember_me.count() > 0:
            try:
                await remember_me.check()
            except Exception:
                pass

        sign_in_button = page.locator(
            'button:has-text("Sign in"), input[type="submit"], button[type="submit"]'
        ).first

        if await sign_in_button.count() == 0:
            Path("debug_signin_button.html").write_text(await page.content(), encoding="utf-8")
            raise RuntimeError(f"GitLab sign in button not found. Current URL: {page.url}")

        await sign_in_button.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(5000)

        if await is_course_list_page(page) or await is_course_page(page):
            await page.goto(MANYTASK_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            if await is_course_page(page):
                await context.storage_state(path=str(STATE_FILE))
                return

    # 5) Последняя попытка: вдруг после /login уже есть доступ к курсу
    await page.goto(MANYTASK_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    if await is_course_page(page):
        await context.storage_state(path=str(STATE_FILE))
        return

    Path("debug_after_login.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError(f"Login flow did not reach course page. Current URL: {page.url}")


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        if STATE_FILE.exists():
            context = await browser.new_context(storage_state=str(STATE_FILE))
        else:
            context = await browser.new_context()

        page = await context.new_page()

        try:
            events = await scrape_manytask(page)
        except Exception:
            await login_manytask(page, context)
            events = await scrape_manytask(page)

        ics = build_calendar(events)
        OUTPUT_ICS.write_text(ics, encoding="utf-8")

        await browser.close()

    print(f"Saved {OUTPUT_ICS.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
