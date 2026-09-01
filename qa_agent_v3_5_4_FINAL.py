import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright


# ============================================================
# AUTONOMOUS QA AGENT V3.5
# Semantic Understanding + Behavioral Test Generation
# ============================================================

MAX_DEPTH = 3
MAX_PAGES = 30
MAX_LINKS_PER_PAGE = 20

NAV_TIMEOUT = 20000
ACTION_TIMEOUT = 7000
DYNAMIC_WAIT_SECONDS = 15

REPORT_DIR = Path("qa_v3_5_4_report")
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

    # Resolve native HTML semantics before generic tag semantics.
    if typ == "range" or role == "slider":
        return "slider"

    # Some custom slider implementations expose a numeric ARIA value
    # even when the native type is not reported as range.
    if e.get("aria_valuenow") not in (None, ""):
        return "slider"

    if typ == "file":
        return "file_upload"

    if typ == "date":
        return "date_picker"

    if typ == "checkbox" or role == "checkbox":
        return "checkbox"

    if typ == "radio" or role == "radio":
        return "radio"

    if role == "combobox":
        return "combobox"

    if role == "tab":
        return "tab"

    if role == "button" or tag == "button":
        return "button"

    if tag == "textarea":
        return "text_area"

    if tag == "select":
        return "dropdown"

    # Only ordinary text-like inputs are text_input.
    if tag == "input":
        text_types = {
            "",
            "text",
            "email",
            "tel",
            "search",
            "url",
            "password",
        }

        if typ in text_types:
            return "text_input"

        return "unknown"

    return "unknown"


def is_explicitly_dynamic_button(e):
    label = clean(
        e.get("text")
        or e.get("aria_label")
        or e.get("title")
        or ""
    ).lower()

    dynamic_phrases = (
        "will enable",
        "enable after",
        "enabled after",
        "becomes enabled",
        "become enabled",
        "wait for",
        "dynamic",
    )

    return any(
        phrase in label
        for phrase in dynamic_phrases
    )


def is_pagination_button(e):
    label = clean(
        e.get("text")
        or e.get("aria_label")
        or e.get("title")
        or ""
    ).lower()

    pagination = {
        "first",
        "previous",
        "prev",
        "next",
        "last",
        "<<",
        ">>",
        "<",
        ">",
    }

    return label in pagination


class QAAgent:

    def __init__(self, target):
        self.target = normalize_url(target)

        self.queue = []
        self.visited = set()

        self.pages = []
        self.tests = []
        self.results = []
        self.dialogs = []

        self.stats = {
            "expected_skips": 0,
            "dynamic_tests": 0,
            "behavioral_tests": 0,
        }

    # ========================================================
    # DISCOVERY QUEUE
    # ========================================================

    def add_url(self, url, depth):

        if not url:
            return

        url = normalize_url(url)

        if not url.startswith(
            ("http://", "https://")
        ):
            return

        if not same_domain(
            self.target,
            url
        ):
            return

        if depth > MAX_DEPTH:
            return

        if url in self.visited:
            return

        if any(
            x["url"] == url
            for x in self.queue
        ):
            return

        self.queue.append(
            {
                "url": url,
                "depth": depth,
            }
        )

    # ========================================================
    # PAGE DISCOVERY
    # ========================================================

    async def discover(
        self,
        page,
        url,
        depth,
    ):

        print(
            f"\n🌐 [{depth}] {url}"
        )

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )

            await page.wait_for_timeout(
                500
            )

        except Exception as exc:

            print(
                f"   ❌ Navigation failed: {exc}"
            )

            return

        title = await page.title()

        raw_elements = await page.locator(
            """
            a,
            button,
            input,
            textarea,
            select,
            [role],
            [aria-label]
            """
        ).evaluate_all(
            """
            els => els.map((e, i) => {

                const r = e.getBoundingClientRect();
                const s = getComputedStyle(e);

                return {
                    index: i,

                    tag:
                        e.tagName.toLowerCase(),

                    text:
                        (e.innerText || "").trim(),

                    role:
                        e.getAttribute("role") || "",

                    aria_label:
                        e.getAttribute("aria-label") || "",

                    placeholder:
                        e.getAttribute("placeholder") || "",

                    input_type:
                        e.getAttribute("type") || "",

                    name:
                        e.getAttribute("name") || "",

                    id:
                        e.getAttribute("id") || "",

                    title:
                        e.getAttribute("title") || "",

                    aria_valuenow:
                        e.getAttribute("aria-valuenow"),

                    data_testid:
                        e.getAttribute("data-testid") || "",

                    class_name:
                        e.className || "",

                    disabled:
                        !!e.disabled,

                    readonly:
                        !!e.readOnly,

                    required:
                        !!e.required,

                    maxlength:
                        e.getAttribute("maxlength") || "",

                    visible:
                        r.width > 0 &&
                        r.height > 0 &&
                        s.display !== "none" &&
                        s.visibility !== "hidden" &&
                        s.opacity !== "0",

                    aria_hidden:
                        e.getAttribute("aria-hidden")
                        === "true"
                };
            })
            """
        )

        usable = []

        for e in raw_elements:

            if not e["visible"]:
                continue

            if e["aria_hidden"]:
                continue

            e["semantic"] = classify(e)

            if e["semantic"] != "unknown":
                usable.append(e)

        links = await page.locator(
            "a[href]"
        ).evaluate_all(
            """
            els => els.map(a => ({
                text: (a.innerText || "").trim(),
                href: a.href
            }))
            """
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

        print(
            f"   📄 {title}"
        )

        print(
            f"   🧩 Elements discovered: "
            f"{len(usable)}"
        )

        counts = {}

        for e in usable:

            semantic = e["semantic"]

            counts[semantic] = (
                counts.get(
                    semantic,
                    0
                ) + 1
            )

        for semantic in sorted(counts):

            print(
                f"      {semantic}: "
                f"{counts[semantic]}"
            )

        added = 0

        for link in links:

            href = link.get("href")

            if not href:
                continue

            absolute = normalize_url(
                urljoin(
                    url,
                    href
                )
            )

            before = len(
                self.queue
            )

            self.add_url(
                absolute,
                depth + 1
            )

            if len(self.queue) > before:
                added += 1

            if added >= MAX_LINKS_PER_PAGE:
                break

    # ========================================================
    # FINGERPRINT
    # ========================================================

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
            "aria_valuenow": clean(e.get("aria_valuenow")),
            "data_testid": clean(e.get("data_testid")),
            "class_name": clean(e.get("class_name")),
        }


    # ========================================================
    # RESOLVE SEMANTIC ELEMENT
    # ========================================================

    async def resolve(self, page, fp):
        """
        Resolve the original semantic element using deterministic identity.

        Priority:
          1. id
          2. data-testid
          3. name
          4. exact accessible label/text
          5. structural semantic query

        A candidate is accepted only after validating its native tag/type/role.
        Duplicate selector paths pointing to the same DOM node are harmless.
        """

        semantic = fp.get("semantic", "")
        element_id = clean(fp.get("id"))
        data_testid = clean(fp.get("data_testid"))
        name = clean(fp.get("name"))
        aria = clean(fp.get("aria_label"))
        placeholder = clean(fp.get("placeholder"))
        text = clean(fp.get("text"))
        expected_tag = clean(fp.get("tag")).lower()
        expected_type = clean(fp.get("input_type")).lower()

        locators = []

        if element_id:
            locators.append(
                page.locator(f'#{element_id}')
            )

        if data_testid:
            locators.append(
                page.locator(
                    f'[data-testid="{data_testid}"]'
                )
            )

        if name and semantic in {
            "text_input",
            "text_area",
            "combobox",
        }:
            if expected_tag == "textarea":
                locators.append(
                    page.locator(
                        f'textarea[name="{name}"]'
                    )
                )
            else:
                locators.append(
                    page.locator(
                        f'input[name="{name}"]'
                    )
                )

        if semantic == "button":
            if text:
                locators.append(
                    page.get_by_role(
                        "button",
                        name=text,
                        exact=True
                    )
                )
            if aria:
                locators.append(
                    page.get_by_role(
                        "button",
                        name=aria,
                        exact=True
                    )
                )

        elif semantic == "tab":
            if text:
                locators.append(
                    page.get_by_role(
                        "tab",
                        name=text,
                        exact=True
                    )
                )

        elif semantic == "checkbox":
            if aria:
                locators.append(
                    page.get_by_label(
                        aria,
                        exact=True
                    )
                )
            elif text:
                locators.append(
                    page.get_by_label(
                        text,
                        exact=True
                    )
                )

        elif semantic == "radio":
            if aria:
                locators.append(
                    page.get_by_label(
                        aria,
                        exact=True
                    )
                )
            elif text:
                locators.append(
                    page.get_by_label(
                        text,
                        exact=True
                    )
                )

        elif semantic == "combobox":
            if aria:
                locators.append(
                    page.get_by_role(
                        "combobox",
                        name=aria,
                        exact=True
                    )
                )

        elif semantic == "text_input":
            if aria:
                locators.append(
                    page.get_by_label(
                        aria,
                        exact=True
                    )
                )
            if placeholder:
                locators.append(
                    page.get_by_placeholder(
                        placeholder,
                        exact=True
                    )
                )

        elif semantic == "text_area":
            if aria:
                locators.append(
                    page.get_by_label(
                        aria,
                        exact=True
                    )
                )
            if placeholder:
                locators.append(
                    page.get_by_placeholder(
                        placeholder,
                        exact=True
                    )
                )

        # Native semantic fallbacks are narrow and type-safe.
        if semantic == "slider":
            locators.append(
                page.locator(
                    'input[type="range"]:visible'
                )
            )
            locators.append(
                page.locator(
                    '[role="slider"]:visible'
                )
            )

        elif semantic == "file_upload":
            locators.append(
                page.locator(
                    'input[type="file"]:visible'
                )
            )

        elif semantic == "date_picker":
            locators.append(
                page.locator(
                    'input[type="date"]:visible'
                )
            )

        elif semantic == "dropdown":
            locators.append(
                page.locator(
                    "select:visible"
                )
            )

        # ----------------------------------------------------
        # Resolve and validate.
        # ----------------------------------------------------

        seen_handles = set()

        for locator in locators:
            try:
                count = await locator.count()

                for i in range(count):
                    item = locator.nth(i)

                    if not await item.is_visible():
                        continue

                    # Obtain a stable DOM identity for this runtime node.
                    try:
                        dom_key = await item.evaluate(
                            """
                            e => {
                                if (!e.__qaAgentId) {
                                    e.__qaAgentId =
                                      Math.random().toString(36).slice(2);
                                }
                                return e.__qaAgentId;
                            }
                            """
                        )
                    except Exception:
                        dom_key = f"{id(item)}"

                    if dom_key in seen_handles:
                        continue

                    seen_handles.add(dom_key)

                    actual_tag = (
                        await item.evaluate(
                            "e => e.tagName.toLowerCase()"
                        )
                    )

                    actual_type = (
                        await item.get_attribute("type")
                        or ""
                    ).strip().lower()

                    actual_role = (
                        await item.get_attribute("role")
                        or ""
                    ).strip().lower()

                    # Strict semantic validation.
                    if semantic == "text_input":
                        allowed = {
                            "",
                            "text",
                            "email",
                            "tel",
                            "search",
                            "url",
                            "password",
                        }

                        if actual_tag != "input":
                            continue

                        if actual_type not in allowed:
                            continue

                        if actual_role == "slider":
                            continue

                    elif semantic == "slider":
                        if not (
                            actual_type == "range"
                            or actual_role == "slider"
                        ):
                            continue

                    elif semantic == "text_area":
                        if actual_tag != "textarea":
                            continue

                    elif semantic == "button":
                        if not (
                            actual_tag == "button"
                            or actual_role == "button"
                        ):
                            continue

                    elif semantic == "checkbox":
                        if not (
                            actual_type == "checkbox"
                            or actual_role == "checkbox"
                        ):
                            continue

                    elif semantic == "radio":
                        if not (
                            actual_type == "radio"
                            or actual_role == "radio"
                        ):
                            continue

                    elif semantic == "tab":
                        if actual_role != "tab":
                            continue

                    elif semantic == "combobox":
                        if actual_role != "combobox":
                            continue

                    elif semantic == "file_upload":
                        if actual_type != "file":
                            continue

                    elif semantic == "date_picker":
                        if actual_type != "date":
                            continue

                    elif semantic == "dropdown":
                        if actual_tag != "select":
                            continue

                    return item

            except Exception:
                continue

        return None


    # ========================================================
    # BEHAVIORAL TEST GENERATION
    # ========================================================

    def generate_tests(self):

        print(
            "\n🧠 GENERATE BEHAVIORAL TESTS"
        )

        seen = set()

        for p in self.pages:

            url = p["url"]

            # ------------------------------------------------
            # PAGE LOAD
            # ------------------------------------------------

            key = (
                "page_load",
                url
            )

            if key not in seen:

                self.tests.append(
                    {
                        "type":
                            "page_load",

                        "url":
                            url,

                        "fingerprint":
                            None,

                        "description":
                            "Page should load successfully",
                    }
                )

                seen.add(key)

            # ------------------------------------------------
            # ELEMENTS
            # ------------------------------------------------

            for e in p["elements"]:

                semantic = e["semantic"]

                if not e["visible"]:
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

                # ============================================
                # TEXT INPUT
                # ============================================

                if semantic == "text_input":

                    input_type = (
                        e.get("input_type") or ""
                    ).strip().lower()

                    if (
                        input_type == "range"
                        or clean(e.get("role")).lower() == "slider"
                        or clean(e.get("aria_valuenow")) != ""
                    ):
                        continue

                    # Defensive guard: a special input must NEVER
                    # become a text-input test, even if classification
                    # is changed by a future discovery enhancement.
                    SPECIAL_INPUT_TYPES = {
                        "hidden",
                        "range",
                        "file",
                        "checkbox",
                        "radio",
                        "date",
                        "datetime-local",
                        "time",
                        "month",
                        "week",
                        "color",
                        "number",
                        "button",
                        "submit",
                        "reset",
                        "image",
                    }

                    if input_type in SPECIAL_INPUT_TYPES:
                        continue

                    if e.get("disabled"):
                        continue

                    if e.get("readonly"):
                        continue

                    test = (
                        "text_input",
                        f"Enter valid data into {label}",
                    )

                # ============================================
                # TEXT AREA
                # ============================================

                elif semantic == "text_area":

                    if e["disabled"] or e["readonly"]:
                        continue

                    test = (
                        "text_area",
                        f"Enter text into {label}",
                    )

                # ============================================
                # CHECKBOX
                # ============================================

                elif semantic == "checkbox":

                    if e["disabled"]:
                        continue

                    test = (
                        "checkbox",
                        f"Toggle {label}",
                    )

                # ============================================
                # RADIO
                # ============================================

                elif semantic == "radio":

                    if e["disabled"]:
                        continue

                    test = (
                        "radio",
                        f"Select {label}",
                    )

                # ============================================
                # SLIDER
                # ============================================

                elif semantic == "slider":

                    if e["disabled"]:
                        continue

                    test = (
                        "slider",
                        f"Change {label}",
                    )

                # ============================================
                # DROPDOWN
                # ============================================

                elif semantic == "dropdown":

                    if e["disabled"]:
                        continue

                    test = (
                        "dropdown",
                        f"Test dropdown {label}",
                    )

                # ============================================
                # COMBOBOX
                # ============================================

                elif semantic == "combobox":

                    if e["disabled"]:
                        continue

                    test = (
                        "combobox",
                        f"Test combobox {label}",
                    )

                # ============================================
                # FILE UPLOAD
                # ============================================

                elif semantic == "file_upload":

                    test = (
                        "file_upload",
                        f"Verify upload control {label}",
                    )

                # ============================================
                # DATE PICKER
                # ============================================

                elif semantic == "date_picker":

                    if e["disabled"]:
                        continue

                    test = (
                        "date_picker",
                        f"Interact with {label}",
                    )

                # ============================================
                # TABS
                # ============================================

                elif semantic == "tab":

                    if e["disabled"]:
                        continue

                    test = (
                        "tab",
                        f"Activate tab {label}",
                    )

                # ============================================
                # BUTTONS
                # ============================================

                elif semantic == "button":

                    # Disabled controls need behavioral
                    # reasoning instead of being assumed
                    # dynamic.

                    if e["disabled"]:

                        if is_explicitly_dynamic_button(e):

                            test = (
                                "dynamic_button",
                                f"Wait for dynamic button {label}",
                            )

                            self.stats[
                                "dynamic_tests"
                            ] += 1

                        elif is_pagination_button(e):

                            # Correctly disabled pagination is
                            # an expected state.
                            self.stats[
                                "expected_skips"
                            ] += 1

                            continue

                        else:

                            # Disabled ordinary buttons are not
                            # automatically failures.
                            self.stats[
                                "expected_skips"
                            ] += 1

                            continue

                    else:

                        test = (
                            "button",
                            f"Activate {label}",
                        )

                if not test:
                    continue

                typ, description = test

                key = (
                    typ,
                    url,
                    json.dumps(
                        fp,
                        sort_keys=True,
                    )
                )

                if key in seen:
                    continue

                seen.add(key)

                self.tests.append(
                    {
                        "type":
                            typ,

                        "url":
                            url,

                        "fingerprint":
                            fp,

                        "description":
                            description,
                    }
                )

                self.stats[
                    "behavioral_tests"
                ] += 1

        print(
            f"   ✓ Generated: "
            f"{len(self.tests)}"
        )

        print(
            f"   ⏭ Expected disabled skips: "
            f"{self.stats['expected_skips']}"
        )

        print(
            f"   ⏳ Dynamic tests: "
            f"{self.stats['dynamic_tests']}"
        )

    # ========================================================
    # CLEANUP
    # ========================================================

    async def cleanup(self, page):

        print(
            "   🧹 CLEANUP"
        )

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
            '.modal.show:visible button:has-text("Cancel")',
        ]

        for selector in selectors:

            try:

                locator = page.locator(
                    selector
                )

                count = await locator.count()

                for i in range(count):

                    item = locator.nth(i)

                    try:

                        if await item.is_visible():

                            await item.click(
                                timeout=1500
                            )

                            await page.wait_for_timeout(
                                200
                            )

                    except Exception:
                        pass

            except Exception:
                pass

        try:
            await page.keyboard.press(
                "Escape"
            )

            await page.wait_for_timeout(
                200
            )

        except Exception:
            pass

        print(
            "   ✓ UI clean"
        )

    # ========================================================
    # RECORD FAILURE
    # ========================================================

    async def fail(
        self,
        page,
        test,
        error,
    ):

        filename = (
            f"{slug(urlparse(test['url']).path)}_"
            f"{slug(test['type'])}_"
            f"{len(self.results)}.png"
        )

        screenshot = (
            SCREENSHOT_DIR /
            filename
        )

        try:

            await page.screenshot(
                path=str(screenshot),
                full_page=True,
            )

        except Exception:

            screenshot = None

        result = {
            **test,

            "status":
                "FAIL",

            "error":
                str(error),

            "evidence":
                str(screenshot)
                if screenshot
                else None,
        }

        self.results.append(
            result
        )

        print(
            f"   ❌ FAIL "
            f"{test['description']}"
        )

        print(
            f"      Reason: {error}"
        )

        if screenshot:

            print(
                f"      Evidence: "
                f"{screenshot}"
            )

    # ========================================================
    # EXECUTE TEST
    # ========================================================

    async def execute(
        self,
        page,
        test,
    ):

        print(
            f"\n🧪 {test['description']}"
        )

        try:

            await page.goto(
                test["url"],
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )

            await page.wait_for_timeout(
                300
            )

            typ = test["type"]

            # ================================================
            # PAGE LOAD
            # ================================================

            if typ == "page_load":

                await page.locator(
                    "body"
                ).wait_for(
                    state="visible",
                    timeout=5000,
                )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

                return

            # ================================================
            # DYNAMIC BUTTON
            # ================================================

            if typ == "dynamic_button":

                deadline = (
                    asyncio.get_running_loop().time()
                    +
                    DYNAMIC_WAIT_SECONDS
                )

                element = None

                while (
                    asyncio.get_running_loop().time()
                    <
                    deadline
                ):

                    element = await self.resolve(
                        page,
                        test["fingerprint"],
                    )

                    if element:

                        try:

                            if await element.is_enabled():

                                await element.click(
                                    timeout=ACTION_TIMEOUT
                                )

                                self.results.append(
                                    {
                                        **test,
                                        "status":
                                            "PASS",
                                        "message":
                                            "Dynamic control became enabled",
                                    }
                                )

                                print(
                                    "   ✅ PASS dynamic button"
                                )

                                return

                        except Exception:
                            pass

                    await page.wait_for_timeout(
                        1000
                    )

                raise AssertionError(
                    "Explicitly dynamic button "
                    "did not become enabled"
                )

            # ================================================
            # NORMAL ELEMENT
            # ================================================

            element = await self.resolve(
                page,
                test["fingerprint"],
            )

            if element is None:

                fp = test.get("fingerprint") or {}

                raise AssertionError(
                    "Semantic element could not be resolved uniquely "
                    f"(semantic={fp.get('semantic')!r}, "
                    f"id={fp.get('id')!r}, "
                    f"label={fp.get('aria_label') or fp.get('text')!r}, "
                    f"type={fp.get('input_type')!r})"
                )

            # Final safety check: a text-input test must never act
            # on a slider/range or another non-text native control.
            if typ == "text_input":
                native_type = (
                    await element.get_attribute("type")
                    or ""
                ).strip().lower()

                role = (
                    await element.get_attribute("role")
                    or ""
                ).strip().lower()

                if native_type in {
                    "range",
                    "file",
                    "checkbox",
                    "radio",
                    "date",
                    "number",
                    "color",
                    "hidden",
                    "button",
                    "submit",
                    "reset",
                } or role == "slider":

                    raise AssertionError(
                        "Safety guard blocked non-text control "
                        f"from text-input test: "
                        f"type={native_type!r}, role={role!r}"
                    )

            # ================================================
            # TEXT INPUT
            # ================================================

            if typ == "text_input":

                await element.fill(
                    "QA_AGENT_TEST",
                    timeout=ACTION_TIMEOUT,
                )

                expected = "QA_AGENT_TEST"

                maxlength = await element.get_attribute(
                    "maxlength"
                )

                if maxlength and maxlength.isdigit():

                    expected = expected[
                        :int(maxlength)
                    ]

                actual = await element.input_value()

                if actual != expected:

                    raise AssertionError(
                        f"Expected '{expected}', "
                        f"got '{actual}'"
                    )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                        "message":
                            "Value persisted",
                    }
                )

                print(
                    "   ✅ PASS value persisted"
                )

            # ================================================
            # TEXT AREA
            # ================================================

            elif typ == "text_area":

                value = (
                    "Autonomous QA Agent"
                )

                await element.fill(
                    value,
                    timeout=ACTION_TIMEOUT,
                )

                actual = await element.input_value()

                if actual != value:

                    raise AssertionError(
                        f"Expected '{value}', "
                        f"got '{actual}'"
                    )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # CHECKBOX
            # ================================================

            elif typ == "checkbox":

                await element.check(
                    timeout=ACTION_TIMEOUT
                )

                if not await element.is_checked():

                    raise AssertionError(
                        "Checkbox did not check"
                    )

                await element.uncheck(
                    timeout=ACTION_TIMEOUT
                )

                if await element.is_checked():

                    raise AssertionError(
                        "Checkbox did not uncheck"
                    )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # RADIO
            # ================================================

            elif typ == "radio":

                await element.check(
                    timeout=ACTION_TIMEOUT
                )

                if not await element.is_checked():

                    raise AssertionError(
                        "Radio did not select"
                    )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # SLIDER
            # ================================================

            elif typ == "slider":

                before = await element.input_value()

                await element.focus()

                await page.keyboard.press(
                    "ArrowRight"
                )

                await page.wait_for_timeout(
                    200
                )

                after = await element.input_value()

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                        "message":
                            f"Slider {before} -> {after}",
                    }
                )

                print(
                    f"   ✅ PASS slider "
                    f"{before} -> {after}"
                )

            # ================================================
            # DROPDOWN
            # ================================================

            elif typ == "dropdown":

                count = await element.locator(
                    "option"
                ).count()

                if count == 0:

                    raise AssertionError(
                        "Dropdown has no options"
                    )

                if count > 1:

                    await element.select_option(
                        index=1
                    )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    f"   ✅ PASS "
                    f"{count} options"
                )

            # ================================================
            # COMBOBOX
            # ================================================

            elif typ == "combobox":

                await element.click(
                    timeout=ACTION_TIMEOUT
                )

                await page.wait_for_timeout(
                    300
                )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                        "message":
                            "Combobox activated",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # BUTTON
            # ================================================

            elif typ == "button":

                if await element.is_disabled():

                    raise AssertionError(
                        "Button unexpectedly disabled"
                    )

                await element.click(
                    timeout=ACTION_TIMEOUT
                )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # FILE UPLOAD
            # ================================================

            elif typ == "file_upload":

                actual_type = await element.get_attribute(
                    "type"
                )

                if actual_type != "file":

                    raise AssertionError(
                        "Not a file input"
                    )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # DATE PICKER
            # ================================================

            elif typ == "date_picker":

                await element.click(
                    timeout=ACTION_TIMEOUT
                )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            # ================================================
            # TAB
            # ================================================

            elif typ == "tab":

                await element.click(
                    timeout=ACTION_TIMEOUT
                )

                self.results.append(
                    {
                        **test,
                        "status": "PASS",
                    }
                )

                print(
                    "   ✅ PASS"
                )

            else:

                raise AssertionError(
                    f"Unknown test type: {typ}"
                )

        except Exception as exc:

            await self.fail(
                page,
                test,
                exc,
            )

        finally:

            await self.cleanup(
                page
            )

    # ========================================================
    # RUN
    # ========================================================

    async def run(self):

        print(
            "\n🤖 AUTONOMOUS QA AGENT V3.5 FINAL"
        )

        print(
            f"🎯 TARGET: {self.target}"
        )

        print(
            "\n🗺️ DISCOVER APPLICATION"
        )

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=False
            )

            page = await browser.new_page()

            self.add_url(
                self.target,
                0
            )

            while (
                self.queue
                and
                len(self.visited) < MAX_PAGES
            ):

                item = self.queue.pop(
                    0
                )

                url = item["url"]
                depth = item["depth"]

                if url in self.visited:
                    continue

                self.visited.add(
                    url
                )

                await self.discover(
                    page,
                    url,
                    depth
                )

            await browser.close()

        print(
            f"\n📚 Pages discovered: "
            f"{len(self.pages)}"
        )

        self.generate_tests()

        print(
            "\n▶️ EXECUTE TESTS"
        )

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=False
            )

            context = await browser.new_context()

            page = await context.new_page()

            async def dialog_handler(dialog):

                print(
                    f"   ⚠ Dialog: "
                    f"{dialog.type}: "
                    f"{dialog.message}"
                )

                self.dialogs.append(
                    {
                        "type":
                            dialog.type,

                        "message":
                            dialog.message,
                    }
                )

                try:
                    await dialog.dismiss()
                except Exception:
                    pass

            page.on(
                "dialog",
                dialog_handler
            )

            for test in self.tests:

                await self.execute(
                    page,
                    test
                )

            await browser.close()

        self.report()

    # ========================================================
    # REPORT
    # ========================================================

    def report(self):

        total = len(
            self.results
        )

        passed = sum(
            x["status"] == "PASS"
            for x in self.results
        )

        failed = sum(
            x["status"] == "FAIL"
            for x in self.results
        )

        application_map = (
            REPORT_DIR /
            "application_map.json"
        )

        test_report = (
            REPORT_DIR /
            "test_report.json"
        )

        application_map.write_text(
            json.dumps(
                self.pages,
                indent=2
            ),
            encoding="utf-8"
        )

        test_report.write_text(
            json.dumps(
                {
                    "agent":
                        "Autonomous QA Agent",

                    "version":
                        "3.5.4",

                    "target":
                        self.target,

                    "pages_discovered":
                        len(self.pages),

                    "tests_generated":
                        len(self.tests),

                    "tests_executed":
                        total,

                    "pass":
                        passed,

                    "fail":
                        failed,

                    "dialogs_handled":
                        len(self.dialogs),

                    "generation_stats":
                        self.stats,

                    "results":
                        self.results,
                },
                indent=2
            ),
            encoding="utf-8"
        )

        print(
            "\n"
            + "=" * 70
        )

        print(
            "📊 V3.5.4 AUTONOMOUS QA REPORT"
        )

        print(
            "=" * 70
        )

        print(
            f"Pages discovered : "
            f"{len(self.pages)}"
        )

        print(
            f"Tests generated  : "
            f"{len(self.tests)}"
        )

        print(
            f"Tests executed   : "
            f"{total}"
        )

        print(
            f"PASS             : "
            f"{passed}"
        )

        print(
            f"FAIL             : "
            f"{failed}"
        )

        print(
            f"Dialogs handled  : "
            f"{len(self.dialogs)}"
        )

        print(
            f"Expected skips   : "
            f"{self.stats['expected_skips']}"
        )

        if failed:

            print(
                "\n🚨 FAILURES"
            )

            for result in self.results:

                if result["status"] != "FAIL":
                    continue

                print(
                    f"\n❌ "
                    f"{result['description']}"
                )

                print(
                    f"URL: "
                    f"{result['url']}"
                )

                print(
                    f"Reason: "
                    f"{result['error']}"
                )

                if result.get("evidence"):

                    print(
                        f"Evidence: "
                        f"{result['evidence']}"
                    )

        else:

            print(
                "\n✅ No execution failures detected."
            )

        print(
            "\n🗺️ Application map:"
        )

        print(
            application_map.absolute()
        )

        print(
            "\n📄 Test report:"
        )

        print(
            test_report.absolute()
        )


async def main():
    print("\n*** RUNNING qa_agent_v3_5_1_FINAL.py — VERSION 3.5.4 FINAL ***\n")
    print("\n*** RUNNING qa_agent_v3_5_1_FINAL.py — VERSION 3.5.4 FINAL ***\n")


    if len(sys.argv) != 2:

        print(
            'Usage: python3 qa_agent_v3_5.py '
            '"https://demoqa.com"'
        )

        sys.exit(1)

    target = sys.argv[1]

    if not target.startswith(
        ("http://", "https://")
    ):

        print(
            "❌ Invalid URL"
        )

        sys.exit(1)

    agent = QAAgent(
        target
    )

    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
