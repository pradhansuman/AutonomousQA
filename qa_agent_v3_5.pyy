import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright


MAX_DEPTH = 3
MAX_PAGES = 30
MAX_LINKS_PER_PAGE = 20
NAV_TIMEOUT = 20000
ACTION_TIMEOUT = 7000
REPORT_DIR = Path("qa_v3_4_report")
SCREENSHOT_DIR = REPORT_DIR / "screenshots"

REPORT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slug(value):
    value = re.sub(r"[^A-Za-z0-9_-]", "_", clean(value))
    return value[:100] or "unknown"


def normalize_url(url):
    return urlparse(url)._replace(fragment="").geturl()


def same_domain(a, b):
    return urlparse(a).netloc == urlparse(b).netloc


def classify(e):
    tag = clean(e.get("tag")).lower()
    role = clean(e.get("role")).lower()
    typ = clean(e.get("input_type")).lower()

    if role == "button" or tag == "button":
        return "button"
    if role == "tab":
        return "tab"
    if role == "checkbox" or typ == "checkbox":
        return "checkbox"
    if role == "radio" or typ == "radio":
        return "radio"
    if role == "combobox":
        return "combobox"
    if tag == "textarea":
        return "text_area"
    if tag == "select":
        return "dropdown"
    if typ == "range":
        return "slider"
    if typ == "file":
        return "file_upload"
    if typ == "date":
        return "date_picker"
    if tag == "input":
        return "text_input"
    return "unknown"


class QAAgent:
    def __init__(self, target):
        self.target = normalize_url(target)
        self.queue = []
        self.visited = set()
        self.pages = []
        self.tests = []
        self.results = []
        self.dialogs = []

    def add_url(self, url, depth):
        url = normalize_url(url)
        if not url.startswith(("http://", "https://")):
            return
        if not same_domain(self.target, url):
            return
        if url in self.visited:
            return
        if depth > MAX_DEPTH:
            return
        if any(x["url"] == url for x in self.queue):
            return
        self.queue.append({"url": url, "depth": depth})

    async def discover(self, page, url, depth):
        print(f"\n🌐 [{depth}] {url}")

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )
            await page.wait_for_timeout(500)
        except Exception as exc:
            print(f"   ❌ Navigation failed: {exc}")
            return

        title = await page.title()

        elements = await page.locator(
            "a,button,input,textarea,select,[role],[aria-label]"
        ).evaluate_all(
            """
            els => els.map((e, i) => {
                const r = e.getBoundingClientRect();
                const s = getComputedStyle(e);
                return {
                    index: i,
                    tag: e.tagName.toLowerCase(),
                    text: (e.innerText || "").trim(),
                    role: e.getAttribute("role") || "",
                    aria_label: e.getAttribute("aria-label") || "",
                    placeholder: e.getAttribute("placeholder") || "",
                    input_type: e.getAttribute("type") || "",
                    name: e.getAttribute("name") || "",
                    id: e.id || "",
                    title: e.getAttribute("title") || "",
                    disabled: !!e.disabled,
                    readonly: !!e.readOnly,
                    visible:
                        r.width > 0 &&
                        r.height > 0 &&
                        s.display !== "none" &&
                        s.visibility !== "hidden" &&
                        s.opacity !== "0",
                    aria_hidden:
                        e.getAttribute("aria-hidden") === "true"
                };
            })
            """
        )

        usable = []

        for e in elements:
            if not e["visible"] or e["aria_hidden"]:
                continue

            e["semantic"] = classify(e)

            if e["semantic"] != "unknown":
                usable.append(e)

        links = await page.locator("a[href]").evaluate_all(
            "els => els.map(a => ({text:(a.innerText||'').trim(), href:a.href}))"
        )

        self.pages.append(
            {
                "url": url,
                "depth": depth,
                "title": title,
                "elements": usable,
                "links": links,
            }
        )

        print(f"   📄 {title}")
        print(f"   🧩 Elements: {len(usable)}")

        for link in links:
            href = link.get("href")
            if href:
                self.add_url(urljoin(url, href), depth + 1)

    def fingerprint(self, e):
        return {
            "semantic": e.get("semantic"),
            "tag": e.get("tag"),
            "text": clean(e.get("text")),
            "aria_label": clean(e.get("aria_label")),
            "placeholder": clean(e.get("placeholder")),
            "name": clean(e.get("name")),
            "id": clean(e.get("id")),
            "input_type": clean(e.get("input_type")),
            "title": clean(e.get("title")),
        }

    async def resolve(self, page, fp):
        semantic = fp["semantic"]
        candidates = []

        text = fp["text"]
        aria = fp["aria_label"]
        placeholder = fp["placeholder"]
        name = fp["name"]
        element_id = fp["id"]

        if semantic == "button":
            if text:
                candidates.append(page.get_by_role("button", name=text, exact=True))
            if aria:
                candidates.append(page.get_by_role("button", name=aria, exact=True))
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            candidates.append(page.locator("button"))

        elif semantic == "text_input":
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            if name:
                candidates.append(page.locator(f'input[name="{name}"]'))
            if aria:
                candidates.append(page.get_by_label(aria, exact=True))
            if placeholder:
                candidates.append(page.get_by_placeholder(placeholder, exact=True))
            candidates.append(page.locator("input:not([type=hidden])"))

        elif semantic == "text_area":
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            if aria:
                candidates.append(page.get_by_label(aria, exact=True))
            if placeholder:
                candidates.append(page.get_by_placeholder(placeholder, exact=True))
            candidates.append(page.locator("textarea"))

        elif semantic == "checkbox":
            if aria:
                candidates.append(page.get_by_label(aria, exact=True))
            if text:
                candidates.append(page.get_by_label(text, exact=True))
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            candidates.append(page.locator('input[type="checkbox"]'))

        elif semantic == "radio":
            if aria:
                candidates.append(page.get_by_label(aria, exact=True))
            if text:
                candidates.append(page.get_by_label(text, exact=True))
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            candidates.append(page.locator('input[type="radio"]'))

        elif semantic == "slider":
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            candidates.append(page.locator('input[type="range"]'))

        elif semantic == "dropdown":
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            candidates.append(page.locator("select"))

        elif semantic == "combobox":
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            if aria:
                candidates.append(page.get_by_role("combobox", name=aria, exact=True))
            candidates.append(page.get_by_role("combobox"))

        elif semantic == "tab":
            if text:
                candidates.append(page.get_by_role("tab", name=text, exact=True))
            candidates.append(page.get_by_role("tab"))

        elif semantic == "file_upload":
            candidates.append(page.locator('input[type="file"]'))

        elif semantic == "date_picker":
            if element_id:
                candidates.append(page.locator(f"#{element_id}"))
            candidates.append(page.locator('input[type="date"]'))

        for locator in candidates:
            try:
                count = await locator.count()
                visible = []
                for i in range(count):
                    item = locator.nth(i)
                    if await item.is_visible():
                        visible.append(item)

                if len(visible) == 1:
                    return visible[0]

                # If multiple candidates exist, choose the first enabled,
                # visible element with the strongest semantic match.
                for item in visible:
                    try:
                        if await item.is_enabled():
                            return item
                    except Exception:
                        pass
            except Exception:
                pass

        return None

    def generate_tests(self):
        seen = set()

        for p in self.pages:
            url = p["url"]

            key = ("page_load", url)
            if key not in seen:
                self.tests.append(
                    {
                        "type": "page_load",
                        "url": url,
                        "fingerprint": None,
                        "description": "Page should load successfully",
                    }
                )
                seen.add(key)

            for e in p["elements"]:
                semantic = e["semantic"]

                if not e["visible"]:
                    continue

                if e["disabled"] and semantic not in ("button",):
                    # Dynamic buttons are handled separately below.
                    if semantic != "button":
                        continue

                fp = self.fingerprint(e)
                label = (
                    e.get("text")
                    or e.get("aria_label")
                    or e.get("placeholder")
                    or e.get("name")
                    or semantic
                )

                test = None

                if semantic == "text_input" and not e["disabled"] and not e["readonly"]:
                    test = ("text_input", f"Enter valid data into {label}")

                elif semantic == "text_area" and not e["disabled"] and not e["readonly"]:
                    test = ("text_area", f"Enter text into {label}")

                elif semantic == "checkbox":
                    test = ("checkbox", f"Toggle {label}")

                elif semantic == "radio":
                    test = ("radio", f"Select {label}")

                elif semantic == "slider":
                    test = ("slider", f"Change {label}")

                elif semantic == "dropdown":
                    test = ("dropdown", f"Test dropdown {label}")

                elif semantic == "combobox":
                    test = ("combobox", f"Test combobox {label}")

                elif semantic == "file_upload":
                    test = ("file_upload", f"Verify upload control {label}")

                elif semantic == "date_picker":
                    test = ("date_picker", f"Interact with {label}")

                elif semantic == "tab":
                    test = ("tab", f"Activate tab {label}")

                elif semantic == "button":
                    low = str(label).lower()
                    if e["disabled"] or any(
                        x in low for x in ("enable", "enabled", "seconds")
                    ):
                        test = ("dynamic_button", f"Wait for dynamic button {label}")
                    else:
                        test = ("button", f"Activate {label}")

                if test:
                    typ, desc = test
                    key = (typ, url, json.dumps(fp, sort_keys=True))
                    if key not in seen:
                        self.tests.append(
                            {
                                "type": typ,
                                "url": url,
                                "fingerprint": fp,
                                "description": desc,
                            }
                        )
                        seen.add(key)

        print(f"\n🧠 TESTS GENERATED: {len(self.tests)}")

    async def cleanup(self, page):
        print("   🧹 CLEANUP")

        selectors = [
            '[role="dialog"]:visible button[aria-label="Close"]',
            '[role="dialog"]:visible button[aria-label="close"]',
            '[role="dialog"]:visible button.close',
            '[role="dialog"]:visible button:has-text("Close")',
            '[role="dialog"]:visible button:has-text("Cancel")',
            '[aria-modal="true"]:visible button[aria-label="Close"]',
            '[aria-modal="true"]:visible button.close',
            '[aria-modal="true"]:visible button:has-text("Close")',
            '[aria-modal="true"]:visible button:has-text("Cancel")',
            '.modal.show:visible button.close',
            '.modal.show:visible button:has-text("Close")',
        ]

        for selector in selectors:
            try:
                loc = page.locator(selector)
                for i in range(await loc.count()):
                    item = loc.nth(i)
                    if await item.is_visible():
                        try:
                            await item.click(timeout=1500)
                            await page.wait_for_timeout(200)
                        except Exception:
                            pass
            except Exception:
                pass

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

        print("   ✓ UI clean")

    async def fail(self, page, test, error):
        name = (
            f"{slug(urlparse(test['url']).path)}_"
            f"{slug(test['type'])}_"
            f"{len(self.results)}.png"
        )
        path = SCREENSHOT_DIR / name

        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception:
            path = None

        result = {
            **test,
            "status": "FAIL",
            "error": str(error),
            "evidence": str(path) if path else None,
        }

        self.results.append(result)

        print(f"   ❌ FAIL {test['description']}")
        print(f"      Reason: {error}")
        if path:
            print(f"      Evidence: {path}")

    async def execute(self, page, test):
        print(f"\n🧪 {test['description']}")

        try:
            await page.goto(
                test["url"],
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )
            await page.wait_for_timeout(300)

            typ = test["type"]

            if typ == "page_load":
                await page.locator("body").wait_for(
                    state="visible",
                    timeout=5000,
                )
                self.results.append({**test, "status": "PASS"})
                print("   ✅ PASS")
                return

            element = await self.resolve(
                page,
                test["fingerprint"],
            )

            if element is None:
                raise AssertionError("Semantic element could not be resolved")

            if typ == "text_input":
                await element.fill("QA_AGENT_TEST", timeout=ACTION_TIMEOUT)
                actual = await element.input_value()
                expected = "QA_AGENT_TEST"
                # Respect maxlength if the application imposes one.
                maxlength = await element.get_attribute("maxlength")
                if maxlength and maxlength.isdigit():
                    expected = expected[: int(maxlength)]
                if actual != expected:
                    raise AssertionError(f"Expected '{expected}', got '{actual}'")
                print("   ✅ PASS value persisted")
                self.results.append({**test, "status": "PASS"})

            elif typ == "text_area":
                await element.fill("Autonomous QA Agent", timeout=ACTION_TIMEOUT)
                actual = await element.input_value()
                if actual != "Autonomous QA Agent":
                    raise AssertionError(f"Value did not persist: '{actual}'")
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            elif typ == "checkbox":
                await element.check(timeout=ACTION_TIMEOUT)
                if not await element.is_checked():
                    raise AssertionError("Checkbox did not become checked")
                await element.uncheck(timeout=ACTION_TIMEOUT)
                if await element.is_checked():
                    raise AssertionError("Checkbox did not become unchecked")
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            elif typ == "radio":
                await element.check(timeout=ACTION_TIMEOUT)
                if not await element.is_checked():
                    raise AssertionError("Radio was not selected")
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            elif typ == "slider":
                before = await element.input_value()
                await element.focus()
                await page.keyboard.press("ArrowRight")
                after = await element.input_value()
                print(f"   ✅ PASS slider {before} → {after}")
                self.results.append({**test, "status": "PASS"})

            elif typ == "dropdown":
                count = await element.locator("option").count()
                if count == 0:
                    raise AssertionError("Dropdown has no options")
                if count > 1:
                    await element.select_option(index=1)
                print(f"   ✅ PASS {count} options")
                self.results.append({**test, "status": "PASS"})

            elif typ == "combobox":
                await element.click(timeout=ACTION_TIMEOUT)
                await page.wait_for_timeout(300)
                print("   ✅ PASS combobox activated")
                self.results.append({**test, "status": "PASS"})

            elif typ == "button":
                if await element.is_disabled():
                    raise AssertionError("Button is disabled")
                await element.click(timeout=ACTION_TIMEOUT)
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            elif typ == "dynamic_button":
                enabled = False
                for _ in range(15):
                    element = await self.resolve(page, test["fingerprint"])
                    if element:
                        try:
                            if await element.is_enabled():
                                enabled = True
                                break
                        except Exception:
                            pass
                    await page.wait_for_timeout(1000)

                if not enabled:
                    raise AssertionError("Dynamic button did not become enabled")

                await element.click(timeout=ACTION_TIMEOUT)
                print("   ✅ PASS dynamic button")
                self.results.append({**test, "status": "PASS"})

            elif typ == "file_upload":
                if await element.get_attribute("type") != "file":
                    raise AssertionError("Not a file input")
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            elif typ == "date_picker":
                await element.click(timeout=ACTION_TIMEOUT)
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            elif typ == "tab":
                await element.click(timeout=ACTION_TIMEOUT)
                print("   ✅ PASS")
                self.results.append({**test, "status": "PASS"})

            else:
                raise AssertionError(f"Unknown test type: {typ}")

        except Exception as exc:
            await self.fail(page, test, exc)

        finally:
            await self.cleanup(page)

    async def run(self):
        print("\n🗺️ DISCOVER APPLICATION")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()

            self.add_url(self.target, 0)

            while self.queue and len(self.visited) < MAX_PAGES:
                item = self.queue.pop(0)
                url = item["url"]
                depth = item["depth"]

                if url in self.visited:
                    continue

                self.visited.add(url)
                await self.discover(page, url, depth)

            await browser.close()

        print(f"\n📚 Pages discovered: {len(self.pages)}")
        self.generate_tests()

        print("\n▶️ EXECUTE TESTS")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

            async def dialog_handler(dialog):
                print(f"   ⚠ Dialog: {dialog.type}: {dialog.message}")
                self.dialogs.append(
                    {"type": dialog.type, "message": dialog.message}
                )
                try:
                    await dialog.dismiss()
                except Exception:
                    pass

            page.on("dialog", dialog_handler)

            for test in self.tests:
                await self.execute(page, test)

            await browser.close()

        self.report()

    def report(self):
        total = len(self.results)
        passed = sum(x["status"] == "PASS" for x in self.results)
        failed = sum(x["status"] == "FAIL" for x in self.results)

        application_map = REPORT_DIR / "application_map.json"
        test_report = REPORT_DIR / "test_report.json"

        application_map.write_text(
            json.dumps(self.pages, indent=2),
            encoding="utf-8",
        )

        test_report.write_text(
            json.dumps(
                {
                    "agent": "Autonomous QA Agent",
                    "version": "3.4",
                    "target": self.target,
                    "pages_discovered": len(self.pages),
                    "tests_generated": len(self.tests),
                    "tests_executed": total,
                    "pass": passed,
                    "fail": failed,
                    "dialogs_handled": len(self.dialogs),
                    "results": self.results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        print("\n" + "=" * 70)
        print("📊 V3.4 AUTONOMOUS QA REPORT")
        print("=" * 70)
        print(f"Pages discovered : {len(self.pages)}")
        print(f"Tests generated  : {len(self.tests)}")
        print(f"Tests executed   : {total}")
        print(f"PASS             : {passed}")
        print(f"FAIL             : {failed}")
        print(f"Dialogs handled  : {len(self.dialogs)}")

        if failed:
            print("\n🚨 FAILURES")
            for result in self.results:
                if result["status"] == "FAIL":
                    print(f"\n❌ {result['description']}")
                    print(f"URL: {result['url']}")
                    print(f"Reason: {result['error']}")
                    if result.get("evidence"):
                        print(f"Evidence: {result['evidence']}")
        else:
            print("\n✅ No execution failures detected.")

        print(f"\n🗺️ Application map:\n{application_map.absolute()}")
        print(f"\n📄 Test report:\n{test_report.absolute()}")


async def main():
    if len(sys.argv) != 2:
        print('Usage: python3 qa_agent.py "https://demoqa.com"')
        sys.exit(1)

    url = sys.argv[1]

    if not url.startswith(("http://", "https://")):
        print("❌ Invalid URL")
        sys.exit(1)

    agent = QAAgent(url)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
