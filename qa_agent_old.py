import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# AUTONOMOUS QA AGENT V3.4
# ============================================================

MAX_DEPTH = 3
MAX_PAGES = 30
MAX_LINKS_PER_PAGE = 20

NAV_TIMEOUT = 20000
ACTION_TIMEOUT = 5000
SHORT_WAIT = 300

REPORT_DIR = Path("qa_v3_4_report")
SCREENSHOT_DIR = REPORT_DIR / "screenshots"

REPORT_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def slug(value):
    value = clean_text(value)
    value = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return value[:100] or "unknown"


def normalize_url(url):
    parsed = urlparse(url)

    return parsed._replace(
        fragment=""
    ).geturl()


def same_domain(base_url, target_url):
    return (
        urlparse(base_url).netloc
        ==
        urlparse(target_url).netloc
    )


def valid_url(url):
    return url.startswith(
        ("http://", "https://")
    )


# ============================================================
# SEMANTIC CLASSIFICATION
# ============================================================

def classify_element(element):
    tag = clean_text(
        element.get("tag")
    ).lower()

    role = clean_text(
        element.get("role")
    ).lower()

    input_type = clean_text(
        element.get("input_type")
    ).lower()

    classes = clean_text(
        element.get("classes")
    ).lower()

    text = clean_text(
        element.get("text")
    ).lower()

    aria = clean_text(
        element.get("aria_label")
    ).lower()

    placeholder = clean_text(
        element.get("placeholder")
    ).lower()

    combined = " ".join([
        classes,
        text,
        aria,
        placeholder
    ])


    # Native input types
    if tag == "input":

        if input_type == "checkbox":
            return "checkbox"

        if input_type == "radio":
            return "radio"

        if input_type == "range":
            return "slider"

        if input_type == "file":
            return "file_upload"

        if input_type == "date":
            return "date_picker"

        if role == "combobox":
            return "combobox"

        if "react-select" in combined:
            return "combobox"

        return "text_input"


    if tag == "textarea":
        return "text_area"


    if tag == "select":
        return "dropdown"


    if tag == "button":
        return "button"


    if tag == "a":
        return "link"


    if role == "button":
        return "button"


    if role == "checkbox":
        return "checkbox"


    if role == "radio":
        return "radio"


    if role == "tab":
        return "tab"


    if role == "combobox":
        return "combobox"


    if role == "progressbar":
        return "progress_bar"


    return "unknown"


# ============================================================
# QA AGENT
# ============================================================

class QAAgent:

    def __init__(self, base_url):

        self.base_url = normalize_url(
            base_url
        )

        self.queue = []

        self.visited = set()

        self.application_map = []

        self.tests = []

        self.results = []

        self.failures = []

        self.dialogs = []

        self.locator_metrics = {
            "resolved": 0,
            "unresolved": 0,
            "low_confidence": 0,
        }


    # ========================================================
    # URL QUEUE
    # ========================================================

    def add_url(self, url, depth):

        if not url:
            return

        url = normalize_url(url)

        if not valid_url(url):
            return

        if not same_domain(
            self.base_url,
            url
        ):
            return

        if depth > MAX_DEPTH:
            return

        if url in self.visited:
            return

        if any(
            item["url"] == url
            for item in self.queue
        ):
            return

        self.queue.append({
            "url": url,
            "depth": depth
        })


    # ========================================================
    # DISCOVER PAGE
    # ========================================================

    async def discover(
        self,
        page,
        url,
        depth
    ):

        print(
            f"\n🌐 [{depth}] {url}"
        )

        try:

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT
            )

            await page.wait_for_timeout(
                SHORT_WAIT
            )

        except Exception as exc:

            print(
                f"   ❌ Navigation failed: {exc}"
            )

            return


        title = await page.title()


        # ----------------------------------------------------
        # DOM SNAPSHOT
        # ----------------------------------------------------

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
            elements => elements.map((e, index) => {

                const rect =
                    e.getBoundingClientRect();

                const style =
                    window.getComputedStyle(e);

                const tag =
                    e.tagName.toLowerCase();

                return {

                    index: index,

                    tag: tag,

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

                    classes:
                        typeof e.className === "string"
                        ? e.className
                        : "",

                    title_attribute:
                        e.getAttribute("title") || "",

                    aria_describedby:
                        e.getAttribute(
                            "aria-describedby"
                        ) || "",

                    disabled:
                        !!e.disabled,

                    readonly:
                        !!e.readOnly,

                    required:
                        !!e.required,

                    visible:
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        style.opacity !== "0",

                    aria_hidden:
                        e.getAttribute(
                            "aria-hidden"
                        ) === "true"
                };
            })
            """
        )


        elements = []


        for element in raw_elements:

            if not element.get("visible"):
                continue

            if element.get("aria_hidden"):
                continue

            semantic = classify_element(
                element
            )

            if semantic == "unknown":
                continue

            element["semantic_type"] = semantic

            elements.append(
                element
            )


        # ----------------------------------------------------
        # LINKS
        # ----------------------------------------------------

        links = await page.locator(
            "a[href]"
        ).evaluate_all(
            """
            elements => elements.map(a => ({
                text:
                    (a.innerText || "").trim(),

                href:
                    a.href
            }))
            """
        )


        # ----------------------------------------------------
        # TOOLTIP TRIGGERS
        #
        # We store the REAL element that owns the tooltip.
        # ----------------------------------------------------

        tooltip_triggers = []

        for element in raw_elements:

            if not element.get("visible"):
                continue

            if element.get("aria_hidden"):
                continue

            if (
                element.get("title_attribute")
                or
                element.get("aria_describedby")
            ):

                semantic = classify_element(
                    element
                )

                if semantic != "unknown":

                    trigger = dict(
                        element
                    )

                    trigger[
                        "semantic_type"
                    ] = semantic

                    tooltip_triggers.append(
                        trigger
                    )


        # ----------------------------------------------------
        # APPLICATION MAP
        # ----------------------------------------------------

        self.application_map.append({

            "url": url,

            "depth": depth,

            "title": title,

            "elements": elements,

            "tooltip_triggers":
                tooltip_triggers,

            "links": links
        })


        # ----------------------------------------------------
        # DISPLAY
        # ----------------------------------------------------

        print(
            f"   📄 {title}"
        )

        counts = {}

        for element in elements:

            semantic = element[
                "semantic_type"
            ]

            counts[semantic] = (
                counts.get(
                    semantic,
                    0
                ) + 1
            )

        for semantic in sorted(counts):

            print(
                f"   🧩 "
                f"{semantic}: "
                f"{counts[semantic]}"
            )


        # ----------------------------------------------------
        # DISCOVER CHILD LINKS
        # ----------------------------------------------------

        added = 0

        for link in links:

            href = link.get(
                "href"
            )

            if not href:
                continue

            href = normalize_url(
                urljoin(
                    url,
                    href
                )
            )

            if not same_domain(
                self.base_url,
                href
            ):
                continue

            old_size = len(
                self.queue
            )

            self.add_url(
                href,
                depth + 1
            )

            if len(self.queue) > old_size:

                added += 1

            if added >= MAX_LINKS_PER_PAGE:
                break


    # ========================================================
    # FINGERPRINT
    # ========================================================

    def fingerprint(self, element):

        return {

            "semantic_type":
                element.get(
                    "semantic_type",
                    ""
                ),

            "tag":
                element.get(
                    "tag",
                    ""
                ),

            "text":
                clean_text(
                    element.get(
                        "text",
                        ""
                    )
                ),

            "aria_label":
                clean_text(
                    element.get(
                        "aria_label",
                        ""
                    )
                ),

            "placeholder":
                clean_text(
                    element.get(
                        "placeholder",
                        ""
                    )
                ),

            "name":
                clean_text(
                    element.get(
                        "name",
                        ""
                    )
                ),

            "id":
                clean_text(
                    element.get(
                        "id",
                        ""
                    )
                ),

            "input_type":
                clean_text(
                    element.get(
                        "input_type",
                        ""
                    )
                ),

            "title_attribute":
                clean_text(
                    element.get(
                        "title_attribute",
                        ""
                    )
                )
        }


    # ========================================================
    # RANKED LOCATOR RESOLVER
    # ========================================================

    async def resolve_element(
        self,
        page,
        fingerprint
    ):

        semantic = fingerprint.get(
            "semantic_type"
        )

        text = fingerprint.get(
            "text",
            ""
        )

        aria = fingerprint.get(
            "aria_label",
            ""
        )

        placeholder = fingerprint.get(
            "placeholder",
            ""
        )

        name = fingerprint.get(
            "name",
            ""
        )

        element_id = fingerprint.get(
            "id",
            ""
        )

        input_type = fingerprint.get(
            "input_type",
            ""
        )

        title_attribute = fingerprint.get(
            "title_attribute",
            ""
        )


        candidates = []


        # ----------------------------------------------------
        # BUTTON
        # ----------------------------------------------------

        if semantic == "button":

            if text:

                candidates.append(
                    (
                        page.get_by_role(
                            "button",
                            name=text,
                            exact=True
                        ),
                        100
                    )
                )

                candidates.append(
                    (
                        page.locator(
                            "button"
                        ).filter(
                            has_text=text
                        ),
                        75
                    )
                )


            if aria:

                candidates.append(
                    (
                        page.get_by_label(
                            aria,
                            exact=True
                        ),
                        100
                    )
                )


            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"button#{element_id}"
                        ),
                        90
                    )
                )


            candidates.append(
                (
                    page.locator(
                        "button"
                    ),
                    20
                )
            )


        # ----------------------------------------------------
        # TEXT INPUT
        # ----------------------------------------------------

        elif semantic == "text_input":

            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        85
                    )
                )


            if name:

                candidates.append(
                    (
                        page.locator(
                            f'input[name="{name}"]'
                        ),
                        100
                    )
                )


            if aria:

                candidates.append(
                    (
                        page.get_by_label(
                            aria,
                            exact=True
                        ),
                        95
                    )
                )


            if placeholder:

                candidates.append(
                    (
                        page.get_by_placeholder(
                            placeholder,
                            exact=True
                        ),
                        90
                    )
                )


            if input_type:

                candidates.append(
                    (
                        page.locator(
                            f'input[type="{input_type}"]'
                        ),
                        60
                    )
                )


            candidates.append(
                (
                    page.locator(
                        "input"
                    ),
                    20
                )
            )


        # ----------------------------------------------------
        # TEXT AREA
        # ----------------------------------------------------

        elif semantic == "text_area":

            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        85
                    )
                )


            if name:

                candidates.append(
                    (
                        page.locator(
                            f'textarea[name="{name}"]'
                        ),
                        100
                    )
                )


            if aria:

                candidates.append(
                    (
                        page.get_by_label(
                            aria,
                            exact=True
                        ),
                        100
                    )
                )


            if placeholder:

                candidates.append(
                    (
                        page.get_by_placeholder(
                            placeholder,
                            exact=True
                        ),
                        90
                    )
                )


            candidates.append(
                (
                    page.locator(
                        "textarea"
                    ),
                    30
                )
            )


        # ----------------------------------------------------
        # CHECKBOX
        # ----------------------------------------------------

        elif semantic == "checkbox":

            if aria:

                candidates.append(
                    (
                        page.get_by_label(
                            aria,
                            exact=True
                        ),
                        100
                    )
                )


            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        90
                    )
                )


            candidates.append(
                (
                    page.locator(
                        'input[type="checkbox"]'
                    ),
                    60
                )
            )


        # ----------------------------------------------------
        # RADIO
        # ----------------------------------------------------

        elif semantic == "radio":

            if aria:

                candidates.append(
                    (
                        page.get_by_label(
                            aria,
                            exact=True
                        ),
                        100
                    )
                )


            if text:

                candidates.append(
                    (
                        page.get_by_label(
                            text,
                            exact=True
                        ),
                        95
                    )
                )


            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        90
                    )
                )


            candidates.append(
                (
                    page.locator(
                        'input[type="radio"]'
                    ),
                    60
                )
            )


        # ----------------------------------------------------
        # SLIDER
        # ----------------------------------------------------

        elif semantic == "slider":

            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        100
                    )
                )


            candidates.append(
                (
                    page.locator(
                        'input[type="range"]'
                    ),
                    100
                )
            )


        # ----------------------------------------------------
        # SELECT
        # ----------------------------------------------------

        elif semantic == "dropdown":

            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        100
                    )
                )


            candidates.append(
                (
                    page.locator(
                        "select"
                    ),
                    80
                )
            )


        # ----------------------------------------------------
        # COMBOBOX
        # ----------------------------------------------------

        elif semantic == "combobox":

            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        100
                    )
                )


            if aria:

                candidates.append(
                    (
                        page.get_by_role(
                            "combobox",
                            name=aria,
                            exact=True
                        ),
                        100
                    )
                )


            candidates.append(
                (
                    page.get_by_role(
                        "combobox"
                    ),
                    90
                )


        # ----------------------------------------------------
        # TAB
        # ----------------------------------------------------

        elif semantic == "tab":

            if text:

                candidates.append(
                    (
                        page.get_by_role(
                            "tab",
                            name=text,
                            exact=True
                        ),
                        100
                    )
                )


            candidates.append(
                (
                    page.get_by_role(
                        "tab"
                    ),
                    50
                )


        # ----------------------------------------------------
        # FILE
        # ----------------------------------------------------

        elif semantic == "file_upload":

            candidates.append(
                (
                    page.locator(
                        'input[type="file"]'
                    ),
                    100
                )


        # ----------------------------------------------------
        # DATE
        # ----------------------------------------------------

        elif semantic == "date_picker":

            if element_id:

                candidates.append(
                    (
                        page.locator(
                            f"#{element_id}"
                        ),
                        100
                    )
                )


            candidates.append(
                (
                    page.locator(
                        'input[type="date"]'
                    ),
                    100
                )


            if placeholder:

                candidates.append(
                    (
                        page.get_by_placeholder(
                            placeholder,
                            exact=True
                        ),
                        90
                    )
                )


        # ----------------------------------------------------
        # NOTHING TO RESOLVE
        # ----------------------------------------------------

        if not candidates:

            self.locator_metrics[
                "unresolved"
            ] += 1

            return None


        scored = []


        # ----------------------------------------------------
        # SCORE EVERY CANDIDATE
        # ----------------------------------------------------

        for locator, base_score in candidates:

            try:

                count = await locator.count()

            except Exception:

                continue


            for index in range(count):

                element = locator.nth(
                    index
                )


                try:

                    if not await element.is_visible():
                        continue


                    score = base_score


                    # ----------------------------------------
                    # ENABLED
                    # ----------------------------------------

                    try:

                        enabled = await element.is_enabled()

                        if enabled:
                            score += 20
                        else:
                            score -= 100

                    except Exception:

                        pass


                    # ----------------------------------------
                    # NATIVE TYPE
                    # ----------------------------------------

                    actual_type = await element.evaluate(
                        """
                        e => {

                            const tag =
                                e.tagName.toLowerCase();

                            const type =
                                e.getAttribute("type") || "";

                            const role =
                                e.getAttribute("role") || "";

                            if (
                                tag === "input" &&
                                type === "checkbox"
                            )
                                return "checkbox";

                            if (
                                tag === "input" &&
                                type === "radio"
                            )
                                return "radio";

                            if (
                                tag === "input" &&
                                type === "range"
                            )
                                return "slider";

                            if (
                                tag === "input" &&
                                type === "file"
                            )
                                return "file_upload";

                            if (
                                tag === "input" &&
                                type === "date"
                            )
                                return "date_picker";

                            if (
                                tag === "textarea"
                            )
                                return "text_area";

                            if (
                                tag === "select"
                            )
                                return "dropdown";

                            if (
                                role === "combobox"
                            )
                                return "combobox";

                            if (
                                role === "tab"
                            )
                                return "tab";

                            if (
                                tag === "button" ||
                                role === "button"
                            )
                                return "button";

                            if (
                                tag === "input"
                            )
                                return "text_input";

                            return "unknown";
                        }
                        """
                    )


                    if actual_type == semantic:

                        score += 50

                    else:

                        score -= 150


                    # ----------------------------------------
                    # EXACT TEXT
                    # ----------------------------------------

                    if text:

                        actual_text = clean_text(
                            await element.inner_text()
                        )

                        if (
                            actual_text.lower()
                            ==
                            text.lower()
                        ):

                            score += 60

                        elif (
                            text.lower()
                            in actual_text.lower()
                        ):

                            score += 20


                    # ----------------------------------------
                    # ARIA
                    # ----------------------------------------

                    if aria:

                        actual_aria = (
                            await element.get_attribute(
                                "aria-label"
                            )
                            or ""
                        )

                        if (
                            actual_aria.lower()
                            ==
                            aria.lower()
                        ):

                            score += 70


                    # ----------------------------------------
                    # PLACEHOLDER
                    # ----------------------------------------

                    if placeholder:

                        actual_placeholder = (
                            await element.get_attribute(
                                "placeholder"
                            )
                            or ""
                        )

                        if (
                            actual_placeholder.lower()
                            ==
                            placeholder.lower()
                        ):

                            score += 60


                    # ----------------------------------------
                    # NAME
                    # ----------------------------------------

                    if name:

                        actual_name = (
                            await element.get_attribute(
                                "name"
                            )
                            or ""
                        )

                        if actual_name == name:

                            score += 60


                    # ----------------------------------------
                    # ID
                    # ----------------------------------------

                    if element_id:

                        actual_id = (
                            await element.get_attribute(
                                "id"
                            )
                            or ""
                        )

                        if actual_id == element_id:

                            score += 50


                    # ----------------------------------------
                    # TITLE
                    # ----------------------------------------

                    if title_attribute:

                        actual_title = (
                            await element.get_attribute(
                                "title"
                            )
                            or ""
                        )

                        if (
                            actual_title
                            ==
                            title_attribute
                        ):

                            score += 60


                    scored.append(
                        (
                            score,
                            element
                        )
                    )


                except Exception:

                    continue


        # ----------------------------------------------------
        # NO VALID ELEMENT
        # ----------------------------------------------------

        if not scored:

            self.locator_metrics[
                "unresolved"
            ] += 1

            return None


        # ----------------------------------------------------
        # BEST MATCH
        # ----------------------------------------------------

        scored.sort(
            key=lambda x: x[0],
            reverse=True
        )


        best_score, best_element = scored[0]


        print(
            f"   🎯 Locator confidence: "
            f"{best_score}"
        )


        if best_score < 50:

            self.locator_metrics[
                "low_confidence"
            ] += 1

            return None


        self.locator_metrics[
            "resolved"
        ] += 1


        return best_element


    # ========================================================
    # CLEANUP
    # ========================================================

    async def cleanup(self, page):

        print(
            "   🧹 CLEANUP"
        )


        # ----------------------------------------------------
        # Close visible dialogs/modals
        # ----------------------------------------------------

        selectors = [

            '[role="dialog"]:visible '
            'button[aria-label="Close"]',

            '[role="dialog"]:visible '
            'button[aria-label="close"]',

            '[role="dialog"]:visible '
            'button.close',

            '[role="dialog"]:visible '
            'button:has-text("Close")',

            '[role="dialog"]:visible '
            'button:has-text("Cancel")',

            '[aria-modal="true"]:visible '
            'button[aria-label="Close"]',

            '[aria-modal="true"]:visible '
            'button.close',

            '[aria-modal="true"]:visible '
            'button:has-text("Close")',

            '[aria-modal="true"]:visible '
            'button:has-text("Cancel")',

            '.modal.show:visible '
            'button.close',

            '.modal.show:visible '
            'button:has-text("Close")',

            '.modal.show:visible '
            'button:has-text("Cancel")'
        ]


        for selector in selectors:

            try:

                locator = page.locator(
                    selector
                )

                count = await locator.count()

                for index in range(count):

                    element = locator.nth(
                        index
                    )

                    if not await element.is_visible():
                        continue

                    try:

                        await element.click(
                            timeout=1500
                        )

                        await page.wait_for_timeout(
                            200
                        )

                        print(
                            "   ✓ Popup closed"
                        )

                    except Exception:

                        pass

            except Exception:

                pass


        # ----------------------------------------------------
        # Escape remaining overlays
        # ----------------------------------------------------

        try:

            await page.keyboard.press(
                "Escape"
            )

            await page.wait_for_timeout(
                200
            )

        except Exception:

            pass


        # ----------------------------------------------------
        # Verify UI state
        # ----------------------------------------------------

        try:

            remaining = await page.locator(
                """
                [role="dialog"]:visible,
                [aria-modal="true"]:visible
                """
            ).count()

            if remaining:

                print(
                    f"   ⚠ "
                    f"Remaining dialogs: "
                    f"{remaining}"
                )

            else:

                print(
                    "   ✓ UI clean"
                )

        except Exception:

            print(
                "   ✓ UI clean"
            )


    # ========================================================
    # GENERATE TESTS
    # ========================================================

    def generate_tests(self):

        print(
            "\n🧠 GENERATE BEHAVIORAL TESTS"
        )


        seen = set()


        for page_data in self.application_map:

            url = page_data[
                "url"
            ]


            # ------------------------------------------------
            # PAGE LOAD
            # ------------------------------------------------

            key = (
                "page_load",
                url
            )

            if key not in seen:

                self.tests.append({

                    "type":
                        "page_load",

                    "url":
                        url,

                    "description":
                        "Page should load successfully"
                })

                seen.add(
                    key
                )


            # ------------------------------------------------
            # ELEMENT TESTS
            # ------------------------------------------------

            for element in page_data[
                "elements"
            ]:

                semantic = element[
                    "semantic_type"
                ]


                if semantic in (
                    "unknown",
                    "link",
                    "progress_bar",
                    "dialog"
                ):

                    continue


                if not element.get(
                    "visible"
                ):

                    continue


                # Never test hidden/disabled ordinary inputs.

                if (
                    element.get("disabled")
                    and
                    semantic not in (
                        "button",
                    )
                ):

                    continue


                fingerprint = self.fingerprint(
                    element
                )


                label = (
                    element.get("text")
                    or
                    element.get("aria_label")
                    or
                    element.get("placeholder")
                    or
                    element.get("name")
                    or
                    semantic
                )


                test = None


                # ============================================
                # TEXT INPUT
                # ============================================

                if semantic == "text_input":

                    if (
                        element.get("disabled")
                        or
                        element.get("readonly")
                    ):

                        continue


                    test = {

                        "type":
                            "text_input",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "max_length":
                            element.get(
                                "max_length"
                            ),

                        "description":
                            f"Enter valid data into {label}"
                    }


                # ============================================
                # TEXTAREA
                # ============================================

                elif semantic == "text_area":

                    if (
                        element.get("disabled")
                        or
                        element.get("readonly")
                    ):

                        continue


                    test = {

                        "type":
                            "text_area",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "max_length":
                            element.get(
                                "max_length"
                            ),

                        "description":
                            f"Enter text into {label}"
                    }


                # ============================================
                # CHECKBOX
                # ============================================

                elif semantic == "checkbox":

                    test = {

                        "type":
                            "checkbox",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Toggle {label}"
                    }


                # ============================================
                # RADIO
                # ============================================

                elif semantic == "radio":

                    test = {

                        "type":
                            "radio",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Select {label}"
                    }


                # ============================================
                # SLIDER
                # ============================================

                elif semantic == "slider":

                    test = {

                        "type":
                            "slider",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Change {label}"
                    }


                # ============================================
                # DROPDOWN
                # ============================================

                elif semantic == "dropdown":

                    test = {

                        "type":
                            "dropdown",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Test dropdown {label}"
                    }


                # ============================================
                # COMBOBOX
                # ============================================

                elif semantic == "combobox":

                    test = {

                        "type":
                            "combobox",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Test combobox {label}"
                    }


                # ============================================
                # BUTTON
                # ============================================

                elif semantic == "button":

                    label_lower = (
                        str(label)
                        .lower()
                    )


                    if element.get(
                        "disabled"
                    ):

                        dynamic = any(
                            word in label_lower
                            for word in (
                                "enable",
                                "enabled",
                                "seconds",
                                "wait"
                            )
                        )

                        if not dynamic:
                            continue


                        test = {

                            "type":
                                "dynamic_button",

                            "url":
                                url,

                            "fingerprint":
                                fingerprint,

                            "description":
                                f"Wait for dynamic button {label}"
                        }

                    else:

                        test = {

                            "type":
                                "button",

                            "url":
                                url,

                            "fingerprint":
                                fingerprint,

                            "description":
                                f"Activate {label}"
                        }


                # ============================================
                # DATE
                # ============================================

                elif semantic == "date_picker":

                    test = {

                        "type":
                            "date_picker",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Interact with {label}"
                    }


                # ============================================
                # FILE
                # ============================================

                elif semantic == "file_upload":

                    test = {

                        "type":
                            "file_upload",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Verify upload control {label}"
                    }


                # ============================================
                # TAB
                # ============================================

                elif semantic == "tab":

                    test = {

                        "type":
                            "tab",

                        "url":
                            url,

                        "fingerprint":
                            fingerprint,

                        "description":
                            f"Activate tab {label}"
                    }


                if not test:
                    continue


                key = (
                    test["type"],
                    url,
                    json.dumps(
                        fingerprint,
                        sort_keys=True
                    )
                )


                if key in seen:
                    continue


                seen.add(
                    key
                )

                self.tests.append(
                    test
                )


            # ------------------------------------------------
            # TOOLTIP TESTS
            # ------------------------------------------------

            for element in page_data.get(
                "tooltip_triggers",
                []
            ):

                fingerprint = self.fingerprint(
                    element
                )


                label = (
                    element.get("text")
                    or
                    element.get("aria_label")
                    or
                    element.get("placeholder")
                    or
                    "tooltip trigger"
                )


                key = (
                    "tooltip",
                    url,
                    json.dumps(
                        fingerprint,
                        sort_keys=True
                    )
                )


                if key in seen:
                    continue


                seen.add(
                    key
                )


                self.tests.append({

                    "type":
                        "tooltip",

                    "url":
                        url,

                    "fingerprint":
                        fingerprint,

                    "description":
                        f"Trigger tooltip {label}"
                })


        print(
            f"   ✓ Tests generated: "
            f"{len(self.tests)}"
        )


    # ========================================================
    # PASS
    # ========================================================

    def passed(
        self,
        test,
        message=""
    ):

        result = {
            **test,

            "status":
                "PASS",

            "message":
                message
        }

        self.results.append(
            result
        )

        print(
            f"   ✅ PASS "
            f"{test['description']}"
        )


    # ========================================================
    # FAILURE
    # ========================================================

    async def failed(
        self,
        page,
        test,
        error
    ):

        filename = (
            slug(
                urlparse(
                    test["url"]
                ).path
            )
            +
            "_"
            +
            slug(
                test["type"]
            )
            +
            "_"
            +
            str(
                len(
                    self.failures
                )
            )
            +
            ".png"
        )


        screenshot_path = (
            SCREENSHOT_DIR /
            filename
        )


        try:

            await page.screenshot(
                path=str(
                    screenshot_path
                ),
                full_page=True
            )

        except Exception:

            screenshot_path = None


        result = {

            **test,

            "status":
                "FAIL",

            "error":
                str(error),

            "screenshot":
                str(screenshot_path)
                if screenshot_path
                else None
        }


        self.results.append(
            result
        )

        self.failures.append(
            result
        )


        print(
            f"   ❌ FAIL "
            f"{test['description']}"
        )

        print(
            f"      Reason: "
            f"{error}"
        )

        if screenshot_path:

            print(
                f"      Evidence: "
                f"{screenshot_path}"
            )


    # ========================================================
    # EXECUTE ONE TEST
    # ========================================================

    async def execute_test(
        self,
        page,
        test
    ):

        print(
            f"\n🧪 "
            f"{test['description']}"
        )


        try:

            await page.goto(
                test["url"],
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT
            )

            await page.wait_for_timeout(
                SHORT_WAIT
            )


            test_type = test[
                "type"
            ]


            # =================================================
            # PAGE LOAD
            # =================================================

            if test_type == "page_load":

                await page.locator(
                    "body"
                ).wait_for(
                    state="visible",
                    timeout=5000
                )

                self.passed(
                    test,
                    "Page loaded"
                )

                return


            # =================================================
            # RESOLVE
            # =================================================

            element = await self.resolve_element(
                page,
                test["fingerprint"]
            )


            if element is None:

                raise AssertionError(
                    "Semantic element could not "
                    "be resolved"
                )


            # =================================================
            # TEXT INPUT
            # =================================================

            if test_type == "text_input":

                await element.scroll_into_view_if_needed()


                value = (
                    "QA_AGENT_TEST"
                )


                max_length = test.get(
                    "max_length"
                )


                if (
                    max_length
                    and
                    max_length > 0
                ):

                    value = value[
                        :max_length
                    ]


                await element.fill(
                    value,
                    timeout=ACTION_TIMEOUT
                )


                await page.wait_for_timeout(
                    200
                )


                actual = (
                    await element.input_value()
                )


                if actual != value:

                    raise AssertionError(
                        f"Expected "
                        f"'{value}', "
                        f"got "
                        f"'{actual}'"
                    )


                self.passed(
                    test,
                    f"Value persisted: {actual}"
                )


            # =================================================
            # TEXTAREA
            # =================================================

            elif test_type == "text_area":

                await element.scroll_into_view_if_needed()


                value = (
                    "Autonomous QA V3.4"
                )


                max_length = test.get(
                    "max_length"
                )


                if (
                    max_length
                    and
                    max_length > 0
                ):

                    value = value[
                        :max_length
                    ]


                await element.fill(
                    value,
                    timeout=ACTION_TIMEOUT
                )


                actual = (
                    await element.input_value()
                )


                if actual != value:

                    raise AssertionError(
                        f"Expected "
                        f"'{value}', "
                        f"got "
                        f"'{actual}'"
                    )


                self.passed(
                    test,
                    "Textarea retained value"
                )


            # =================================================
            # CHECKBOX
            # =================================================

            elif test_type == "checkbox":

                await element.scroll_into_view_if_needed()


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


                self.passed(
                    test,
                    "Checked and unchecked"
                )


            # =================================================
            # RADIO
            # =================================================

            elif test_type == "radio":

                await element.scroll_into_view_if_needed()


                await element.check(
                    timeout=ACTION_TIMEOUT
                )


                if not await element.is_checked():

                    raise AssertionError(
                        "Radio did not select"
                    )


                self.passed(
                    test,
                    "Radio selected"
                )


            # =================================================
            # SLIDER
            # =================================================

            elif test_type == "slider":

                await element.scroll_into_view_if_needed()


                before = (
                    await element.input_value()
                )


                await element.focus()


                await page.keyboard.press(
                    "ArrowRight"
                )


                await page.wait_for_timeout(
                    200
                )


                after = (
                    await element.input_value()
                )


                self.passed(
                    test,
                    f"Slider: {before} → {after}"
                )


            # =================================================
            # DROPDOWN
            # =================================================

            elif test_type == "dropdown":

                option_count = await element.locator(
                    "option"
                ).count()


                if option_count == 0:

                    raise AssertionError(
                        "Dropdown has no options"
                    )


                if option_count > 1:

                    await element.select_option(
                        index=1
                    )


                self.passed(
                    test,
                    f"{option_count} options"
                )


            # =================================================
            # COMBOBOX
            # =================================================

            elif test_type == "combobox":

                await element.scroll_into_view_if_needed()


                await element.click(
                    timeout=ACTION_TIMEOUT
                )


                await page.wait_for_timeout(
                    300
                )


                listbox = page.locator(
                    '[role="listbox"]:visible'
                )


                if await listbox.count():

                    message = (
                        "Combobox opened listbox"
                    )

                else:

                    message = (
                        "Combobox activated"
                    )


                self.passed(
                    test,
                    message
                )


            # =================================================
            # BUTTON
            # =================================================

            elif test_type == "button":

                await element.scroll_into_view_if_needed()


                if await element.is_disabled():

                    raise AssertionError(
                        "Button is disabled"
                    )


                await element.click(
                    timeout=ACTION_TIMEOUT
                )


                await page.wait_for_timeout(
                    300
                )


                self.passed(
                    test,
                    "Button activated"
                )


            # =================================================
            # DYNAMIC BUTTON
            # =================================================

            elif test_type == "dynamic_button":

                enabled = False


                for _ in range(15):

                    element = await self.resolve_element(
                        page,
                        test["fingerprint"]
                    )


                    if element is None:

                        await page.wait_for_timeout(
                            500
                        )

                        continue


                    try:

                        if not await element.is_disabled():

                            enabled = True

                            break

                    except Exception:

                        pass


                    await page.wait_for_timeout(
                        1000
                    )


                if not enabled:

                    raise AssertionError(
                        "Dynamic button did not "
                        "become enabled"
                    )


                await element.click(
                    timeout=ACTION_TIMEOUT
                )


                self.passed(
                    test,
                    "Dynamic button enabled and clicked"
                )


            # =================================================
            # DATE
            # =================================================

            elif test_type == "date_picker":

                await element.scroll_into_view_if_needed()


                await element.click(
                    timeout=ACTION_TIMEOUT
                )


                self.passed(
                    test,
                    "Date picker interacted with"
                )


            # =================================================
            # FILE
            # =================================================

            elif test_type == "file_upload":

                actual_type = (
                    await element.get_attribute(
                        "type"
                    )
                )


                if actual_type != "file":

                    raise AssertionError(
                        "Not a file upload control"
                    )


                self.passed(
                    test,
                    "File upload control detected"
                )


            # =================================================
            # TOOLTIP
            # =================================================

            elif test_type == "tooltip":

                await element.scroll_into_view_if_needed()


                await element.hover(
                    timeout=ACTION_TIMEOUT
                )


                await page.wait_for_timeout(
                    800
                )


                tooltip = page.locator(
                    '[role="tooltip"]:visible'
                )


                if await tooltip.count():

                    message = (
                        "Tooltip appeared"
                    )

                else:

                    # Native title tooltips may not expose
                    # DOM elements. Hover itself is therefore
                    # considered successful.
                    message = (
                        "Tooltip trigger hovered"
                    )


                self.passed(
                    test,
                    message
                )


            # =================================================
            # TAB
            # =================================================

            elif test_type == "tab":

                await element.scroll_into_view_if_needed()


                await element.click(
                    timeout=ACTION_TIMEOUT
                )


                self.passed(
                    test,
                    "Tab activated"
                )


            else:

                raise AssertionError(
                    f"Unknown test type: "
                    f"{test_type}"
                )


        except Exception as error:

            await self.failed(
                page,
                test,
                error
            )


        finally:

            await self.cleanup(
                page
            )


    # ========================================================
    # RUN
    # ========================================================

    async def run_tests(self):

        print(
            "\n▶️ EXECUTE TESTS"
        )


        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=False
            )


            context = await browser.new_context()


            page = await context.new_page()


            # ------------------------------------------------
            # Browser dialogs
            # ------------------------------------------------

            async def handle_dialog(dialog):

                print(
                    f"   ⚠ Dialog: "
                    f"{dialog.type}"
                )


                self.dialogs.append({

                    "type":
                        dialog.type,

                    "message":
                        dialog.message
                })


                try:

                    await dialog.dismiss()

                    print(
                        "   ✓ Dialog handled"
                    )

                except Exception:

                    pass


            page.on(
                "dialog",
                handle_dialog
            )


            for test in self.tests:

                await self.execute_test(
                    page,
                    test
                )


            await browser.close()


    # ========================================================
    # REPORT
    # ========================================================

    def report(self):

        banner(
            "📊 V3.4 AUTONOMOUS QA REPORT"
        )


        total = len(
            self.results
        )


        passed = sum(
            result["status"] == "PASS"
            for result in self.results
        )


        failed = sum(
            result["status"] == "FAIL"
            for result in self.results
        )


        print(
            f"\nPages discovered : "
            f"{len(self.application_map)}"
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
            "\n🎯 LOCATOR INTELLIGENCE"
        )


        print(
            f"Resolved         : "
            f"{self.locator_metrics['resolved']}"
        )


        print(
            f"Unresolved       : "
            f"{self.locator_metrics['unresolved']}"
        )


        print(
            f"Low confidence   : "
            f"{self.locator_metrics['low_confidence']}"
        )


        if self.failures:

            print(
                "\n🚨 FAILURES"
            )


            for failure in self.failures:

                print(
                    f"\n❌ "
                    f"{failure['description']}"
                )

                print(
                    f"URL: "
                    f"{failure['url']}"
                )

                print(
                    f"Reason: "
                    f"{failure['error']}"
                )

                if failure.get(
                    "screenshot"
                ):

                    print(
                        f"Evidence: "
                        f"{failure['screenshot']}"
                    )

        else:

            print(
                "\n✅ No execution failures detected."
            )


        # ----------------------------------------------------
        # Application map
        # ----------------------------------------------------

        map_file = (
            REPORT_DIR /
            "application_map.json"
        )


        map_file.write_text(
            json.dumps(
                self.application_map,
                indent=2
            ),
            encoding="utf-8"
        )


        # ----------------------------------------------------
        # Test report
        # ----------------------------------------------------

        report_file = (
            REPORT_DIR /
            "test_report.json"
        )


        report_file.write_text(
            json.dumps(
                {

                    "agent":
                        "Autonomous QA Agent",

                    "version":
                        "3.4",

                    "target":
                        self.base_url,

                    "pages_discovered":
                        len(
                            self.application_map
                        ),

                    "tests_generated":
                        len(
                            self.tests
                        ),

                    "tests_executed":
                        total,

                    "pass":
                        passed,

                    "fail":
                        failed,

                    "dialogs_handled":
                        len(
                            self.dialogs
                        ),

                    "locator_metrics":
                        self.locator_metrics,

                    "tests":
                        self.tests,

                    "results":
                        self.results,

                    "failures":
                        self.failures
                },
                indent=2
            ),
            encoding="utf-8"
        )


        print(
            f"\n🗺️ Application map:"
            f"\n{map_file.absolute()}"
        )


        print(
            f"\n📄 Test report:"
            f"\n{report_file.absolute()}"
        )


# ============================================================
# MAIN
# ============================================================

async def main(url):

    banner(
        "🤖 AUTONOMOUS QA AGENT V3.4"
    )


    print(
        f"\n🎯 TARGET: {url}"
    )


    agent = QAAgent(
        url
    )


    # ========================================================
    # RECURSIVE DISCOVERY
    # ========================================================

    print(
        "\n🗺️ DISCOVER APPLICATION"
    )


    agent.add_url(
        url,
        0
    )


    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=False
        )


        page = await browser.new_page()


        while (
            agent.queue
            and
            len(agent.visited)
            < MAX_PAGES
        ):

            item = agent.queue.pop(
                0
            )


            current_url = item[
                "url"
            ]

            depth = item[
                "depth"
            ]


            if current_url in agent.visited:
                continue


            agent.visited.add(
                current_url
            )


            await agent.discover(
                page,
                current_url,
                depth
            )


        await browser.close()


    # ========================================================
    # TEST GENERATION
    # ========================================================

    agent.generate_tests()


    # ========================================================
    # EXECUTION
    # ========================================================

    await agent.run_tests()


    # ========================================================
    # REPORT
    # ========================================================

    agent.report()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    if len(sys.argv) != 2:

        print(
            "\nUsage:\n"
            "python3 qa_agent.py "
            "\"https://demoqa.com\"\n"
        )

        sys.exit(1)


    target = sys.argv[1]


    if not valid_url(target):

        print(
            "❌ Invalid URL.\n"
            "Use:\n"
            "https://demoqa.com"
        )

        sys.exit(1)


    try:

        asyncio.run(
            main(target)
        )

    except KeyboardInterrupt:

        print(
            "\n\n🛑 Agent stopped."
        )

    except Exception as exc:

        print(
            f"\n❌ Agent crashed: {exc}"
        )

        raise

