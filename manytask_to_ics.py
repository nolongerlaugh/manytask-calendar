import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import os

from playwright.async_api import async_playwright, Page

MANYTASK_URL = "https://app.manytask.org/cpp-2026-spring/"
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
            if status:
                description_parts.append(f"Status: {status}")
            if percent is not None:
                description_parts.append(f"Percent: {percent}%")
            if task_url:
                description_parts.append(f"URL: {task_url}")

            uid = stable_uid("manytask", section_title, task_name, dt.isoformat(), task_url)

            events.append(
                Event(
                    uid=uid,
                    dtstart=dt,
                    dtend=dt + timedelta(hours=1),
                    summary=f"[Manytask] {task_name}",
                    description="\n".join(description_parts),
                    url=task_url,
                )
            )

    return events


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



async def login_manytask(page: Page, context) -> None:
    if not MANYTASK_USERNAME or not MANYTASK_PASSWORD:
        raise RuntimeError("Missing MANYTASK_USERNAME or MANYTASK_PASSWORD")

    await page.goto("https://app.manytask.org/", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Если уже залогинены и на странице курса есть секции — выходим
    await page.goto(MANYTASK_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    if await page.locator(".container-fluid.rounded.mt-lecture").count() > 0:
        await context.storage_state(path=str(STATE_FILE))
        return

    # Возвращаемся на главную Manytask
    await page.goto("https://app.manytask.org/", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # Для диагностики
    Path("debug_before_login.html").write_text(await page.content(), encoding="utf-8")

    # Ищем ссылку/кнопку логина более гибко
    login_candidates = [
        'a:has-text("Login with")',
        'button:has-text("Login with")',
        'text=Login with',
        'a[href*="gitlab"]',
        'a[href*="sign_in"]',
        'a[href*="login"]',
    ]

    clicked = False

    for selector in login_candidates:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            continue

        href = await loc.get_attribute("href")

        # Если это обычная ссылка — лучше перейти напрямую по href
        if href:
            if href.startswith("/"):
                href = "https://app.manytask.org" + href
            await page.goto(href, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            clicked = True
            break

        # Иначе пробуем клик
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=5000):
                await loc.click()
            await page.wait_for_timeout(3000)
            clicked = True
            break
        except Exception:
            try:
                await loc.click()
                await page.wait_for_timeout(3000)
                clicked = True
                break
            except Exception:
                pass

    if not clicked:
        Path("debug_login_not_clicked.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError(f"Could not click login entrypoint. Current URL: {page.url}")

    # Если после клика мы все еще на manytask, сохраняем страницу для диагностики
    if "app.manytask.org" in page.url:
        Path("debug_after_click.html").write_text(await page.content(), encoding="utf-8")

    # Иногда логин открывается в той же вкладке, иногда через редирект
    # Ищем gitlab-поля на текущей странице
    user_input = page.locator(
        'input[name="username"], input[name="user[login]"], input[autocomplete="username"]'
    ).first
    pass_input = page.locator(
        'input[name="password"], input[name="user[password]"], input[type="password"]'
    ).first

    # Если формы нет, попробуем явно открыть gitlab sign-in,
    # если текущий url уже на gitlab.manytask.org
    if await user_input.count() == 0 or await pass_input.count() == 0:
        if "gitlab.manytask.org" in page.url:
            await page.goto("https://gitlab.manytask.org/users/sign_in", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            user_input = page.locator(
                'input[name="username"], input[name="user[login]"], input[autocomplete="username"]'
            ).first
            pass_input = page.locator(
                'input[name="password"], input[name="user[password]"], input[type="password"]'
            ).first

    if await user_input.count() == 0 or await pass_input.count() == 0:
        Path("debug_gitlab_form_not_found.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError(f"GitLab login form not found. Current URL: {page.url}")

    await user_input.fill(MANYTASK_USERNAME)
    await pass_input.fill(MANYTASK_PASSWORD)

    sign_in_button = page.locator(
        'button:has-text("Sign in"), input[type="submit"], button[type="submit"]'
    ).first

    if await sign_in_button.count() == 0:
        Path("debug_signin_button_not_found.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError(f"GitLab sign in button not found. Current URL: {page.url}")

    await sign_in_button.click()
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(5000)

    # После логина идем на страницу курса
    await page.goto(MANYTASK_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    if await page.locator(".container-fluid.rounded.mt-lecture").count() == 0:
        Path("debug_after_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError(
            f"Login finished, but course sections still not found. Current URL: {page.url}"
        )

    await context.storage_state(path=str(STATE_FILE))

if __name__ == "__main__":
    asyncio.run(main())
