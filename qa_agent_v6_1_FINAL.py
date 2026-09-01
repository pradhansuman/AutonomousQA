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

# ============================================================
# V5.2 CREDENTIAL-AWARE QA
# ============================================================
#
# Credentials are NEVER printed, embedded in screenshots, or
# written to JSON reports.
#
# Supported environment variables:
#   QA_USERNAME
#   QA_PASSWORD
#
# Optional:
#   QA_LOGIN_URL       explicit login page
#   QA_AUTH_REQUIRED   1/true/yes to require authentication
#
# If credentials are not supplied, the agent remains anonymous.
#
# IMPORTANT:
# Do not pass passwords as ordinary command-line arguments because
# shell history/process listings can expose them.
# ============================================================

import os
import getpass

QA_USERNAME = os.environ.get("QA_USERNAME", "").strip()
QA_PASSWORD = os.environ.get("QA_PASSWORD", "")
QA_LOGIN_URL = os.environ.get("QA_LOGIN_URL", "").strip()
QA_AUTH_REQUIRED = os.environ.get(
    "QA_AUTH_REQUIRED", ""
).strip().lower() in {"1", "true", "yes"}


# ============================================================
# V6 USER PREFERENCES
# ============================================================
#
# The left-side preference panel controls the agent behavior.
# These values can also be overridden with environment variables.
#
# QA_BROWSER=chromium|firefox|webkit
# QA_HEADLESS=0|1
# QA_MAX_PAGES=<number>
# QA_MAX_ACTIONS=<number>
# QA_EXPLORE=0|1
# QA_INVESTIGATE=0|1
# QA_RETRIES=<number>
# QA_NETWORK=0|1
# QA_CONSOLE=0|1
# QA_ACCESSIBILITY=0|1
# QA_SCREENSHOTS=0|1
# QA_RISK_THRESHOLD=LOW|MEDIUM|HIGH
# ============================================================

# ============================================================
# V6.1 APPLICATION MEMORY
# ============================================================

V61_MEMORY_DIR = Path(
    os.environ.get(
        "QA_MEMORY_DIR",
        str(Path.home() / "web_agent" / "qa_v6_1_memory")
    )
)
V61_MEMORY_FILE = V61_MEMORY_DIR / "application_memory.json"


V6_PREFERENCES = {
    "browser": os.environ.get("QA_BROWSER", "chromium").strip().lower(),
    "headless": os.environ.get("QA_HEADLESS", "0").strip().lower()
        in {"1", "true", "yes"},
    "max_pages": int(os.environ.get("QA_MAX_PAGES", "30")),
    "max_actions": int(os.environ.get("QA_MAX_ACTIONS", "100")),
    "explore": os.environ.get("QA_EXPLORE", "1").strip().lower()
        in {"1", "true", "yes"},
    "investigate": os.environ.get("QA_INVESTIGATE", "1").strip().lower()
        in {"1", "true", "yes"},
    "retries": max(1, int(os.environ.get("QA_RETRIES", "3"))),
    "network": os.environ.get("QA_NETWORK", "1").strip().lower()
        in {"1", "true", "yes"},
    "console": os.environ.get("QA_CONSOLE", "1").strip().lower()
        in {"1", "true", "yes"},
    "accessibility": os.environ.get("QA_ACCESSIBILITY", "1").strip().lower()
        in {"1", "true", "yes"},
    "screenshots": os.environ.get("QA_SCREENSHOTS", "1").strip().lower()
        in {"1", "true", "yes"},
    "risk_threshold": os.environ.get(
        "QA_RISK_THRESHOLD", "MEDIUM"
    ).strip().upper(),
    "memory": os.environ.get(
        "QA_MEMORY", "1"
    ).strip().lower() in {"1", "true", "yes"},
}

if V6_PREFERENCES["browser"] not in {"chromium", "firefox", "webkit"}:
    V6_PREFERENCES["browser"] = "chromium"

if V6_PREFERENCES["risk_threshold"] not in {"LOW", "MEDIUM", "HIGH"}:
    V6_PREFERENCES["risk_threshold"] = "MEDIUM"

MAX_PAGES = 30
MAX_LINKS_PER_PAGE = 20

NAV_TIMEOUT = 20000
ACTION_TIMEOUT = 7000
DYNAMIC_WAIT_SECONDS = 15

REPORT_DIR = Path("qa_v6_1_report")
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

    # Detect custom/range sliders even when the framework omits
    # type="range". Numeric min/max/step/value is strong evidence.
    if (
        e.get("min") not in (None, "")
        and e.get("max") not in (None, "")
        and (
            e.get("step") not in (None, "")
            or str(e.get("value", "")).strip().lstrip("-").isdigit()
        )
    ):
        return "slider"

    # Detect slider widgets whose framework exposes the control as a
    # text input but marks it through class/HTML semantics.
    slider_text = " ".join(
        str(e.get(k, "")).lower()
        for k in ("class_name", "outer_html", "aria_label", "title")
    )

    if (
        "slider" in slider_text
        and (
            str(e.get("value", "")).strip().lstrip("-").isdigit()
            or "aria-valuenow" in slider_text
            or "role=\"slider\"" in slider_text
        )
    ):
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

        self.goal = (
            "Find the highest-risk defects and verify critical user behavior."
        )
        self.plan = []
        self.exploration_actions = []
        self.exploration_defects = []
        self.investigated_defects = []
        self.confirmed_defects = []
        self.false_positives = []
        self.evidence_investigations = []
        self.confidence_summary = {
            "HIGH": 0,
            "MEDIUM": 0,
            "LOW": 0,
        }
        self.risk_counts = {
            "HIGH": 0,
            "MEDIUM": 0,
            "LOW": 0,
        }
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

                    value:
                        e.getAttribute("value") || "",

                    min:
                        e.getAttribute("min") || "",

                    max:
                        e.getAttribute("max") || "",

                    step:
                        e.getAttribute("step") || "",

                    data_testid:
                        e.getAttribute("data-testid") || "",

                    class_name:
                        e.className || "",

                    outer_html:
                        e.outerHTML || "",

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
            "value": clean(e.get("value")),
            "min": clean(e.get("min")),
            "max": clean(e.get("max")),
            "step": clean(e.get("step")),
            "data_testid": clean(e.get("data_testid")),
            "class_name": clean(e.get("class_name")),
            "outer_html": clean(e.get("outer_html")),
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
        expected_min = clean(fp.get("min"))
        expected_max = clean(fp.get("max"))
        expected_step = clean(fp.get("step"))

        locators = []

        if element_id:
            # Stable DOM id is the strongest identity signal. Resolve it
            # independently before adding broader semantic locators.
            # Do not treat the same DOM node returned by role/label/id
            # selectors as multiple candidates.
            try:
                id_locator = page.locator(f'#{element_id}')
                count = await id_locator.count()

                for i in range(count):
                    item = id_locator.nth(i)

                    if not await item.is_visible():
                        continue

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

                    compatible = True

                    if semantic == "button":
                        compatible = (
                            actual_tag == "button"
                            or actual_role == "button"
                        )

                    elif semantic == "text_input":
                        compatible = (
                            actual_tag == "input"
                            and actual_type in {
                                "",
                                "text",
                                "email",
                                "tel",
                                "search",
                                "url",
                                "password",
                            }
                            and actual_type != "range"
                            and actual_role != "slider"
                        )

                    elif semantic == "text_area":
                        compatible = actual_tag == "textarea"

                    elif semantic == "checkbox":
                        compatible = (
                            actual_type == "checkbox"
                            or actual_role == "checkbox"
                        )

                    elif semantic == "radio":
                        compatible = (
                            actual_type == "radio"
                            or actual_role == "radio"
                        )

                    elif semantic == "slider":
                        compatible = (
                            actual_type == "range"
                            or actual_role == "slider"
                        )

                    elif semantic == "combobox":
                        compatible = actual_role == "combobox"

                    elif semantic == "tab":
                        compatible = actual_role == "tab"

                    elif semantic == "file_upload":
                        compatible = actual_type == "file"

                    elif semantic == "date_picker":
                        compatible = actual_type == "date"

                    elif semantic == "dropdown":
                        compatible = actual_tag == "select"

                    if compatible:
                        return item

            except Exception:
                pass

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
            if expected_min and expected_max:
                locators.append(
                    page.locator(
                        'input:visible'
                    ).filter(
                        has=page.locator(
                            ':scope'
                        )
                    )
                )

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

                    slider_signal = " ".join(
                        str(e.get(k, "")).lower()
                        for k in (
                            "class_name",
                            "outer_html",
                            "aria_label",
                            "title",
                        )
                    )

                    if (
                        input_type == "range"
                        or clean(e.get("role")).lower() == "slider"
                        or clean(e.get("aria_valuenow")) != ""
                        or (
                            "slider" in slider_signal
                            and (
                                clean(e.get("value")).lstrip("-").isdigit()
                                or "aria-valuenow" in slider_signal
                                or 'role="slider"' in slider_signal
                            )
                        )
                        or (
                            clean(e.get("min")) != ""
                            and clean(e.get("max")) != ""
                            and (
                                clean(e.get("step")) != ""
                                or clean(e.get("value")).lstrip("-").isdigit()
                            )
                        )
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

                    # An anonymous button with no stable semantic identity
                    # is not safely executable. Do not guess which button
                    # the agent should click.
                    if not any(
                        clean(e.get(k))
                        for k in (
                            "id",
                            "data_testid",
                            "aria_label",
                            "text",
                            "title",
                            "name",
                        )
                    ):
                        self.stats["expected_skips"] += 1
                        continue

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

                native_min = (
                    await element.get_attribute("min")
                    or ""
                ).strip()

                native_max = (
                    await element.get_attribute("max")
                    or ""
                ).strip()

                native_step = (
                    await element.get_attribute("step")
                    or ""
                ).strip()

                native_class = (
                    await element.get_attribute("class")
                    or ""
                ).strip().lower()

                native_html = ""
                try:
                    native_html = (
                        await element.evaluate(
                            "e => e.outerHTML"
                        )
                    ).lower()
                except Exception:
                    pass

                slider_runtime_signal = (
                    "slider" in native_class
                    or "aria-valuenow" in native_html
                    or 'role="slider"' in native_html
                )

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
                } or role == "slider" or slider_runtime_signal or (
                    native_min != ""
                    and native_max != ""
                    and native_step != ""
                ):

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
    # V4 GOAL-DRIVEN QUALITY PLANNER
    # ========================================================

    def build_quality_plan(self):
        """Rank already-generated tests by observable risk."""

        print("\n🎯 BUILD V4 QUALITY PLAN")

        planned = []

        for index, test in enumerate(list(self.tests)):
            semantic = clean(
                (test.get("fingerprint") or {}).get("semantic")
            ).lower()

            description = clean(
                test.get("description")
            ).lower()

            url = clean(
                test.get("url")
            ).lower()

            score = 10
            reasons = []

            if semantic in {
                "button", "text_input", "text_area", "combobox",
                "dropdown", "checkbox", "radio", "file_upload",
                "date_picker", "slider"
            }:
                score += 15
                reasons.append("interactive control")

            if semantic == "button":
                score += 15
                reasons.append("action")

            if "dynamic" in description or "wait" in description:
                score += 20
                reasons.append("dynamic behavior")

            if any(
                word in url or word in description
                for word in ("alert", "dialog", "confirm", "prompt")
            ):
                score += 15
                reasons.append("dialog behavior")

            if "form" in url:
                score += 10
                reasons.append("form workflow")

            if test.get("type") == "page_load":
                score = min(score, 20)
                reasons.append("baseline")

            if score >= 55:
                level = "HIGH"
            elif score >= 30:
                level = "MEDIUM"
            else:
                level = "LOW"

            planned.append({
                **test,
                "plan": {
                    "priority": 0,
                    "risk_score": score,
                    "risk_level": level,
                    "risk_reasons": reasons,
                    "original_index": index,
                },
            })

        planned.sort(
            key=lambda item: (
                -item["plan"]["risk_score"],
                item["plan"]["original_index"],
            )
        )

        self.risk_counts = {
            "HIGH": 0,
            "MEDIUM": 0,
            "LOW": 0,
        }

        for priority, test in enumerate(planned, start=1):
            test["plan"]["priority"] = priority
            self.risk_counts[
                test["plan"]["risk_level"]
            ] += 1

        self.plan = planned

        print(
            f"   HIGH   : {self.risk_counts['HIGH']}"
        )
        print(
            f"   MEDIUM : {self.risk_counts['MEDIUM']}"
        )
        print(
            f"   LOW    : {self.risk_counts['LOW']}"
        )

        print("\n🧭 TOP PRIORITIES")

        for test in self.plan[:10]:
            p = test["plan"]
            print(
                f"   #{p['priority']:02d} "
                f"[{p['risk_level']:<6}] "
                f"{p['risk_score']:02d} "
                f"{clean(test.get('description'))[:90]}"
            )

    def build_retest_plan(self):
        """Identify failed tests for a future targeted retry."""

        failed = [
            result
            for result in self.results
            if result.get("status") == "FAIL"
        ]

        retests = []

        for result in failed:
            for test in self.plan:
                if (
                    test.get("url") == result.get("url")
                    and clean(test.get("description"))
                    == clean(result.get("description"))
                ):
                    retests.append({
                        "url": test.get("url"),
                        "description": test.get("description"),
                        "reason": result.get("error"),
                        "risk": test.get("plan", {}),
                    })
                    break

        return retests


    # ========================================================
    # V5 TRUE EXPLORATORY QA
    # ========================================================

    async def explore_page(self, page, url):
        """Bounded, state-aware exploratory probing of visible controls."""
        print(f"\n🔬 EXPLORE {url}")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(300)
        except Exception as exc:
            self.exploration_defects.append({
                "url": url,
                "type": "navigation",
                "reason": str(exc)[:500],
            })
            return

        candidates = await page.locator(
            "button:visible,input:visible,textarea:visible,select:visible,[role='button']:visible,[role='checkbox']:visible,[role='radio']:visible"
        ).all()

        for element in candidates[:12]:
            try:
                tag = await element.evaluate("e => e.tagName.toLowerCase()")
                typ = (await element.get_attribute("type") or "").lower()
                role = (await element.get_attribute("role") or "").lower()
                eid = await element.get_attribute("id") or ""
                label = (
                    await element.get_attribute("aria-label")
                    or await element.get_attribute("placeholder")
                    or (await element.inner_text()).strip()
                )[:100]

                if await element.is_disabled():
                    continue

                # Input exploration.
                if tag == "input" and typ in {"text", "search", "email", "tel", "url"}:
                    original = await element.input_value()
                    await element.fill("QA_EXPLORE_TEST")
                    await page.wait_for_timeout(100)
                    actual = await element.input_value()

                    if actual != "QA_EXPLORE_TEST":
                        self.exploration_defects.append({
                            "url": url,
                            "type": "input_persistence",
                            "label": label,
                            "id": eid,
                            "reason": f"Expected test value, got {actual!r}",
                        })

                    await element.fill(original)

                elif tag == "textarea":
                    original = await element.input_value()
                    await element.fill("QA_EXPLORE_TEST")
                    await page.wait_for_timeout(100)
                    actual = await element.input_value()

                    if actual != "QA_EXPLORE_TEST":
                        self.exploration_defects.append({
                            "url": url,
                            "type": "textarea_persistence",
                            "label": label,
                            "id": eid,
                            "reason": f"Expected test value, got {actual!r}",
                        })

                    await element.fill(original)

                # Checkbox/radio exploration is reversible.
                elif typ == "checkbox" or role == "checkbox":
                    before = await element.is_checked()
                    await element.click()
                    await page.wait_for_timeout(100)
                    after = await element.is_checked()

                    if before == after:
                        self.exploration_defects.append({
                            "url": url,
                            "type": "checkbox_state",
                            "label": label,
                            "id": eid,
                            "reason": "Click did not change checkbox state",
                        })

                    await element.click()

                elif typ == "radio" or role == "radio":
                    await element.click()
                    await page.wait_for_timeout(100)

                    if not await element.is_checked():
                        self.exploration_defects.append({
                            "url": url,
                            "type": "radio_state",
                            "label": label,
                            "id": eid,
                            "reason": "Radio did not become selected",
                        })

                # Native sliders are observed, not incorrectly treated as text.
                elif typ == "range" or role == "slider":
                    value = await element.input_value()
                    self.exploration_actions.append({
                        "url": url,
                        "type": "slider_probe",
                        "label": label,
                        "id": eid,
                        "value": value,
                    })
                    continue

                # Buttons are clicked only when they have stable identity.
                elif tag == "button" or role == "button":
                    if not (eid or label):
                        continue

                    before_url = page.url
                    await element.click(timeout=3000)
                    await page.wait_for_timeout(250)

                    self.exploration_actions.append({
                        "url": url,
                        "type": "button_probe",
                        "label": label,
                        "id": eid,
                        "navigated": page.url != before_url,
                    })

                    # Return to the original page after exploration.
                    if page.url != url:
                        await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=15000
                        )
                        await page.wait_for_timeout(150)

                self.exploration_actions.append({
                    "url": url,
                    "type": "control_probe",
                    "tag": tag,
                    "input_type": typ,
                    "role": role,
                    "label": label,
                    "id": eid,
                })

            except Exception as exc:
                self.exploration_defects.append({
                    "url": url,
                    "type": "interaction",
                    "reason": str(exc)[:500],
                })

    async def run_exploration(self):
        print("\n🛡️ V5.2 RESOLUTION REGRESSION PROTECTION")
        print("   Unique-ID resolution: ENABLED")
        print("   Credential-aware login: ENABLED when configured")
        print("\n🔬 V5 TRUE EXPLORATORY PHASE")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()

            authenticated = False

            if QA_USERNAME or QA_PASSWORD or QA_AUTH_REQUIRED:
                try:
                    authenticated = await self.authenticate_if_configured(page)
                except Exception as exc:
                    print(f"🔐 Authentication error: {exc}")
                    if QA_AUTH_REQUIRED:
                        await browser.close()
                        raise

            for url in list(self.visited):
                if len(self.exploration_actions) >= 60:
                    break
                await self.explore_page(page, url)

            await browser.close()

        print(f"\n🔬 Exploratory actions : {len(self.exploration_actions)}")
        print(f"🚨 Exploratory defects : {len(self.exploration_defects)}")



    async def _safe_candidate_locator(self, page, candidate):
        """
        V5.2 deterministic semantic resolver.

        Resolution priority:
          1. unique visible DOM id
          2. exact role/name
          3. stable native attributes

        A unique compatible ID is authoritative. This protects against
        false ambiguity for DemoQA-style controls such as #login and
        #searchBox while preventing arbitrary nth() selection.
        """
        element_id = clean(candidate.get("id"))
        role = clean(candidate.get("role")).lower()
        tag = clean(candidate.get("tag")).lower()
        label = clean(candidate.get("label"))
        name = clean(candidate.get("name"))
        typ = clean(candidate.get("type")).lower()

        if element_id:
            try:
                loc = page.locator(f"#{element_id}")
                if await loc.count() == 1 and await loc.is_visible():
                    actual_tag = (
                        await loc.evaluate(
                            "e => e.tagName.toLowerCase()"
                        )
                    )
                    actual_type = (
                        await loc.get_attribute("type") or ""
                    ).lower()
                    actual_role = (
                        await loc.get_attribute("role") or ""
                    ).lower()

                    compatible = True

                    if candidate.get("semantic") == "button":
                        compatible = (
                            actual_tag == "button"
                            or actual_role == "button"
                        )
                    elif candidate.get("semantic") == "text_input":
                        compatible = (
                            actual_tag == "input"
                            and actual_type != "range"
                            and actual_role != "slider"
                        )
                    elif candidate.get("semantic") == "text_area":
                        compatible = actual_tag == "textarea"
                    elif candidate.get("semantic") == "checkbox":
                        compatible = (
                            actual_type == "checkbox"
                            or actual_role == "checkbox"
                        )
                    elif candidate.get("semantic") == "radio":
                        compatible = (
                            actual_type == "radio"
                            or actual_role == "radio"
                        )

                    if compatible:
                        return loc
            except Exception:
                pass

        if role and label:
            try:
                loc = page.get_by_role(
                    role,
                    name=label,
                    exact=True
                )
                if await loc.count() == 1 and await loc.is_visible():
                    return loc
            except Exception:
                pass

        try:
            if tag == "button" and label:
                loc = page.locator("button").filter(
                    has_text=label
                )
                if await loc.count() == 1 and await loc.is_visible():
                    return loc

            if tag == "input":
                if name:
                    loc = page.locator(
                        f'input[name="{name}"]'
                    )
                elif typ:
                    loc = page.locator(
                        f'input[type="{typ}"]'
                    )
                else:
                    loc = page.locator("input")

                if await loc.count() == 1 and await loc.is_visible():
                    return loc

            if tag == "textarea":
                loc = page.locator("textarea")
                if await loc.count() == 1 and await loc.is_visible():
                    return loc

            if tag == "select":
                loc = page.locator("select")
                if await loc.count() == 1 and await loc.is_visible():
                    return loc
        except Exception:
            pass

        return None



    # ========================================================
    # V5.2 AUTHENTICATION / CREDENTIAL-AWARE EXPLORATION
    # ========================================================

    async def _find_visible_input(self, page, selectors):
        for selector in selectors:
            try:
                loc = page.locator(selector)
                count = await loc.count()
                for i in range(count):
                    item = loc.nth(i)
                    if await item.is_visible() and await item.is_editable():
                        return item
            except Exception:
                continue
        return None

    async def authenticate_if_configured(self, page):
        """
        Authenticate only when credentials are explicitly supplied.

        Discovery is semantic/DOM based; selectors are limited to common
        login field identities. Password values are never logged.
        """

        if not QA_USERNAME and not QA_PASSWORD:
            return False

        if not QA_USERNAME or not QA_PASSWORD:
            if QA_AUTH_REQUIRED:
                raise RuntimeError(
                    "QA_AUTH_REQUIRED is enabled but QA_USERNAME/QA_PASSWORD "
                    "are incomplete."
                )
            print("🔐 Credentials incomplete; running anonymously.")
            return False

        if QA_LOGIN_URL:
            try:
                await page.goto(
                    QA_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=20000
                )
                await page.wait_for_timeout(300)
            except Exception as exc:
                if QA_AUTH_REQUIRED:
                    raise RuntimeError(
                        f"Configured login URL could not be opened: {exc}"
                    )
                print(f"⚠️ Login URL unavailable; continuing anonymously.")
                return False

        username = await self._find_visible_input(
            page,
            [
                'input[name="username"]',
                'input[name="userName"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[id*="username" i]',
                'input[id*="user" i]',
                'input[id*="email" i]',
                'input[autocomplete="username"]',
            ]
        )

        password = await self._find_visible_input(
            page,
            [
                'input[name="password"]',
                'input[type="password"]',
                'input[id*="password" i]',
                'input[autocomplete="current-password"]',
            ]
        )

        if username is None or password is None:
            # Some applications expose a Login button before the fields.
            try:
                login = page.get_by_role(
                    "button",
                    name=re.compile(r"^(log\s*in|sign\s*in)$", re.I)
                )
                if await login.count() == 1 and await login.is_visible():
                    await login.click()
                    await page.wait_for_timeout(500)

                    username = await self._find_visible_input(
                        page,
                        [
                            'input[name="username"]',
                            'input[name="userName"]',
                            'input[name="email"]',
                            'input[type="email"]',
                            'input[id*="username" i]',
                            'input[id*="user" i]',
                            'input[id*="email" i]',
                            'input[autocomplete="username"]',
                        ]
                    )
                    password = await self._find_visible_input(
                        page,
                        [
                            'input[name="password"]',
                            'input[type="password"]',
                            'input[id*="password" i]',
                            'input[autocomplete="current-password"]',
                        ]
                    )
            except Exception:
                pass

        if username is None or password is None:
            if QA_AUTH_REQUIRED:
                raise RuntimeError(
                    "Credentials were supplied but a login form could not "
                    "be resolved."
                )

            print("🔐 Login form not found; running anonymously.")
            return False

        await username.fill(QA_USERNAME)
        await password.fill(QA_PASSWORD)

        # Submit using semantic button identity.
        submitted = False

        try:
            buttons = page.get_by_role(
                "button",
                name=re.compile(
                    r"^(log\s*in|sign\s*in|submit)$",
                    re.I
                )
            )

            if await buttons.count() == 1 and await buttons.is_visible():
                await buttons.click()
                submitted = True
        except Exception:
            pass

        if not submitted:
            try:
                await password.press("Enter")
                submitted = True
            except Exception:
                pass

        await page.wait_for_timeout(1000)

        # Never print credentials. Only report authentication state.
        current = page.url
        body = ""
        try:
            body = (await page.locator("body").inner_text()).lower()
        except Exception:
            pass

        login_failed = any(
            phrase in body
            for phrase in (
                "invalid username",
                "invalid password",
                "incorrect password",
                "login failed",
                "authentication failed",
            )
        )

        if login_failed:
            raise RuntimeError(
                "Authentication was attempted but the application "
                "reported a login failure."
            )

        print(f"🔐 Authentication completed: {current}")
        return True


    # ========================================================
    # V5.1 DEFECT INVESTIGATION ENGINE
    # ========================================================

    async def _capture_diagnostics(self, page):
        """Capture browser-visible diagnostics for a reproducible finding."""

        diagnostics = {
            "url": page.url,
            "title": "",
            "console_errors": [],
            "page_errors": [],
        }

        try:
            diagnostics["title"] = await page.title()
        except Exception:
            pass

        return diagnostics

    async def _reproduce_exploratory_defect(self, page, finding):
        """
        Reproduce a finding using the same safe action that discovered it.

        Investigation is intentionally conservative: no destructive actions,
        no arbitrary JavaScript, and no credentials/payment/account mutation.
        """

        url = finding.get("url", "")
        candidate = finding.get("candidate") or {}
        original_result = finding.get("result") or {}

        # Older exploration records may store identifying data inside result.
        if not candidate and original_result:
            candidate = {
                "id": original_result.get("id", ""),
                "label": original_result.get("label", ""),
                "tag": original_result.get("tag", ""),
                "type": original_result.get("type", ""),
                "role": original_result.get("role", ""),
                "name": original_result.get("name", ""),
            }

        if not url:
            return {
                "reproducible": False,
                "reason": "missing URL"
            }

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000
            )
            await page.wait_for_timeout(300)
        except Exception as exc:
            return {
                "reproducible": False,
                "reason": f"navigation failed: {exc}"
            }

        loc = await self._safe_candidate_locator(
            page,
            candidate
        )

        if loc is None:
            return {
                "reproducible": False,
                "reason": "candidate could not be resolved during retest"
            }

        try:
            tag = clean(candidate.get("tag")).lower()
            typ = clean(candidate.get("type")).lower()
            role = clean(candidate.get("role")).lower()

            before = await self._page_state_fingerprint(page)

            if tag == "input" and typ in {
                "text", "search", "email", "tel", "url"
            }:
                await loc.fill("QA_EXPLORE_TEST")
                await page.wait_for_timeout(150)
                actual = await loc.input_value()

                reproduced = actual != "QA_EXPLORE_TEST"

                return {
                    "reproducible": reproduced,
                    "actual": actual,
                    "expected": "QA_EXPLORE_TEST",
                    "reason": (
                        "input persistence defect reproduced"
                        if reproduced
                        else "input behaved correctly on retest"
                    ),
                    "state_changed": (
                        before != await self._page_state_fingerprint(page)
                    ),
                }

            if tag == "textarea":
                await loc.fill("QA_EXPLORE_TEST")
                await page.wait_for_timeout(150)
                actual = await loc.input_value()

                reproduced = actual != "QA_EXPLORE_TEST"

                return {
                    "reproducible": reproduced,
                    "actual": actual,
                    "expected": "QA_EXPLORE_TEST",
                    "reason": (
                        "textarea persistence defect reproduced"
                        if reproduced
                        else "textarea behaved correctly on retest"
                    ),
                }

            if typ == "checkbox" or role == "checkbox":
                before_checked = await loc.is_checked()
                await loc.click()
                await page.wait_for_timeout(100)
                after_checked = await loc.is_checked()

                reproduced = before_checked == after_checked

                return {
                    "reproducible": reproduced,
                    "before": before_checked,
                    "after": after_checked,
                    "reason": (
                        "checkbox state did not change on retest"
                        if reproduced
                        else "checkbox state changed correctly"
                    ),
                }

            if typ == "radio" or role == "radio":
                await loc.click()
                await page.wait_for_timeout(100)
                selected = await loc.is_checked()

                reproduced = not selected

                return {
                    "reproducible": reproduced,
                    "selected": selected,
                    "reason": (
                        "radio did not become selected"
                        if reproduced
                        else "radio selected correctly"
                    ),
                }

            if tag == "select":
                options = await loc.locator("option").all()
                current = await loc.input_value()
                target = None

                for option in options:
                    value = await option.get_attribute("value")
                    if value and value != current:
                        target = value
                        break

                if not target:
                    return {
                        "reproducible": False,
                        "reason": "no alternate option available"
                    }

                await loc.select_option(target)
                await page.wait_for_timeout(100)
                actual = await loc.input_value()

                reproduced = actual != target

                return {
                    "reproducible": reproduced,
                    "expected": target,
                    "actual": actual,
                    "reason": (
                        "dropdown selection did not persist"
                        if reproduced
                        else "dropdown behaved correctly"
                    ),
                }

            if tag == "button" or role == "button":
                before_url = page.url

                await loc.click(timeout=3000)
                await page.wait_for_timeout(500)

                after_url = page.url

                return {
                    "reproducible": False,
                    "reason": (
                        "button interaction completed; no deterministic "
                        "failure assertion was available"
                    ),
                    "before_url": before_url,
                    "after_url": after_url,
                }

            return {
                "reproducible": False,
                "reason": (
                    "finding type has no safe deterministic "
                    "reproduction rule"
                ),
            }

        except Exception as exc:
            return {
                "reproducible": False,
                "investigation_error": True,
                "reason": str(exc)[:1000],
            }

    def _classify_defect(self, finding, reproduction):
        """Assign severity from observable impact, not invented business data."""

        reason = clean(
            reproduction.get("reason")
        ).lower()

        candidate = finding.get("candidate") or {}
        semantic = (
            clean(candidate.get("role"))
            or clean(candidate.get("type"))
            or clean(candidate.get("tag"))
        ).lower()

        if reproduction.get("investigation_error"):
            return "INVESTIGATION_ERROR"

        if not reproduction.get("reproducible"):
            return "NOT_REPRODUCED"

        high_signals = (
            "navigation failed",
            "page navigation",
            "data loss",
            "security",
            "authorization",
            "crash",
        )

        medium_signals = (
            "did not persist",
            "state did not change",
            "did not become selected",
            "timeout",
            "exception",
        )

        if any(signal in reason for signal in high_signals):
            severity = "HIGH"
        elif any(signal in reason for signal in medium_signals):
            severity = "MEDIUM"
        elif semantic in {
            "button", "checkbox", "radio", "select", "combobox"
        }:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        return severity


    # ========================================================
    # V5.3 EVIDENCE-DRIVEN INVESTIGATION
    # ========================================================

    async def _collect_runtime_evidence(self, page):
        """Collect observable browser evidence without exposing credentials."""

        evidence = {
            "url": page.url,
            "title": "",
            "console_errors": [],
            "page_errors": [],
        }

        try:
            evidence["title"] = await page.title()
        except Exception:
            pass

        # Page-level JS error buffer, if available.
        try:
            errors = await page.evaluate(
                "() => window.__qa_v53_errors || []"
            )
            if isinstance(errors, list):
                evidence["page_errors"] = errors[-20:]
        except Exception:
            pass

        return evidence

    async def _evidence_snapshot(self, page, finding, run_number):
        """Capture screenshot + compact DOM/state evidence."""

        evidence_dir = REPORT_DIR / "screenshots"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        safe = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            f"v53_{run_number}_{finding.get('url','finding')}"
        )[:160]

        record = {
            "run": run_number,
            "url": page.url,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            if V6_PREFERENCES["screenshots"]:
                path = evidence_dir / f"{safe}.png"
                await page.screenshot(
                    path=str(path),
                    full_page=True
                )
                record["screenshot"] = str(path)
        except Exception as exc:
            record["screenshot_error"] = str(exc)

        try:
            record["dom"] = await page.evaluate(
                """
                () => ({
                    url: location.href,
                    title: document.title,
                    body_text: (document.body.innerText || "").slice(0, 5000),
                    active_element: document.activeElement
                        ? {
                            tag: document.activeElement.tagName,
                            id: document.activeElement.id || "",
                            name: document.activeElement.getAttribute("name") || ""
                          }
                        : null
                })
                """
            )
        except Exception as exc:
            record["dom_error"] = str(exc)

        record["runtime"] = await self._collect_runtime_evidence(page)
        return record

    def _confidence_from_runs(self, runs):
        """
        Confidence is based on repeated identical observable outcomes.

        HIGH   = same failure reproduced in >= 3/3 runs
        MEDIUM = same failure reproduced in 2/3 runs
        LOW    = one reproduction or inconsistent evidence
        """
        reproduced = sum(
            1 for r in runs
            if r.get("reproducible") is True
        )

        total = len(runs)
        total = max(1, total)
        ratio = reproduced / total

        if ratio >= 0.80 and reproduced >= 2:
            return "HIGH"
        if ratio >= 0.50:
            return "MEDIUM"
        if reproduced >= 1:
            return "LOW"
        return "NOT_REPRODUCED"

    async def investigate_defects(self):
        """
        V5.3: investigate each exploratory finding up to three times,
        collect evidence on every run, compare outcomes, and assign
        confidence.

        No credential values are captured in evidence.
        """

        print("\n🔎 V5.3 EVIDENCE-DRIVEN INVESTIGATION")

        if not self.exploration_defects:
            print("   No exploratory findings require investigation.")
            return

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()

            for index, finding in enumerate(
                self.exploration_defects,
                start=1
            ):
                print(f"\n   🔬 INVESTIGATE #{index}")

                runs = []

                for run_number in range(1, V6_PREFERENCES['retries'] + 1):
                    try:
                        reproduction = (
                            await self._reproduce_exploratory_defect(
                                page,
                                finding
                            )
                        )
                    except Exception as exc:
                        reproduction = {
                            "reproducible": False,
                            "investigation_error": True,
                            "reason": str(exc)[:1000],
                        }

                    snapshot = await self._evidence_snapshot(
                        page,
                        finding,
                        f"{index}_{run_number}"
                    )

                    run_record = {
                        "run": run_number,
                        "reproduction": reproduction,
                        "evidence": snapshot,
                    }

                    runs.append(run_record)

                    # Reset page between repetitions.
                    try:
                        await page.goto(
                            finding.get("url", page.url),
                            wait_until="domcontentloaded",
                            timeout=20000
                        )
                        await page.wait_for_timeout(300)
                    except Exception:
                        pass

                reproduction_results = [
                    r["reproduction"] for r in runs
                ]

                investigation_errors = sum(
                    1 for r in reproduction_results
                    if r.get("investigation_error")
                )

                confidence = self._confidence_from_runs(
                    reproduction_results
                )

                severity = self._classify_defect(
                    finding,
                    next(
                        (
                            r for r in reproduction_results
                            if r.get("reproducible")
                        ),
                        reproduction_results[0]
                    )
                )

                record = {
                    "investigation_id": index,
                    "url": finding.get("url"),
                    "candidate": finding.get("candidate"),
                    "original_result": finding.get("result"),
                    "runs": runs,
                    "confidence": confidence,
                    "severity": severity,
                    "investigation_errors": investigation_errors,
                    "reproduced_runs": sum(
                        1 for r in reproduction_results
                        if r.get("reproducible") is True
                    ),
                    "total_runs": len(runs),
                }

                self.evidence_investigations.append(record)

                if confidence in self.confidence_summary:
                    self.confidence_summary[confidence] += 1

                if confidence in {"HIGH", "MEDIUM"}:
                    self.confirmed_defects.append(record)

                    print(
                        f"      🚨 CONFIRMED "
                        f"[{confidence}/{severity}] "
                        f"{record['reproduced_runs']}/3 runs"
                    )
                else:
                    self.false_positives.append(record)

                    print(
                        f"      ✅ NOT CONFIRMED "
                        f"[{confidence}] "
                        f"{record['reproduced_runs']}/3 runs"
                    )

            await browser.close()

        print(
            f"\n🔎 Investigated : "
            f"{len(self.evidence_investigations)}"
        )
        print(
            f"🚨 Confirmed    : "
            f"{len(self.confirmed_defects)}"
        )
        print(
            f"📸 Evidence runs: "
            f"{sum(len(x['runs']) for x in self.evidence_investigations)}"
        )


    async def investigate_defects(self):
        """
        Re-run each exploratory finding independently.

        A defect becomes CONFIRMED only when the same failure reproduces.
        """

        print("\n🔎 V5.1 DEFECT INVESTIGATION")

        if not self.exploration_defects:
            print("   No exploratory findings require investigation.")
            return

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=False
            )

            context = await browser.new_context()
            page = await context.new_page()

            for index, finding in enumerate(
                self.exploration_defects,
                start=1
            ):

                print(
                    f"\n   🔬 INVESTIGATE #{index}"
                )

                reproduction = (
                    await self._reproduce_exploratory_defect(
                        page,
                        finding
                    )
                )

                severity = self._classify_defect(
                    finding,
                    reproduction
                )

                record = {
                    "investigation_id": index,
                    "url": finding.get("url"),
                    "candidate": finding.get("candidate"),
                    "original_result": finding.get("result"),
                    "reproduction": reproduction,
                    "classification": severity,
                    "reproduced": (
                        reproduction.get("reproducible")
                        is True
                    ),
                }

                # Capture evidence for confirmed defects.
                if record["reproduced"]:
                    try:
                        evidence_dir = (
                            REPORT_DIR / "screenshots"
                        )
                        evidence_dir.mkdir(
                            parents=True,
                            exist_ok=True
                        )

                        safe = re.sub(
                            r"[^A-Za-z0-9_.-]+",
                            "_",
                            f"{index}_{finding.get('url','defect')}"
                        )[:180]

                        evidence = (
                            evidence_dir
                            / f"investigated_{safe}.png"
                        )

                        await page.screenshot(
                            path=str(evidence),
                            full_page=True
                        )

                        record["evidence"] = str(evidence)

                    except Exception:
                        pass

                    self.confirmed_defects.append(record)

                    print(
                        f"      🚨 CONFIRMED "
                        f"[{severity}]"
                    )

                else:
                    self.false_positives.append(record)

                    print(
                        "      ✅ NOT REPRODUCED "
                        "(false-positive candidate)"
                    )

                self.investigated_defects.append(record)

            await browser.close()

        print(
            f"\n🔎 Investigated : "
            f"{len(self.investigated_defects)}"
        )

        print(
            f"🚨 Confirmed    : "
            f"{len(self.confirmed_defects)}"
        )

        print(
            f"✅ Not reproduced: "
            f"{len(self.false_positives)}"
        )

    # ========================================================
    # RUN
    # ========================================================


    # ========================================================
    # V6 LEFT-SIDE PREFERENCE PANEL
    # ========================================================

    def _print_preferences(self):
        p = V6_PREFERENCES

        print("""
╔══════════════════════════════════════════════════════════════╗
║              🤖 V6 AUTONOMOUS QA AGENT                      ║
╠════════════════════════════ LEFT SIDE ══════════════════════╣
║  ⚙ PREFERENCES                                               ║
║                                                              ║
║  Browser        : {browser:<10}                              ║
║  Headless       : {headless:<10}                              ║
║  Max Pages      : {pages:<10}                              ║
║  Max Actions    : {actions:<10}                              ║
║  Exploration    : {explore:<10}                              ║
║  Investigation  : {investigate:<10}                          ║
║  Evidence retry : {retries:<10}                              ║
║  Network capture: {network:<10}                              ║
║  Console capture: {console:<10}                              ║
║  Accessibility  : {access:<10}                              ║
║  Screenshots    : {shots:<10}                              ║
║  Risk threshold : {risk:<10}                              ║
╚══════════════════════════════════════════════════════════════╝
""".format(
            browser=p["browser"],
            headless=str(p["headless"]),
            pages=p["max_pages"],
            actions=p["max_actions"],
            explore=str(p["explore"]),
            investigate=str(p["investigate"]),
            retries=p["retries"],
            network=str(p["network"]),
            console=str(p["console"]),
            access=str(p["accessibility"]),
            shots=str(p["screenshots"]),
            risk=p["risk_threshold"],
        ))

    async def _v6_runtime_observation(self, page):
        """Collect console/network/runtime signals for V6."""
        observation = {
            "url": page.url,
            "title": "",
            "console_errors": [],
            "page_errors": [],
        }

        try:
            observation["title"] = await page.title()
        except Exception:
            pass

        try:
            observation["page_errors"] = await page.evaluate(
                "() => window.__qa_v6_page_errors || []"
            )
        except Exception:
            pass

        return observation


    # ========================================================
    # V6.1 LEARNING / APPLICATION MEMORY
    # ========================================================

    def _load_v61_memory(self):
        default = {
            "version": "6.1",
            "runs": 0,
            "urls": {},
            "actions": {},
            "findings": {},
        }
        try:
            V61_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            if not V61_MEMORY_FILE.exists():
                return default
            data = json.loads(
                V61_MEMORY_FILE.read_text(encoding="utf-8")
            )
            if not isinstance(data, dict):
                return default
            for key, value in default.items():
                data.setdefault(key, value)
            return data
        except Exception:
            return default

    def _save_v61_memory(self):
        try:
            V61_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            safe = {
                "version": "6.1",
                "runs": self.v61_memory.get("runs", 0),
                "urls": self.v61_memory.get("urls", {}),
                "actions": self.v61_memory.get("actions", {}),
                "findings": self.v61_memory.get("findings", {}),
            }
            tmp = V61_MEMORY_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(safe, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            tmp.replace(V61_MEMORY_FILE)
        except Exception as exc:
            print(f"⚠️ Memory save skipped: {exc}")

    def _v61_finding_key(self, finding):
        candidate = finding.get("candidate") or {}
        return "|".join([
            clean(finding.get("url", "")),
            clean(candidate.get("semantic", "")),
            clean(candidate.get("id", "")),
            clean(candidate.get("label", "")),
            clean(candidate.get("tag", "")),
        ])

    def _v61_prior_knowledge(self, url):
        urls = self.v61_memory.get("urls", {})
        findings = self.v61_memory.get("findings", {})
        known = []
        for key, item in findings.items():
            if item.get("url") == url:
                known.append({
                    "key": key,
                    "status": item.get("last_status"),
                    "confidence": item.get("last_confidence", "UNKNOWN"),
                })
        return {
            "visited_before": url in urls,
            "url_history": urls.get(url, {}),
            "known_findings": known,
        }

    def _v61_score_url(self, url):
        info = self._v61_prior_knowledge(url)
        history = info["url_history"]
        score = 25.0 if not info["visited_before"] else 0.0
        score += min(float(history.get("exploration_actions", 0)) * 0.05, 10)
        score += min(float(history.get("failures", 0)) * 15, 45)
        score += min(float(history.get("confirmed_defects", 0)) * 30, 60)
        score -= min(float(history.get("not_reproduced", 0)) * 3, 15)

        lowered = url.lower()
        for keyword in (
            "login", "checkout", "payment", "account",
            "profile", "search", "form", "upload", "download"
        ):
            if keyword in lowered:
                score += 8
        return round(score, 2)

    def _v61_rank_urls(self, urls):
        return sorted(
            ((url, self._v61_score_url(url)) for url in urls),
            key=lambda x: x[1],
            reverse=True
        )

    def _v61_learn(self):
        if not V6_PREFERENCES.get("memory", True):
            return

        urls = self.v61_memory.setdefault("urls", {})
        for url in getattr(self, "visited", []):
            entry = urls.setdefault(url, {
                "visits": 0,
                "exploration_actions": 0,
                "failures": 0,
                "confirmed_defects": 0,
                "not_reproduced": 0,
            })
            entry["visits"] += 1

        for finding in getattr(self, "exploration_defects", []):
            url = finding.get("url", "")
            if not url:
                continue
            entry = urls.setdefault(url, {
                "visits": 0,
                "exploration_actions": 0,
                "failures": 0,
                "confirmed_defects": 0,
                "not_reproduced": 0,
            })
            entry["exploration_actions"] += 1

            key = self._v61_finding_key(finding)
            self.v61_memory["findings"].setdefault(
                key,
                {
                    "url": url,
                    "observations": 0,
                    "last_status": "UNINVESTIGATED",
                    "history": [],
                }
            )
            item = self.v61_memory["findings"][key]
            item["observations"] += 1
            item["history"].append({
                "run": self.v61_memory["runs"],
                "status": "UNINVESTIGATED",
            })
            item["history"] = item["history"][-10:]

        for item in getattr(self, "evidence_investigations", []):
            url = item.get("url", "")
            if not url:
                continue
            entry = urls.setdefault(url, {
                "visits": 0,
                "exploration_actions": 0,
                "failures": 0,
                "confirmed_defects": 0,
                "not_reproduced": 0,
            })
            confidence = item.get("confidence", "NOT_REPRODUCED")
            status = (
                "CONFIRMED"
                if confidence in {"HIGH", "MEDIUM"}
                else "NOT_REPRODUCED"
            )
            if status == "CONFIRMED":
                entry["confirmed_defects"] += 1
            else:
                entry["not_reproduced"] += 1

        self._save_v61_memory()

        ranked = self._v61_rank_urls(list(getattr(self, "visited", [])))
        print("\n🧠 V6.1 LEARNING SUMMARY")
        print(f"   Memory runs : {self.v61_memory['runs']}")
        print(f"   Known URLs  : {len(self.v61_memory.get('urls', {}))}")
        print(f"   Findings    : {len(self.v61_memory.get('findings', {}))}")
        if ranked:
            print("   Priority surfaces:")
            for url, score in ranked[:5]:
                print(f"   • {score:>6.2f}  {url}")

    def _v61_start(self):
        self.v61_memory = self._load_v61_memory()
        self.v61_memory["runs"] = int(self.v61_memory.get("runs", 0)) + 1
        print("\n🧠 V6.1 APPLICATION MEMORY")
        print(f"   Previous runs : {self.v61_memory['runs'] - 1}")
        print(f"   Current run   : {self.v61_memory['runs']}")
        print(f"   Memory file   : {V61_MEMORY_FILE}")


    async def run(self):
        self._print_preferences()
        self._v61_start()
        print("\n🛡️ V5.2.1 DEFECT-INVESTIGATION PATCH ACTIVE")

        print(
            "\n🤖 AUTONOMOUS QA AGENT V4.1 RETRY"
        )

        print(
            f"🎯 GOAL: {self.goal}"
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

                item = self.queue.pop(0)

                url = item["url"]
                depth = item["depth"]

                if url in self.visited:
                    continue

                self.visited.add(url)

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

        # Proven V3.5.6 test generation.
        self.generate_tests()

        # New V4 planning layer.
        self.build_quality_plan()

        # Hard fallback: NEVER silently execute zero tests.
        if not self.plan:
            print(
                "\n⚠️ QUALITY PLAN EMPTY — "
                "falling back to generated tests."
            )
            self.plan = list(self.tests)

        print(
            f"\n▶️ EXECUTE V4.1 QUALITY PLAN "
            f"({len(self.plan)} tests)"
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
                        "type": dialog.type,
                        "message": dialog.message,
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

            for test in self.plan:

                await self.execute(
                    page,
                    test
                )

            await browser.close()

        self.retest_plan = self.build_retest_plan()

        if self.retest_plan:
            print(
                f"\n🔄 RETEST CANDIDATES: "
                f"{len(self.retest_plan)}"
            )
        else:
            print(
                "\n🔄 RETEST PLAN: No failed tests."
            )

        await self.run_exploration()

        await self.investigate_defects()

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

                    "v4_goal":
                        self.goal,

                    "risk_counts":
                        self.risk_counts,

                    "quality_plan":
                        [
                            {
                                "priority":
                                    t.get("plan", {}).get("priority"),
                                "risk_score":
                                    t.get("plan", {}).get("risk_score"),
                                "risk_level":
                                    t.get("plan", {}).get("risk_level"),
                                "risk_reasons":
                                    t.get("plan", {}).get("risk_reasons"),
                                "description":
                                    t.get("description"),
                                "url":
                                    t.get("url"),
                            }
                            for t in self.plan
                        ],

                    "retest_plan":
                        getattr(self, "retest_plan", []),

                    "v6_1_memory": {
                        "runs": self.v61_memory.get("runs", 0),
                        "memory_file": str(V61_MEMORY_FILE),
                        "known_urls": len(self.v61_memory.get("urls", {})),
                        "known_findings": len(self.v61_memory.get("findings", {})),
                    },

                    "v6_preferences": {
                        "browser": V6_PREFERENCES["browser"],
                        "headless": V6_PREFERENCES["headless"],
                        "max_pages": V6_PREFERENCES["max_pages"],
                        "max_actions": V6_PREFERENCES["max_actions"],
                        "explore": V6_PREFERENCES["explore"],
                        "investigate": V6_PREFERENCES["investigate"],
                        "retries": V6_PREFERENCES["retries"],
                        "network": V6_PREFERENCES["network"],
                        "console": V6_PREFERENCES["console"],
                        "accessibility": V6_PREFERENCES["accessibility"],
                        "screenshots": V6_PREFERENCES["screenshots"],
                        "risk_threshold": V6_PREFERENCES["risk_threshold"],
                    },

                    "v5_3_evidence_investigation": {
                        "investigations": self.evidence_investigations,
                        "confidence_summary": self.confidence_summary,
                        "investigated_count":
                            len(self.evidence_investigations),
                    },

                    "v5_1_investigation": {
                        "investigated": self.investigated_defects,
                        "confirmed": self.confirmed_defects,
                        "false_positives": self.false_positives,
                        "investigated_count":
                            len(self.investigated_defects),
                        "confirmed_count":
                            len(self.confirmed_defects),
                        "false_positive_count":
                            len(self.false_positives),
                    },

                    "v5_exploration": {
                        "actions": self.exploration_actions,
                        "defects": self.exploration_defects,
                        "action_count": len(self.exploration_actions),
                        "defect_count": len(self.exploration_defects),
                    },

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
            "📊 V6.1 LEARNING AUTONOMOUS QA REPORT"
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

        print(
            f"Exploratory actions : "
            f"{len(self.exploration_actions)}"
        )

        print(
            f"Exploratory defects : "
            f"{len(self.exploration_defects)}"
        )

        print(
            f"Confirmed defects   : "
            f"{len(self.confirmed_defects)}"
        )

        print(
            f"V5.3 confidence      : "
            f"HIGH={self.confidence_summary['HIGH']} "
            f"MEDIUM={self.confidence_summary['MEDIUM']} "
            f"LOW={self.confidence_summary['LOW']}"
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
    print("\n*** RUNNING qa_agent_v5_1_DEFECT_INVESTIGATION_FINAL.py — VERSION 6.1 FINAL ***\n")

    print("\n*** RUNNING qa_agent_v5_1_DEFECT_INVESTIGATION_FINAL.py — VERSION 6.1 FINAL ***\n")
    print("\n*** RUNNING qa_agent_v5_1_DEFECT_INVESTIGATION_FINAL.py — VERSION 6.1 FINAL ***\n")


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
        agent._v61_learn()


if __name__ == "__main__":
    asyncio.run(main())
