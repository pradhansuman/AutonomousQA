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

REPORT_DIR = Path("qa_v8_report")
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



# ============================================================
# V7.9 HEADLESS PERFORMANCE CONFIGURATION
# ============================================================

V79_DEFAULT_HEADLESS = True

def _v79_parse_cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="V7.9 Autonomous QA Agent"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://demoqa.com",
        help="Application URL",
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=None,
        help="Run browser headless (default)",
    )
    parser.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Show browser UI",
    )
    parser.add_argument(
        "--slow",
        action="store_true",
        help="Disable performance optimizations useful for debugging",
    )

    args = parser.parse_args(argv)

    if args.headless is None:
        args.headless = V79_DEFAULT_HEADLESS

    return args


class QAAgent:

    def __init__(self, target):

        # V7.9 runtime configuration.
        self.v79_headless = True
        self.v79_slow_mode = False
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

            element = None
            # SPA pages can render/re-hydrate controls after DOMContentLoaded.
            # Retry semantic resolution briefly before declaring a deterministic
            # regression failure.
            for _ in range(5):
                element = await self.resolve(
                    page,
                    test["fingerprint"],
                )
                if element is not None:
                    break
                await page.wait_for_timeout(300)

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
            browser = await p.chromium.launch(headless=getattr(self, "v79_headless", True))
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
            browser = await p.chromium.launch(headless=getattr(self, "v79_headless", True))
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

        print("\n🔎 V7.5 DEFECT INVESTIGATION")

        if not self.exploration_defects:
            print("   No exploratory findings require investigation.")
            return

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=getattr(self, "v79_headless", True)
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
        print("\n🧠 V7.5 LEARNING SUMMARY")
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



    # ========================================================
    # V6.2 ADAPTIVE RISK-DRIVEN EXPLORATION
    # ========================================================

    def _v62_candidate_urls(self):
        urls = list(getattr(self, "visited", []))

        # Include discovered URLs if the agent exposes them.
        for attr in ("discovered_urls", "pages", "urls"):
            value = getattr(self, attr, None)
            if isinstance(value, (list, tuple, set)):
                urls.extend(str(x) for x in value if x)

        # Preserve order and remove duplicates.
        return list(dict.fromkeys(urls))

    def _v62_build_priority_queue(self, urls):
        """
        Turn V6.1 learning scores into an actual exploration queue.
        The queue is consumed by V6.2 exploration whenever possible.
        """
        ranked = self._v61_rank_urls(urls)

        queue = []
        for rank, (url, score) in enumerate(ranked, 1):
            knowledge = self._v61_prior_knowledge(url)

            if score >= 50:
                tier = "HIGH"
            elif score >= 20:
                tier = "MEDIUM"
            else:
                tier = "LOW"

            queue.append({
                "rank": rank,
                "url": url,
                "score": score,
                "tier": tier,
                "visited_before": knowledge["visited_before"],
                "known_findings": len(
                    knowledge["known_findings"]
                ),
            })

        self.v62_priority_queue = queue
        return queue

    def _v62_print_priority_queue(self, queue):
        print("\n🎯 V6.2 ADAPTIVE EXPLORATION QUEUE")

        if not queue:
            print("   No candidate URLs available.")
            return

        for item in queue[:10]:
            print(
                f"   {item['rank']:>2}. "
                f"[{item['tier']:<6}] "
                f"{item['score']:>6.2f}  "
                f"{item['url']}"
            )

    def _v62_should_prioritize(self, url):
        for item in getattr(self, "v62_priority_queue", []):
            if item["url"] == url:
                return item["tier"] in {"HIGH", "MEDIUM"}
        return False

    def _v62_record_outcome(self, url, outcome):
        """
        outcome:
          exploration
          failure
          confirmed
          not_reproduced
        """
        if not url:
            return

        memory = getattr(self, "v61_memory", None)
        if not isinstance(memory, dict):
            return

        urls = memory.setdefault("urls", {})
        entry = urls.setdefault(url, {
            "visits": 0,
            "exploration_actions": 0,
            "failures": 0,
            "confirmed_defects": 0,
            "not_reproduced": 0,
        })

        if outcome == "exploration":
            entry["exploration_actions"] += 1
        elif outcome == "failure":
            entry["failures"] += 1
        elif outcome == "confirmed":
            entry["confirmed_defects"] += 1
        elif outcome == "not_reproduced":
            entry["not_reproduced"] += 1

    def _v62_adaptive_summary(self):
        queue = getattr(self, "v62_priority_queue", [])

        high = sum(
            1 for x in queue if x["tier"] == "HIGH"
        )
        medium = sum(
            1 for x in queue if x["tier"] == "MEDIUM"
        )
        low = sum(
            1 for x in queue if x["tier"] == "LOW"
        )

        print("\n🧠 V6.2 ADAPTIVE SUMMARY")
        print(f"   High priority   : {high}")
        print(f"   Medium priority : {medium}")
        print(f"   Low priority    : {low}")

        if queue:
            print(
                f"   Next target     : "
                f"{queue[0]['url']}"
            )

    def _v62_plan_exploration(self):
        urls = self._v62_candidate_urls()

        if not urls:
            return []

        queue = self._v62_build_priority_queue(urls)
        self._v62_print_priority_queue(queue)
        self._v62_adaptive_summary()

        return queue




    async def _safe_candidate_locator(self, page, candidate):
        """
        Backward-compatible resolver entry point.

        Older V5/V6 investigation code calls this method. Route it through
        the V6.2.1 adaptive resolver so investigation and normal execution
        use exactly the same locator strategy.
        """
        return await self._v621_resolve_candidate(page, candidate)

    # ========================================================
    # V6.2.1 ADAPTIVE RESOLVER
    # ========================================================
    #
    # Locator strength:
    #   1. exact ID
    #   2. exact name / aria-label
    #   3. exact role + accessible name
    #   4. semantic candidate
    #
    # Ambiguous generic elements must never override a unique
    # strong identifier.
    # ========================================================

    async def _v621_resolve_candidate(self, page, candidate):
        if not candidate:
            return None

        element_id = str(
            candidate.get("id") or ""
        ).strip()

        label = str(
            candidate.get("label") or ""
        ).strip()

        name = str(
            candidate.get("name") or ""
        ).strip()

        semantic = str(
            candidate.get("semantic") or ""
        ).strip().lower()

        tag = str(
            candidate.get("tag") or ""
        ).strip().lower()

        # ----------------------------------------------------
        # 1. Exact ID: strongest and deterministic.
        # ----------------------------------------------------
        if element_id:
            try:
                loc = page.locator(
                    f"#{self._css_escape(element_id)}"
                )
                count = await loc.count()

                if count == 1:
                    return loc
            except Exception:
                pass

        # ----------------------------------------------------
        # 2. Exact name.
        # ----------------------------------------------------
        if name:
            try:
                loc = page.locator(
                    f'[name="{self._css_escape(name)}"]'
                )
                count = await loc.count()

                if count == 1:
                    return loc
            except Exception:
                pass

        # ----------------------------------------------------
        # 3. Exact aria-label.
        # ----------------------------------------------------
        if label:
            try:
                loc = page.locator(
                    f'[aria-label="{self._css_escape(label)}"]'
                )
                count = await loc.count()

                if count == 1:
                    return loc
            except Exception:
                pass

        # ----------------------------------------------------
        # 4. Role + accessible name.
        # ----------------------------------------------------
        if semantic in {
            "button",
            "link",
            "checkbox",
            "radio",
            "combobox",
            "tab",
            "textbox",
            "heading",
        } and label:
            try:
                loc = page.get_by_role(
                    semantic,
                    name=label,
                    exact=True
                )
                count = await loc.count()

                if count == 1:
                    return loc
            except Exception:
                pass

        # ----------------------------------------------------
        # 5. Semantic/tag fallback.
        # ----------------------------------------------------
        selectors = []

        if semantic == "button":
            selectors.append("button")

        elif semantic == "link":
            selectors.append("a")

        elif semantic in {"text_input", "textbox"}:
            selectors.extend([
                "input:not([type='hidden'])",
                "textarea",
            ])

        elif semantic == "text_area":
            selectors.append("textarea")

        elif semantic == "checkbox":
            selectors.append("input[type='checkbox']")

        elif semantic == "radio":
            selectors.append("input[type='radio']")

        elif semantic == "combobox":
            selectors.extend([
                "select",
                "[role='combobox']",
            ])

        elif semantic == "tab":
            selectors.append("[role='tab']")

        elif tag:
            selectors.append(tag)

        if label:
            safe_label = self._css_escape(label)

            selectors.extend([
                f'[aria-label="{safe_label}"]',
                f'[placeholder="{safe_label}"]',
                f'text="{safe_label}"',
            ])

        for selector in selectors:
            try:
                loc = page.locator(selector)
                count = await loc.count()

                if count == 1:
                    return loc

                # If several candidates exist, prefer the first
                # visible/enabled candidate only when exactly one
                # satisfies those runtime conditions.
                if count > 1:
                    visible = []

                    for i in range(count):
                        candidate_loc = loc.nth(i)

                        try:
                            if await candidate_loc.is_visible():
                                visible.append(candidate_loc)
                        except Exception:
                            continue

                    if len(visible) == 1:
                        return visible[0]

            except Exception:
                continue

        return None

    def _css_escape(self, value):
        value = str(value)

        # Escape CSS string characters conservatively.
        return (
            value
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\A ")
            .replace("\r", "\\D ")
        )



    # ========================================================
    # V6.3 REASONING ADAPTIVE PLANNER
    # ========================================================
    #
    # V6.2 calculated priority.
    # V6.3 reasons over history and dynamically changes priority.
    #
    # Factors:
    #   - previous failures
    #   - confirmed defects
    #   - repeated false positives
    #   - exploration coverage
    #   - page interaction richness
    #   - business-sensitive URL vocabulary
    #   - recency of observations
    #
    # The planner is advisory and does not weaken the deterministic
    # regression suite.
    # ========================================================

    def _v63_history(self, url):
        memory = getattr(self, "v61_memory", {}) or {}
        return (
            memory.get("urls", {}).get(url, {})
            if isinstance(memory, dict)
            else {}
        )

    def _v63_reason_score(self, url):
        h = self._v63_history(url)

        score = 0.0

        visits = float(h.get("visits", 0))
        explorations = float(h.get("exploration_actions", 0))
        failures = float(h.get("failures", 0))
        confirmed = float(h.get("confirmed_defects", 0))
        not_reproduced = float(h.get("not_reproduced", 0))

        # Novelty.
        if visits == 0:
            score += 30.0

        # Unstable areas deserve attention.
        score += min(failures * 18.0, 54.0)

        # Confirmed defects dominate priority.
        score += min(confirmed * 45.0, 90.0)

        # Exploration gaps.
        if explorations == 0:
            score += 20.0
        elif explorations < 3:
            score += 8.0

        # Repeated false positives reduce priority, but never eliminate it.
        score -= min(not_reproduced * 4.0, 20.0)

        # Business-risk vocabulary.
        lowered = url.lower()
        business_terms = (
            "login", "auth", "account", "checkout", "payment",
            "order", "cart", "profile", "admin", "search",
            "upload", "download", "form", "settings"
        )
        for term in business_terms:
            if term in lowered:
                score += 7.0

        # Previously clean pages gradually become lower priority.
        if visits >= 3 and failures == 0 and confirmed == 0:
            score -= 8.0

        return round(max(score, 0.0), 2)

    def _v63_reason_tier(self, score):
        if score >= 60:
            return "CRITICAL"
        if score >= 35:
            return "HIGH"
        if score >= 15:
            return "MEDIUM"
        return "LOW"

    def _v63_build_reasoned_plan(self, urls):
        plan = []

        for url in urls:
            score = self._v63_reason_score(url)
            history = self._v63_history(url)

            plan.append({
                "url": url,
                "score": score,
                "tier": self._v63_reason_tier(score),
                "visits": int(history.get("visits", 0)),
                "explorations": int(
                    history.get("exploration_actions", 0)
                ),
                "failures": int(history.get("failures", 0)),
                "confirmed_defects": int(
                    history.get("confirmed_defects", 0)
                ),
                "not_reproduced": int(
                    history.get("not_reproduced", 0)
                ),
            })

        plan.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        self.v63_reasoned_plan = plan
        return plan

    def _v63_print_reasoned_plan(self, plan):
        print("\n🧠 V6.4 REASONED EXPLORATION PLAN")

        if not plan:
            print("   No application surfaces available.")
            return

        for i, item in enumerate(plan[:10], 1):
            print(
                f"   {i:>2}. "
                f"[{item['tier']:<8}] "
                f"{item['score']:>6.2f}  "
                f"{item['url']}"
            )

    def _v63_learning_effect(self, url):
        """
        Explain why a URL currently has its priority.
        Useful for debugging the agent's decisions.
        """
        h = self._v63_history(url)
        reasons = []

        if int(h.get("visits", 0)) == 0:
            reasons.append("novel")

        if int(h.get("failures", 0)) > 0:
            reasons.append("previous instability")

        if int(h.get("confirmed_defects", 0)) > 0:
            reasons.append("confirmed defect history")

        if int(h.get("exploration_actions", 0)) < 3:
            reasons.append("low exploration coverage")

        if int(h.get("not_reproduced", 0)) > 0:
            reasons.append("false-positive history")

        if not reasons:
            reasons.append("stable history")

        return reasons

    def _v63_replan(self):
        urls = []

        for attr in (
            "visited",
            "discovered_urls",
            "pages",
            "urls",
        ):
            value = getattr(self, attr, None)

            if isinstance(value, (list, tuple, set)):
                urls.extend(str(x) for x in value if x)

        urls = list(dict.fromkeys(urls))

        plan = self._v63_build_reasoned_plan(urls)
        self._v63_print_reasoned_plan(plan)

        if plan:
            top = plan[0]
            reasons = self._v63_learning_effect(top["url"])

            print("\n🎯 V6.4 NEXT BEST TARGET")
            print(f"   {top['url']}")
            print(f"   Score   : {top['score']:.2f}")
            print(f"   Tier    : {top['tier']}")
            print(
                "   Reasons : "
                + ", ".join(reasons)
            )

        return plan



    # ========================================================
    # V6.4 EXPLAINABLE GOAL-DRIVEN QA PLANNER
    # ========================================================
    #
    # V6.3 produced a risk score.
    # V6.4 converts that score into:
    #
    #   GOAL -> REASON -> PLAN -> ACTION -> OBSERVATION
    #        -> DECISION -> NEXT GOAL
    #
    # The planner is intentionally deterministic and explainable.
    # It does not replace the regression suite.
    # ========================================================

    def _v64_goal_for_url(self, url):
        lowered = url.lower()

        if any(x in lowered for x in ("login", "auth", "signin")):
            return "Validate authentication and session behavior"

        if any(x in lowered for x in ("upload", "download")):
            return "Validate file transfer and boundary behavior"

        if any(x in lowered for x in ("form", "input", "text-box")):
            return "Validate input, validation, and submission behavior"

        if any(x in lowered for x in ("search", "books")):
            return "Validate search, filtering, and result behavior"

        if any(x in lowered for x in ("table", "webtables")):
            return "Validate data integrity and table interactions"

        if any(x in lowered for x in ("modal", "dialog", "alert")):
            return "Validate transient UI and dialog behavior"

        if any(x in lowered for x in ("dynamic", "slider", "progress")):
            return "Validate state transitions and dynamic behavior"

        return "Discover unexpected behavior and edge cases"

    def _v64_reason_for_url(self, url):
        h = self._v63_history(url)
        reasons = []

        if int(h.get("visits", 0)) == 0:
            reasons.append("new application surface")

        if int(h.get("failures", 0)) > 0:
            reasons.append("previous execution instability")

        if int(h.get("confirmed_defects", 0)) > 0:
            reasons.append("confirmed defect history")

        if int(h.get("exploration_actions", 0)) == 0:
            reasons.append("no exploratory coverage")

        elif int(h.get("exploration_actions", 0)) < 3:
            reasons.append("limited exploratory coverage")

        if int(h.get("not_reproduced", 0)) > 0:
            reasons.append("previous false-positive findings")

        if not reasons:
            reasons.append("stable history requires deeper exploration")

        return reasons

    def _v64_actions_for_url(self, url):
        lowered = url.lower()

        if "upload-download" in lowered:
            return [
                "exercise upload with valid data",
                "exercise upload with invalid/boundary data",
                "exercise download",
                "verify resulting artifact/state",
                "repeat after state transition",
            ]

        if any(x in lowered for x in ("login", "auth", "signin")):
            return [
                "verify visible authentication controls",
                "exercise valid authentication path",
                "exercise invalid authentication path",
                "verify session/state transition",
                "verify logout/session termination",
            ]

        if any(x in lowered for x in ("form", "text-box")):
            return [
                "identify required fields",
                "enter valid boundary data",
                "enter invalid data",
                "submit and observe validation",
                "verify resulting state",
            ]

        if any(x in lowered for x in ("search", "books")):
            return [
                "exercise empty search",
                "exercise normal search",
                "exercise boundary search",
                "change search state",
                "verify result consistency",
            ]

        if any(x in lowered for x in ("table", "webtables")):
            return [
                "inspect table state",
                "exercise sorting/filtering",
                "exercise pagination",
                "exercise row editing",
                "verify data consistency",
            ]

        if any(x in lowered for x in ("modal", "dialog", "alert")):
            return [
                "open transient UI",
                "verify visible state",
                "dismiss transient UI",
                "repeat transition",
                "verify clean state",
            ]

        if any(x in lowered for x in ("dynamic", "slider", "progress")):
            return [
                "capture initial state",
                "trigger state transition",
                "wait for expected transition",
                "verify final state",
                "repeat transition",
            ]

        return [
            "inspect interactive controls",
            "exercise normal behavior",
            "exercise boundary behavior",
            "repeat a state transition",
            "verify resulting state",
        ]

    def _v64_build_explainable_plan(self):
        urls = []

        for attr in (
            "visited",
            "discovered_urls",
            "pages",
            "urls",
        ):
            value = getattr(self, attr, None)

            if isinstance(value, (list, tuple, set)):
                urls.extend(str(x) for x in value if x)

        urls = list(dict.fromkeys(urls))

        base_plan = self._v63_build_reasoned_plan(urls)
        plan = []

        for item in base_plan:
            url = item["url"]

            # V6.4 uses the same evidence-based score, but exposes
            # the actual reasoning behind it.
            goal = self._v64_goal_for_url(url)
            reasons = self._v64_reason_for_url(url)
            actions = self._v64_actions_for_url(url)

            plan.append({
                **item,
                "goal": goal,
                "reasons": reasons,
                "planned_actions": actions,
                "decision": (
                    "EXPLORE_NOW"
                    if item["tier"] in {"CRITICAL", "HIGH"}
                    else (
                        "EXPLORE_NEXT"
                        if item["tier"] == "MEDIUM"
                        else "DEFER"
                    )
                ),
            })

        self.v64_explainable_plan = plan
        return plan

    def _v64_print_plan(self, plan):
        print("\n🎯 V6.5 EXPLAINABLE QA PLAN")

        if not plan:
            print("   No application surfaces available.")
            return

        for rank, item in enumerate(plan[:10], 1):
            print(
                f"\n   {rank}. "
                f"[{item['tier']}] "
                f"{item['score']:.2f}  "
                f"{item['url']}"
            )
            print(f"      GOAL     : {item['goal']}")
            print(
                "      WHY      : "
                + "; ".join(item["reasons"])
            )
            print(
                f"      DECISION : {item['decision']}"
            )
            print("      PLAN     :")

            for action_no, action in enumerate(
                item["planned_actions"], 1
            ):
                print(
                    f"         {action_no}. {action}"
                )

    def _v64_next_best_target(self, plan):
        for item in plan:
            if item["decision"] in {
                "EXPLORE_NOW",
                "EXPLORE_NEXT",
            }:
                return item

        return plan[0] if plan else None

    def _v64_replan(self):
        plan = self._v64_build_explainable_plan()
        self._v64_print_plan(plan)

        target = self._v64_next_best_target(plan)

        if target:
            print("\n🧠 V6.5 NEXT DECISION")
            print(f"   Target : {target['url']}")
            print(f"   Goal   : {target['goal']}")
            print(f"   Score  : {target['score']:.2f}")
            print(f"   Tier   : {target['tier']}")
            print(
                "   Why    : "
                + "; ".join(target["reasons"])
            )
            print(
                f"   Action : {target['decision']}"
            )

        return plan



    # ========================================================
    # V6.5 BEHAVIORAL COVERAGE INTELLIGENCE
    # ========================================================
    #
    # PASS != COVERED
    #
    # V6.5 models behavioral surfaces independently of the
    # deterministic regression suite:
    #
    #   structure -> behavior -> state -> transition -> history
    #
    # The coverage model is advisory. It never changes the
    # proven deterministic regression execution path.
    # ========================================================

    def _v65_surface_type(self, url):
        u = str(url).lower()

        if "upload-download" in u:
            return "file_transfer"
        if any(x in u for x in ("login", "auth", "signin")):
            return "authentication"
        if any(x in u for x in ("form", "text-box")):
            return "form"
        if any(x in u for x in ("webtables", "table")):
            return "data_table"
        if any(x in u for x in ("modal", "dialog", "alerts")):
            return "transient_ui"
        if any(x in u for x in ("slider", "progress", "dynamic")):
            return "dynamic_state"
        if any(x in u for x in ("search", "books")):
            return "search"
        return "general"

    def _v65_expected_behaviors(self, url):
        surface = self._v65_surface_type(url)

        common = [
            "page_load",
            "navigation",
            "visible_controls",
            "normal_interaction",
            "boundary_behavior",
            "state_after_action",
        ]

        specialized = {
            "file_transfer": [
                "valid_upload",
                "invalid_upload",
                "download",
                "download_artifact_validation",
                "upload_download_state_transition",
            ],
            "authentication": [
                "valid_credentials",
                "invalid_credentials",
                "session_creation",
                "session_persistence",
                "logout",
            ],
            "form": [
                "valid_input",
                "required_field_validation",
                "invalid_input",
                "boundary_input",
                "submission_result",
            ],
            "data_table": [
                "row_read",
                "row_edit",
                "pagination",
                "sorting",
                "data_persistence",
            ],
            "transient_ui": [
                "open_dialog",
                "dialog_content",
                "confirm_or_cancel",
                "dismiss_dialog",
                "clean_state_after_dismiss",
            ],
            "dynamic_state": [
                "initial_state",
                "trigger_transition",
                "wait_for_transition",
                "final_state",
                "repeat_transition",
            ],
            "search": [
                "empty_search",
                "normal_search",
                "partial_search",
                "no_result_search",
                "result_consistency",
            ],
            "general": [
                "alternative_interaction",
                "repeat_action",
                "unexpected_sequence",
                "recovery_after_action",
                "clean_state",
            ],
        }

        return list(dict.fromkeys(common + specialized[surface]))

    def _v65_memory_behavior_names(self, url):
        h = self._v63_history(url)
        names = set()

        for key in (
            "covered_behaviors",
            "behaviors",
            "explored_behaviors",
        ):
            value = h.get(key, [])
            if isinstance(value, (list, tuple, set)):
                names.update(str(x) for x in value)

        return names

    def _v65_coverage_for_url(self, url):
        expected = self._v65_expected_behaviors(url)
        covered = self._v65_memory_behavior_names(url)

        # Also infer coverage from historical exploration count.
        h = self._v63_history(url)
        exploration_count = int(h.get("exploration_actions", 0))

        inferred = min(
            exploration_count,
            max(0, len(expected) - 1)
        )

        known = len(
            [x for x in expected if x in covered]
        )

        # If legacy memory has no named behavior records, represent
        # historical exploration conservatively rather than claiming
        # specific behavior was tested.
        effective = min(
            len(expected),
            max(known, inferred)
        )

        unknown = [
            x for x in expected
            if x not in covered
        ]

        if not covered and effective > 0:
            unknown = expected[effective:]

        percentage = round(
            (effective / len(expected)) * 100.0,
            1
        ) if expected else 100.0

        return {
            "url": url,
            "surface": self._v65_surface_type(url),
            "expected": expected,
            "covered": expected[:known] if not covered else [
                x for x in expected if x in covered
            ],
            "unknown": unknown,
            "coverage_percent": percentage,
            "known_behavior_count": known,
            "effective_behavior_count": effective,
            "total_behavior_count": len(expected),
        }

    def _v65_build_coverage_matrix(self, urls):
        matrix = [
            self._v65_coverage_for_url(url)
            for url in urls
        ]

        matrix.sort(
            key=lambda x: (
                -len(x["unknown"]),
                x["coverage_percent"],
                x["url"],
            )
        )

        self.v65_coverage_matrix = matrix
        return matrix

    def _v65_next_behavior(self, item):
        if not item["unknown"]:
            return None

        return item["unknown"][0]

    def _v65_print_coverage(self, matrix):
        print("\n🧪 V6.5 BEHAVIORAL COVERAGE MATRIX")

        if not matrix:
            print("   No behavioral surfaces available.")
            return

        for item in matrix[:10]:
            print(
                f"\n   {item['coverage_percent']:>5.1f}%  "
                f"[{item['surface']}]  "
                f"{item['url']}"
            )
            print(
                f"      Covered : "
                f"{item['effective_behavior_count']}/"
                f"{item['total_behavior_count']}"
            )
            print(
                f"      Unknown : "
                f"{len(item['unknown'])}"
            )

            next_behavior = self._v65_next_behavior(item)
            if next_behavior:
                print(
                    f"      NEXT    : {next_behavior}"
                )

    def _v65_replan_coverage(self):
        urls = []

        for attr in (
            "visited",
            "discovered_urls",
            "pages",
            "urls",
        ):
            value = getattr(self, attr, None)

            if isinstance(value, (list, tuple, set)):
                urls.extend(
                    str(x) for x in value if x
                )

        urls = list(dict.fromkeys(urls))

        matrix = self._v65_build_coverage_matrix(urls)
        self._v65_print_coverage(matrix)

        # Combine V6.4 reasoning with behavioral gaps.
        reasoned = getattr(
            self,
            "v64_explainable_plan",
            []
        )

        by_url = {
            x["url"]: x
            for x in matrix
        }

        combined = []

        for item in reasoned:
            coverage_item = by_url.get(item["url"])

            if not coverage_item:
                continue

            # Unknown behavior is a first-class exploration signal.
            gap_bonus = min(
                len(coverage_item["unknown"]) * 3.0,
                30.0
            )

            combined_score = round(
                float(item.get("score", 0.0))
                + gap_bonus
                + max(
                    0.0,
                    50.0 - coverage_item["coverage_percent"]
                ) * 0.25,
                2
            )

            combined.append({
                **item,
                "behavioral_coverage":
                    coverage_item["coverage_percent"],
                "unknown_behaviors":
                    coverage_item["unknown"],
                "next_behavior":
                    self._v65_next_behavior(coverage_item),
                "v65_score": combined_score,
            })

        combined.sort(
            key=lambda x: x["v65_score"],
            reverse=True
        )

        self.v65_reasoned_plan = combined

        print("\n🎯 V6.5 NEXT BEST BEHAVIOR")

        if combined:
            top = combined[0]
            print(f"   URL      : {top['url']}")
            print(
                f"   Coverage : "
                f"{top['behavioral_coverage']:.1f}%"
            )
            print(
                f"   Score    : "
                f"{top['v65_score']:.2f}"
            )
            print(
                f"   Behavior : "
                f"{top['next_behavior'] or 'none'}"
            )
            print(
                f"   Goal     : "
                f"{top.get('goal', 'Discover unknown behavior')}"
            )
        else:
            print("   No next behavior available.")

        return combined



    # ========================================================
    # V6.6 CLOSED-LOOP AUTONOMOUS QA
    # ========================================================
    #
    # V6.5 identified behavioral coverage gaps.
    # V6.6 turns those gaps into an explicit feedback loop:
    #
    #   OBSERVE -> UNDERSTAND -> CHOOSE -> ACT
    #       -> EVALUATE -> LEARN -> REPLAN
    #
    # The deterministic regression suite remains untouched.
    # This layer controls exploratory decision-making only.
    # ========================================================

    def _v66_surface_state(self, url):
        coverage = {}
        matrix = getattr(self, "v65_coverage_matrix", []) or []

        for item in matrix:
            if item.get("url") == url:
                coverage = item
                break

        history = self._v63_history(url)

        return {
            "coverage": coverage,
            "history": history,
        }

    def _v66_next_action(self, url):
        state = self._v66_surface_state(url)
        coverage = state["coverage"]
        history = state["history"]

        unknown = coverage.get("unknown", []) if coverage else []

        # Highest-value unknown behavioral gap wins.
        if unknown:
            return {
                "action": unknown[0],
                "reason": "uncovered behavioral surface",
                "confidence": "HIGH",
            }

        # If all named behaviors are covered, deliberately vary the
        # sequence to search for emergent/state-dependent defects.
        exploration_count = int(
            history.get("exploration_actions", 0)
        )

        if exploration_count < 3:
            return {
                "action": "repeat interaction under a different state",
                "reason": "insufficient repeated-state coverage",
                "confidence": "MEDIUM",
            }

        return {
            "action": "explore unexpected interaction sequence",
            "reason": "known behavior is covered; seek emergent behavior",
            "confidence": "MEDIUM",
        }

    def _v66_decision_score(self, item):
        score = float(item.get("v65_score", item.get("score", 0.0)))

        unknown_count = len(
            item.get("unknown_behaviors", [])
        )

        coverage = float(
            item.get("behavioral_coverage", 100.0)
        )

        # Behavioral uncertainty is more important than merely
        # visiting a page.
        score += min(unknown_count * 5.0, 40.0)
        score += max(0.0, 60.0 - coverage) * 0.35

        if item.get("confirmed_defects", 0):
            score += 50.0

        return round(score, 2)

    def _v66_build_closed_loop_plan(self):
        previous = getattr(
            self,
            "v65_reasoned_plan",
            []
        ) or []

        plan = []

        for item in previous:
            action = self._v66_next_action(item["url"])
            score = self._v66_decision_score(item)

            if score >= 75:
                decision = "ACT_NOW"
            elif score >= 40:
                decision = "ACT_NEXT"
            else:
                decision = "DEFER"

            plan.append({
                **item,
                "v66_score": score,
                "next_action": action["action"],
                "decision_reason": action["reason"],
                "decision_confidence": action["confidence"],
                "decision": decision,
            })

        plan.sort(
            key=lambda x: x["v66_score"],
            reverse=True
        )

        self.v66_closed_loop_plan = plan
        return plan

    def _v66_print_closed_loop(self, plan):
        print("\n🔄 V6.6 CLOSED-LOOP DECISION ENGINE")

        if not plan:
            print("   No application surfaces available.")
            return

        for rank, item in enumerate(plan[:10], 1):
            print(
                f"\n   {rank}. "
                f"[{item['decision']:<8}] "
                f"{item['v66_score']:>6.2f}  "
                f"{item['url']}"
            )
            print(
                f"      COVERAGE : "
                f"{item.get('behavioral_coverage', 0):.1f}%"
            )
            print(
                f"      GOAL     : "
                f"{item.get('goal', 'Discover behavior')}"
            )
            print(
                f"      NEXT     : "
                f"{item['next_action']}"
            )
            print(
                f"      WHY      : "
                f"{item['decision_reason']}"
            )
            print(
                f"      CONFIDENCE: "
                f"{item['decision_confidence']}"
            )

    def _v66_replan(self):
        plan = self._v66_build_closed_loop_plan()
        self._v66_print_closed_loop(plan)

        if plan:
            target = next(
                (
                    x for x in plan
                    if x["decision"] in {
                        "ACT_NOW",
                        "ACT_NEXT",
                    }
                ),
                plan[0]
            )

            print("\n🧠 V6.6 NEXT BEST ACTION")
            print(f"   Target : {target['url']}")
            print(f"   Action : {target['next_action']}")
            print(f"   Score  : {target['v66_score']:.2f}")
            print(f"   Why    : {target['decision_reason']}")

        return plan

    def _v66_record_observation(self, url, action, outcome):
        """
        Store a lightweight closed-loop observation when the
        underlying memory structure supports it.
        """
        memory = getattr(self, "v61_memory", None)

        if not isinstance(memory, dict):
            return

        urls = memory.setdefault("urls", {})
        record = urls.setdefault(url, {})

        observations = record.setdefault(
            "closed_loop_observations", []
        )

        observations.append({
            "action": str(action),
            "outcome": str(outcome),
        })

        # Prevent unbounded memory growth.
        if len(observations) > 100:
            del observations[:-100]



    # ========================================================
    # V6.7 DECISION / MEMORY INTEGRITY
    # ========================================================
    #
    # V6.7 makes learning measurable:
    #
    #   BEFORE -> DECIDE -> ACT -> OBSERVE -> LEARN -> AFTER
    #
    # It records a compact decision audit and compares the current
    # plan with the previous plan. This proves whether memory changes
    # the agent's next decision rather than merely growing a file.
    #
    # The deterministic regression engine remains untouched.
    # ========================================================

    def _v67_memory_snapshot(self):
        memory = getattr(self, "v61_memory", {}) or {}
        if not isinstance(memory, dict):
            return {
                "runs": 0,
                "urls": {},
            }

        urls = memory.get("urls", {})
        if not isinstance(urls, dict):
            urls = {}

        return {
            "runs": int(memory.get("runs", 0) or 0),
            "urls": urls,
        }

    def _v67_plan_signature(self, plan):
        if not plan:
            return None

        top = plan[0]
        return {
            "url": top.get("url", ""),
            "action": top.get(
                "next_action",
                top.get("next_behavior", "")
            ),
            "score": float(
                top.get(
                    "v66_score",
                    top.get(
                        "v65_score",
                        top.get("score", 0.0)
                    )
                )
            ),
            "coverage": float(
                top.get("behavioral_coverage", 0.0)
            ),
        }

    def _v67_capture_before(self):
        plan = getattr(
            self,
            "v66_closed_loop_plan",
            None
        )

        if not plan:
            plan = getattr(
                self,
                "v65_reasoned_plan",
                []
            )

        self.v67_before_plan = [
            dict(x) for x in (plan or [])
        ]
        self.v67_before_signature = (
            self._v67_plan_signature(
                self.v67_before_plan
            )
        )

    def _v67_capture_after(self):
        plan = getattr(
            self,
            "v66_closed_loop_plan",
            None
        )

        if not plan:
            plan = getattr(
                self,
                "v65_reasoned_plan",
                []
            )

        self.v67_after_plan = [
            dict(x) for x in (plan or [])
        ]
        self.v67_after_signature = (
            self._v67_plan_signature(
                self.v67_after_plan
            )
        )

    def _v67_decision_changed(self):
        before = getattr(
            self,
            "v67_before_signature",
            None
        )
        after = getattr(
            self,
            "v67_after_signature",
            None
        )

        if not before or not after:
            return False

        return (
            before.get("url") != after.get("url")
            or before.get("action") != after.get("action")
            or abs(
                before.get("score", 0.0)
                - after.get("score", 0.0)
            ) > 0.01
            or abs(
                before.get("coverage", 0.0)
                - after.get("coverage", 0.0)
            ) > 0.01
        )

    def _v67_learning_delta(self):
        before = getattr(
            self,
            "v67_before_plan",
            []
        )
        after = getattr(
            self,
            "v67_after_plan",
            []
        )

        before_map = {
            x.get("url"): x
            for x in before
            if x.get("url")
        }
        after_map = {
            x.get("url"): x
            for x in after
            if x.get("url")
        }

        delta = []

        for url in sorted(
            set(before_map) | set(after_map)
        ):
            b = before_map.get(url, {})
            a = after_map.get(url, {})

            old_score = float(
                b.get(
                    "v66_score",
                    b.get(
                        "v65_score",
                        b.get("score", 0.0)
                    )
                )
            )
            new_score = float(
                a.get(
                    "v66_score",
                    a.get(
                        "v65_score",
                        a.get("score", 0.0)
                    )
                )
            )

            old_cov = float(
                b.get("behavioral_coverage", 0.0)
            )
            new_cov = float(
                a.get("behavioral_coverage", 0.0)
            )

            if (
                abs(old_score - new_score) > 0.01
                or abs(old_cov - new_cov) > 0.01
            ):
                delta.append({
                    "url": url,
                    "old_score": round(old_score, 2),
                    "new_score": round(new_score, 2),
                    "score_delta": round(
                        new_score - old_score,
                        2
                    ),
                    "old_coverage": round(old_cov, 1),
                    "new_coverage": round(new_cov, 1),
                    "coverage_delta": round(
                        new_cov - old_cov,
                        1
                    ),
                })

        self.v67_learning_delta = delta
        return delta

    def _v67_print_audit(self):
        before = getattr(
            self,
            "v67_before_signature",
            None
        )
        after = getattr(
            self,
            "v67_after_signature",
            None
        )
        delta = self._v67_learning_delta()

        print("\n🧠 V6.7 DECISION / MEMORY AUDIT")

        if before:
            print(
                f"   BEFORE : "
                f"{before.get('url')} "
                f"→ {before.get('action')} "
                f"({before.get('score', 0):.2f})"
            )
        else:
            print("   BEFORE : no previous decision")

        if after:
            print(
                f"   AFTER  : "
                f"{after.get('url')} "
                f"→ {after.get('action')} "
                f"({after.get('score', 0):.2f})"
            )
        else:
            print("   AFTER  : no current decision")

        changed = self._v67_decision_changed()
        print(
            f"   DECISION CHANGED : "
            f"{'YES' if changed else 'NO'}"
        )

        print(
            f"   LEARNING DELTAS  : {len(delta)}"
        )

        if delta:
            for item in delta[:10]:
                print(
                    f"\n   {item['url']}"
                )
                print(
                    f"      Score    : "
                    f"{item['old_score']:.2f} "
                    f"→ {item['new_score']:.2f} "
                    f"({item['score_delta']:+.2f})"
                )
                print(
                    f"      Coverage : "
                    f"{item['old_coverage']:.1f}% "
                    f"→ {item['new_coverage']:.1f}% "
                    f"({item['coverage_delta']:+.1f}%)"
                )

        self.v67_audit = {
            "decision_changed": changed,
            "learning_deltas": delta,
            "before": before,
            "after": after,
        }

    def _v67_save_decision_audit(self):
        """
        Persist only compact audit data when a writable memory dict
        exists. This avoids storing page content or credentials.
        """
        memory = getattr(self, "v61_memory", None)

        if not isinstance(memory, dict):
            return

        audits = memory.setdefault(
            "decision_audits",
            []
        )

        audit = getattr(
            self,
            "v67_audit",
            None
        )

        if isinstance(audit, dict):
            audits.append(audit)

        # Bound memory growth.
        if len(audits) > 50:
            del audits[:-50]



    # ========================================================
    # V6.8 EVIDENCE GRAPH
    # ========================================================
    #
    # V6.8 connects QA evidence instead of keeping isolated
    # counters:
    #
    # PAGE -> BEHAVIOR -> ACTION -> OBSERVATION -> OUTCOME
    #   -> FINDING -> INVESTIGATION -> VERDICT
    #   -> MEMORY -> DECISION
    #
    # This layer is intentionally lightweight and bounded.
    # It does not store credentials or page secrets.
    # The deterministic regression engine remains untouched.
    # ========================================================

    def _v68_graph(self):
        graph = getattr(self, "v68_evidence_graph", None)
        if not isinstance(graph, dict):
            graph = {
                "version": "6.8",
                "nodes": {},
                "edges": [],
                "decision_history": [],
            }
            self.v68_evidence_graph = graph
        return graph

    def _v68_node_id(self, kind, value):
        import hashlib
        raw = f"{kind}:{value}".encode("utf-8", errors="ignore")
        return f"{kind}:{hashlib.sha1(raw).hexdigest()[:16]}"

    def _v68_add_node(self, kind, value, **data):
        graph = self._v68_graph()
        node_id = self._v68_node_id(kind, value)

        node = graph["nodes"].setdefault(
            node_id,
            {
                "id": node_id,
                "kind": kind,
                "value": str(value),
            },
        )
        node.update({
            k: v for k, v in data.items()
            if v is not None
        })

        return node_id

    def _v68_add_edge(self, source, relation, target):
        graph = self._v68_graph()
        edge = {
            "source": source,
            "relation": relation,
            "target": target,
        }

        if edge not in graph["edges"]:
            graph["edges"].append(edge)

        # Keep the graph bounded.
        if len(graph["edges"]) > 5000:
            del graph["edges"][:-5000]

    def _v68_link(self, source_kind, source_value,
                  relation, target_kind, target_value):
        source = self._v68_add_node(
            source_kind,
            source_value
        )
        target = self._v68_add_node(
            target_kind,
            target_value
        )
        self._v68_add_edge(
            source,
            relation,
            target
        )
        return source, target

    def _v68_record_decision(self, plan):
        if not plan:
            return

        top = plan[0]

        url = str(top.get("url", ""))
        action = str(
            top.get(
                "next_action",
                top.get(
                    "next_behavior",
                    "unknown"
                )
            )
        )
        goal = str(
            top.get(
                "goal",
                "discover application behavior"
            )
        )

        page_id = self._v68_add_node(
            "page",
            url
        )
        behavior_id = self._v68_add_node(
            "behavior",
            action
        )
        goal_id = self._v68_add_node(
            "goal",
            goal
        )
        decision_id = self._v68_add_node(
            "decision",
            f"{url}|{action}|{len(self._v68_graph()['edges'])}",
            score=top.get(
                "v66_score",
                top.get(
                    "v65_score",
                    top.get("score", 0)
                )
            ),
            decision=top.get("decision"),
        )

        self._v68_add_edge(
            page_id,
            "has_goal",
            goal_id
        )
        self._v68_add_edge(
            page_id,
            "has_behavior_gap",
            behavior_id
        )
        self._v68_add_edge(
            decision_id,
            "targets",
            behavior_id
        )
        self._v68_add_edge(
            decision_id,
            "for_page",
            page_id
        )

        graph = self._v68_graph()
        graph["decision_history"].append({
            "page": url,
            "goal": goal,
            "action": action,
            "score": float(
                top.get(
                    "v66_score",
                    top.get(
                        "v65_score",
                        top.get("score", 0)
                    )
                )
            ),
        })

        if len(graph["decision_history"]) > 100:
            del graph["decision_history"][:-100]

    def _v68_record_finding(
        self,
        url,
        finding,
        outcome=None,
        evidence=None,
    ):
        finding_text = str(finding)

        page_id, finding_id = self._v68_link(
            "page",
            url,
            "has_finding",
            "finding",
            finding_text,
        )

        if outcome is not None:
            outcome_id = self._v68_add_node(
                "outcome",
                str(outcome)
            )
            self._v68_add_edge(
                finding_id,
                "resulted_in",
                outcome_id
            )

        if evidence:
            evidence_id = self._v68_add_node(
                "evidence",
                str(evidence)
            )
            self._v68_add_edge(
                finding_id,
                "supported_by",
                evidence_id
            )

        return finding_id

    def _v68_record_verdict(
        self,
        url,
        finding,
        verdict,
    ):
        finding_id = self._v68_add_node(
            "finding",
            str(finding)
        )
        verdict_id = self._v68_add_node(
            "verdict",
            str(verdict)
        )

        page_id = self._v68_add_node(
            "page",
            url
        )

        self._v68_add_edge(
            page_id,
            "has_finding",
            finding_id
        )
        self._v68_add_edge(
            finding_id,
            "has_verdict",
            verdict_id
        )

    def _v68_build_summary(self):
        graph = self._v68_graph()

        counts = {}

        for node in graph["nodes"].values():
            kind = node.get("kind", "unknown")
            counts[kind] = counts.get(kind, 0) + 1

        return {
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "node_types": counts,
            "decisions": len(
                graph["decision_history"]
            ),
        }

    def _v68_print_graph_summary(self):
        summary = self._v68_build_summary()

        print("\n🕸️ V6.8 EVIDENCE GRAPH")

        print(
            f"   Nodes    : {summary['nodes']}"
        )
        print(
            f"   Edges    : {summary['edges']}"
        )
        print(
            f"   Decisions: {summary['decisions']}"
        )

        if summary["node_types"]:
            print("   Evidence types:")

            for kind, count in sorted(
                summary["node_types"].items()
            ):
                print(
                    f"      • {kind}: {count}"
                )

    def _v68_explain_current_decision(self):
        plan = getattr(
            self,
            "v66_closed_loop_plan",
            None
        ) or getattr(
            self,
            "v65_reasoned_plan",
            []
        )

        if not plan:
            return

        top = plan[0]

        print("\n🔎 V6.8 DECISION TRACE")
        print(
            f"   PAGE     : {top.get('url', '')}"
        )
        print(
            f"   GOAL     : "
            f"{top.get('goal', 'unknown')}"
        )
        print(
            f"   BEHAVIOR : "
            f"{top.get('next_action', top.get('next_behavior', 'unknown'))}"
        )
        print(
            f"   SCORE    : "
            f"{top.get('v66_score', top.get('v65_score', top.get('score', 0))):.2f}"
        )
        print(
            f"   REASON   : "
            f"{top.get('decision_reason', 'evidence-based selection')}"
        )

    def _v68_replan(self):
        # Use the proven V6.6 closed-loop planner.
        plan = self._v66_replan()

        if plan:
            self._v68_record_decision(plan)

        self._v68_explain_current_decision()
        self._v68_print_graph_summary()

        return plan



    # ========================================================
    # V6.9 AUTONOMOUS QA COMMAND CENTER
    # ========================================================
    #
    # V6.9 turns the evidence graph into an auditable QA view:
    #
    #   WHAT WAS TESTED?
    #   WHAT WAS LEARNED?
    #   WHY THIS NEXT TEST?
    #   WHAT REMAINS UNKNOWN?
    #
    # This is an observability/decision layer. It does not alter
    # the deterministic regression execution engine.
    # ========================================================

    def _v69_command_center(self):
        matrix = getattr(
            self,
            "v65_coverage_matrix",
            []
        ) or []

        plan = getattr(
            self,
            "v66_closed_loop_plan",
            []
        ) or []

        graph_summary = (
            self._v68_build_summary()
            if hasattr(self, "_v68_build_summary")
            else {}
        )

        return {
            "coverage": matrix,
            "decision_plan": plan,
            "graph": graph_summary,
        }

    def _v69_find_item(self, url):
        center = self._v69_command_center()

        for item in center["coverage"]:
            if item.get("url") == url:
                return item

        return None

    def _v69_remaining_behaviors(self, url):
        item = self._v69_find_item(url)

        if not item:
            return []

        return list(
            item.get("unknown", [])
        )

    def _v69_decision_explanation(self):
        plan = getattr(
            self,
            "v66_closed_loop_plan",
            []
        ) or []

        if not plan:
            return None

        top = plan[0]

        remaining = list(
            top.get("unknown_behaviors", [])
        )

        return {
            "target": top.get("url", ""),
            "goal": top.get(
                "goal",
                "Discover application behavior"
            ),
            "behavior": top.get(
                "next_action",
                top.get(
                    "next_behavior",
                    "unknown"
                )
            ),
            "score": float(
                top.get(
                    "v66_score",
                    top.get(
                        "v65_score",
                        top.get("score", 0)
                    )
                )
            ),
            "reason": top.get(
                "decision_reason",
                "evidence-based selection"
            ),
            "coverage": float(
                top.get(
                    "behavioral_coverage",
                    0
                )
            ),
            "remaining": remaining,
        }

    def _v69_print_command_center(self):
        center = self._v69_command_center()
        decision = self._v69_decision_explanation()

        print("\n")
        print("=" * 70)
        print("🧭 V6.9 AUTONOMOUS QA COMMAND CENTER")
        print("=" * 70)

        print("\n📊 WHAT WAS TESTED?")

        print(
            f"   Known pages          : "
            f"{len(center['coverage'])}"
        )

        total_expected = sum(
            int(x.get("total_behavior_count", 0))
            for x in center["coverage"]
        )

        total_effective = sum(
            int(x.get("effective_behavior_count", 0))
            for x in center["coverage"]
        )

        total_unknown = sum(
            len(x.get("unknown", []))
            for x in center["coverage"]
        )

        print(
            f"   Behavioral surfaces  : "
            f"{total_expected}"
        )
        print(
            f"   Covered behaviors    : "
            f"{total_effective}"
        )
        print(
            f"   Unknown behaviors    : "
            f"{total_unknown}"
        )

        print("\n🧠 WHAT DID WE LEARN?")

        audits = getattr(
            self,
            "v67_audit",
            {}
        ) or {}

        print(
            f"   Decision changed     : "
            f"{'YES' if audits.get('decision_changed') else 'NO'}"
        )

        print(
            f"   Learning deltas      : "
            f"{len(audits.get('learning_deltas', []))}"
        )

        graph = center["graph"]

        print(
            f"   Evidence graph nodes : "
            f"{graph.get('nodes', 0)}"
        )
        print(
            f"   Evidence graph edges : "
            f"{graph.get('edges', 0)}"
        )

        print("\n🎯 WHY THIS NEXT TEST?")

        if decision:
            print(
                f"   Target   : "
                f"{decision['target']}"
            )
            print(
                f"   Goal     : "
                f"{decision['goal']}"
            )
            print(
                f"   Behavior : "
                f"{decision['behavior']}"
            )
            print(
                f"   Score    : "
                f"{decision['score']:.2f}"
            )
            print(
                f"   Coverage : "
                f"{decision['coverage']:.1f}%"
            )
            print(
                f"   Reason   : "
                f"{decision['reason']}"
            )
        else:
            print("   No decision target available.")

        print("\n❓ WHAT REMAINS UNKNOWN?")

        if decision and decision["remaining"]:
            for behavior in decision["remaining"][:10]:
                print(
                    f"   • {behavior}"
                )
        else:
            print(
                "   No unknown behavior recorded "
                "for the selected target."
            )

        print("\n🕸️ EVIDENCE GRAPH")

        if graph:
            print(
                f"   Nodes    : {graph.get('nodes', 0)}"
            )
            print(
                f"   Edges    : {graph.get('edges', 0)}"
            )
            print(
                f"   Decisions: {graph.get('decisions', 0)}"
            )

        print("=" * 70)

    def _v69_write_dashboard_artifact(self):
        """
        Write a JSON command-center artifact if the agent already has
        a report directory configured. No credentials or page secrets
        are included.
        """
        import json
        from pathlib import Path

        candidates = []

        for attr in (
            "report_dir",
            "output_dir",
            "report_path",
        ):
            value = getattr(self, attr, None)
            if value:
                candidates.append(Path(str(value)))

        if not candidates:
            return

        out_dir = candidates[0]
        if out_dir.suffix:
            out_dir = out_dir.parent

        try:
            out_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            payload = {
                "version": "6.9",
                "command_center":
                    self._v69_command_center(),
                "decision":
                    self._v69_decision_explanation(),
                "learning_audit":
                    getattr(
                        self,
                        "v67_audit",
                        {}
                    ),
                "evidence_graph":
                    getattr(
                        self,
                        "v68_evidence_graph",
                        {}
                    ),
            }

            path = (
                out_dir
                / "qa_command_center_v6_9.json"
            )

            path.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    default=str
                ),
                encoding="utf-8"
            )

            print(
                f"\n📄 Command center artifact: "
                f"{path}"
            )

        except Exception as exc:
            # Observability must never fail the QA run.
            print(
                f"\n⚠️ Command-center artifact "
                f"could not be written: {exc}"
            )

    def _v69_run_command_center(self):
        self._v69_print_command_center()
        self._v69_write_dashboard_artifact()



    # ========================================================
    # V7.0 MISSION-DRIVEN AUTONOMOUS QA
    # ========================================================
    #
    # V7.0 adds a mission layer above the proven V6.x engine.
    #
    #   MISSION
    #      ↓
    #   GOALS
    #      ↓
    #   COVERAGE / RISK
    #      ↓
    #   NEXT BEST ACTION
    #      ↓
    #   EXECUTE
    #      ↓
    #   EVIDENCE / INVESTIGATION
    #      ↓
    #   LEARN
    #      ↓
    #   REPLAN
    #      ↓
    #   COMPLETION / STOP
    #
    # This layer does not replace deterministic regression.
    # It decides what exploratory work remains and when the
    # mission has reached its stopping criteria.
    # ========================================================

    def _v70_mission(self):
        mission = getattr(self, "v70_mission_state", None)

        if not isinstance(mission, dict):
            mission = {
                "version": "7.0",
                "name": "Maximize meaningful behavioral coverage",
                "status": "IN_PROGRESS",
                "goals": [
                    "execute deterministic regression",
                    "increase behavioral coverage",
                    "investigate anomalies",
                    "avoid repeated false-positive work",
                    "prioritize high-risk unknown behavior",
                ],
                "runs": 0,
                "actions": 0,
                "decisions": 0,
                "completed_goals": [],
                "stop_reason": None,
            }
            self.v70_mission_state = mission

        return mission

    def _v70_coverage_totals(self):
        matrix = getattr(
            self,
            "v65_coverage_matrix",
            []
        ) or []

        total = sum(
            int(x.get("total_behavior_count", 0))
            for x in matrix
        )

        covered = sum(
            int(x.get("effective_behavior_count", 0))
            for x in matrix
        )

        unknown = sum(
            len(x.get("unknown", []))
            for x in matrix
        )

        critical_unknown = 0

        for x in matrix:
            if (
                float(
                    x.get(
                        "coverage_percent",
                        100
                    )
                ) < 50
            ):
                critical_unknown += len(
                    x.get("unknown", [])
                )

        percentage = (
            round(
                covered / total * 100.0,
                1
            )
            if total
            else 100.0
        )

        return {
            "total": total,
            "covered": covered,
            "unknown": unknown,
            "critical_unknown": critical_unknown,
            "percentage": percentage,
        }

    def _v70_goal_status(self):
        mission = self._v70_mission()
        coverage = self._v70_coverage_totals()

        confirmed = int(
            getattr(
                self,
                "confirmed_defects",
                0
            ) or 0
        )

        investigated = int(
            getattr(
                self,
                "investigated",
                0
            ) or 0
        )

        # A mission is not considered complete while there are
        # confirmed defects or high-value unknown behavior.
        if confirmed > 0:
            return {
                "status": "BLOCKED",
                "reason": "confirmed defects require resolution",
                "coverage": coverage,
            }

        if coverage["critical_unknown"] > 0:
            return {
                "status": "IN_PROGRESS",
                "reason": "high-value behavioral gaps remain",
                "coverage": coverage,
            }

        if coverage["unknown"] > 0:
            return {
                "status": "IN_PROGRESS",
                "reason": "behavioral gaps remain",
                "coverage": coverage,
            }

        mission["completed_goals"] = list(
            dict.fromkeys(
                mission.get("goals", [])
            )
        )

        return {
            "status": "COMPLETE",
            "reason": "no meaningful unknown behavior remains",
            "coverage": coverage,
        }

    def _v70_next_mission_action(self):
        plan = getattr(
            self,
            "v66_closed_loop_plan",
            []
        ) or []

        candidates = []

        for item in plan:
            unknown = list(
                item.get(
                    "unknown_behaviors",
                    []
                )
            )

            if not unknown:
                continue

            score = float(
                item.get(
                    "v66_score",
                    item.get(
                        "v65_score",
                        item.get("score", 0)
                    )
                )
            )

            gap = len(unknown)

            # Prefer a high-risk page with a meaningful behavioral gap.
            mission_score = round(
                score + min(gap * 6.0, 36.0),
                2
            )

            candidates.append({
                "url": item.get("url", ""),
                "goal": item.get(
                    "goal",
                    "Discover unknown behavior"
                ),
                "behavior": item.get(
                    "next_action",
                    item.get(
                        "next_behavior",
                        unknown[0]
                    )
                ),
                "unknown_count": gap,
                "coverage": float(
                    item.get(
                        "behavioral_coverage",
                        0
                    )
                ),
                "score": mission_score,
            })

        candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return candidates[0] if candidates else None

    def _v70_update_mission(self):
        mission = self._v70_mission()
        status = self._v70_goal_status()
        coverage = status["coverage"]

        mission["runs"] = int(
            mission.get("runs", 0)
        ) + 1

        next_action = self._v70_next_mission_action()

        if next_action:
            mission["decisions"] = int(
                mission.get("decisions", 0)
            ) + 1

        mission["status"] = status["status"]
        mission["stop_reason"] = (
            status["reason"]
            if status["status"] == "COMPLETE"
            else None
        )

        mission["coverage"] = coverage

        if next_action:
            mission["next_action"] = next_action
        else:
            mission["next_action"] = None

        return mission

    def _v70_print_mission(self):
        mission = self._v70_update_mission()
        coverage = mission.get(
            "coverage",
            {}
        )

        print("\n")
        print("=" * 70)
        print("🎯 V7.0 AUTONOMOUS QA MISSION")
        print("=" * 70)

        print(
            f"\nMission : "
            f"{mission.get('name', 'QA mission')}"
        )

        print(
            f"Status  : "
            f"{mission.get('status', 'UNKNOWN')}"
        )

        print("\n📈 MISSION PROGRESS")
        print(
            f"   Behavioral coverage : "
            f"{coverage.get('percentage', 0):.1f}%"
        )
        print(
            f"   Behaviors total     : "
            f"{coverage.get('total', 0)}"
        )
        print(
            f"   Covered             : "
            f"{coverage.get('covered', 0)}"
        )
        print(
            f"   Unknown             : "
            f"{coverage.get('unknown', 0)}"
        )
        print(
            f"   High-value unknown  : "
            f"{coverage.get('critical_unknown', 0)}"
        )

        print("\n🎯 MISSION GOALS")

        for goal in mission.get(
            "goals",
            []
        ):
            marker = (
                "✓"
                if goal in mission.get(
                    "completed_goals",
                    []
                )
                else "•"
            )
            print(
                f"   {marker} {goal}"
            )

        next_action = mission.get(
            "next_action"
        )

        print("\n🧠 NEXT BEST MISSION ACTION")

        if next_action:
            print(
                f"   Target   : "
                f"{next_action['url']}"
            )
            print(
                f"   Goal     : "
                f"{next_action['goal']}"
            )
            print(
                f"   Behavior : "
                f"{next_action['behavior']}"
            )
            print(
                f"   Coverage : "
                f"{next_action['coverage']:.1f}%"
            )
            print(
                f"   Score    : "
                f"{next_action['score']:.2f}"
            )
            print(
                f"   Unknown  : "
                f"{next_action['unknown_count']}"
            )
        else:
            print(
                "   No exploratory action remains."
            )

        if mission.get("status") == "COMPLETE":
            print("\n🏁 MISSION COMPLETE")
            print(
                f"   Stop reason: "
                f"{mission.get('stop_reason')}"
            )

        elif mission.get("status") == "BLOCKED":
            print("\n🚨 MISSION BLOCKED")
            print(
                f"   Reason: "
                f"{self._v70_goal_status().get('reason')}"
            )

        print("=" * 70)

    def _v70_write_mission_artifact(self):
        import json
        from pathlib import Path

        candidates = []

        for attr in (
            "report_dir",
            "output_dir",
            "report_path",
        ):
            value = getattr(self, attr, None)
            if value:
                candidates.append(
                    Path(str(value))
                )

        if not candidates:
            return

        out_dir = candidates[0]

        if out_dir.suffix:
            out_dir = out_dir.parent

        try:
            out_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            payload = {
                "version": "7.0",
                "mission": self._v70_mission(),
                "coverage": self._v70_coverage_totals(),
                "next_action":
                    self._v70_next_mission_action(),
                "decision_audit":
                    getattr(
                        self,
                        "v67_audit",
                        {}
                    ),
                "evidence_graph_summary":
                    (
                        self._v68_build_summary()
                        if hasattr(
                            self,
                            "_v68_build_summary"
                        )
                        else {}
                    ),
            }

            path = (
                out_dir
                / "qa_mission_v7_0.json"
            )

            path.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    default=str
                ),
                encoding="utf-8"
            )

            print(
                f"\n📄 Mission artifact: {path}"
            )

        except Exception as exc:
            # Mission observability must never fail QA execution.
            print(
                f"\n⚠️ Mission artifact could not "
                f"be written: {exc}"
            )

    def _v70_run_mission(self):
        self._v70_print_mission()
        self._v70_write_mission_artifact()



    # ========================================================
    # V7.1 RISK CLOSURE ENGINE
    # ========================================================
    #
    # V7.1 distinguishes:
    #   PASSING TESTS != CLOSED RISK
    #
    # It calculates:
    #   - risk remaining
    #   - risk retired
    #   - unknown behavior
    #   - uninvestigated anomalies
    #   - confirmed defects
    #   - mission completion percentage
    #
    # It never declares the mission complete merely because the
    # deterministic regression suite passed.
    # ========================================================

    def _v71_risk_state(self):
        coverage = (
            self._v70_coverage_totals()
            if hasattr(self, "_v70_coverage_totals")
            else {
                "total": 0,
                "covered": 0,
                "unknown": 0,
                "critical_unknown": 0,
                "percentage": 100.0,
            }
        )

        exploratory = int(
            getattr(self, "exploratory_defects", 0) or 0
        )
        confirmed = int(
            getattr(self, "confirmed_defects", 0) or 0
        )
        investigated = int(
            getattr(self, "investigated", 0) or 0
        )

        # Findings are anomalies discovered during exploration.
        # Unresolved findings are conservatively treated as open risk.
        findings = int(
            getattr(self, "findings", 0) or 0
        )

        not_reproduced = max(
            0,
            investigated - confirmed
        )

        open_anomalies = max(
            0,
            findings - not_reproduced - confirmed
        )

        # Risk units are deliberately transparent rather than pretending
        # to be a probability. Higher score = more unresolved risk.
        risk_unknown = float(
            coverage.get("unknown", 0)
        )
        risk_critical = float(
            coverage.get("critical_unknown", 0) * 3
        )
        risk_confirmed = float(
            confirmed * 10
        )
        risk_anomalies = float(
            open_anomalies * 5
        )

        risk_remaining = round(
            risk_unknown
            + risk_critical
            + risk_confirmed
            + risk_anomalies,
            2
        )

        baseline_risk = max(
            1.0,
            float(coverage.get("total", 0))
            + float(exploratory)
        )

        risk_retired = round(
            max(
                0.0,
                baseline_risk - risk_remaining
            ),
            2
        )

        risk_retirement_pct = round(
            min(
                100.0,
                risk_retired / baseline_risk * 100.0
            ),
            1
        )

        mission_completion = round(
            min(
                100.0,
                coverage.get("percentage", 0)
                * 0.70
                + risk_retirement_pct * 0.30
            ),
            1
        )

        return {
            "risk_remaining": risk_remaining,
            "risk_retired": risk_retired,
            "risk_retirement_percent": risk_retirement_pct,
            "unknown_behavior": int(
                coverage.get("unknown", 0)
            ),
            "high_value_unknown": int(
                coverage.get("critical_unknown", 0)
            ),
            "uninvestigated_anomalies": int(
                open_anomalies
            ),
            "confirmed_defects": int(
                confirmed
            ),
            "investigated_findings": int(
                investigated
            ),
            "not_reproduced": int(
                not_reproduced
            ),
            "mission_completion_percent":
                mission_completion,
            "baseline_risk": baseline_risk,
        }

    def _v71_completion_decision(self):
        state = self._v71_risk_state()

        if state["confirmed_defects"] > 0:
            return (
                "BLOCKED",
                "confirmed defects remain"
            )

        if state["uninvestigated_anomalies"] > 0:
            return (
                "IN_PROGRESS",
                "uninvestigated anomalies remain"
            )

        if state["high_value_unknown"] > 0:
            return (
                "IN_PROGRESS",
                "high-value unknown behavior remains"
            )

        if state["unknown_behavior"] > 0:
            return (
                "IN_PROGRESS",
                "unknown behavior remains"
            )

        return (
            "COMPLETE",
            "no unresolved meaningful risk remains"
        )

    def _v71_next_risk_action(self):
        plan = getattr(
            self,
            "v66_closed_loop_plan",
            []
        ) or []

        candidates = []

        for item in plan:
            unknown = list(
                item.get(
                    "unknown_behaviors",
                    []
                )
            )

            if not unknown:
                continue

            score = float(
                item.get(
                    "v66_score",
                    item.get(
                        "v65_score",
                        item.get("score", 0)
                    )
                )
            )

            coverage = float(
                item.get(
                    "behavioral_coverage",
                    0
                )
            )

            # Risk priority favors high score, low coverage and a
            # larger remaining behavioral gap.
            risk_score = round(
                score
                + max(0.0, 100.0 - coverage) * 0.50
                + min(len(unknown) * 4.0, 40.0),
                2
            )

            candidates.append({
                "url": item.get("url", ""),
                "behavior": item.get(
                    "next_action",
                    item.get(
                        "next_behavior",
                        unknown[0]
                    )
                ),
                "goal": item.get(
                    "goal",
                    "Close remaining behavioral risk"
                ),
                "coverage": coverage,
                "unknown": len(unknown),
                "risk_score": risk_score,
            })

        candidates.sort(
            key=lambda x: x["risk_score"],
            reverse=True
        )

        return candidates[0] if candidates else None

    def _v71_print_risk_closure(self):
        state = self._v71_risk_state()
        status, reason = self._v71_completion_decision()
        next_action = self._v71_next_risk_action()

        print("\n")
        print("=" * 70)
        print("🛡️ V7.1 RISK-CLOSURE CONTROL CENTER")
        print("=" * 70)

        print("\n📉 RISK STATUS")
        print(
            f"   Risk remaining       : "
            f"{state['risk_remaining']:.2f}"
        )
        print(
            f"   Risk retired         : "
            f"{state['risk_retired']:.2f}"
        )
        print(
            f"   Risk retired %       : "
            f"{state['risk_retirement_percent']:.1f}%"
        )

        print("\n🔍 RISK BREAKDOWN")
        print(
            f"   Unknown behavior     : "
            f"{state['unknown_behavior']}"
        )
        print(
            f"   High-value unknown   : "
            f"{state['high_value_unknown']}"
        )
        print(
            f"   Uninvestigated       : "
            f"{state['uninvestigated_anomalies']}"
        )
        print(
            f"   Confirmed defects    : "
            f"{state['confirmed_defects']}"
        )
        print(
            f"   Investigated         : "
            f"{state['investigated_findings']}"
        )
        print(
            f"   Not reproduced       : "
            f"{state['not_reproduced']}"
        )

        print("\n🎯 MISSION COMPLETION")
        print(
            f"   Completion           : "
            f"{state['mission_completion_percent']:.1f}%"
        )
        print(
            f"   Decision             : "
            f"{status}"
        )
        print(
            f"   Reason               : "
            f"{reason}"
        )

        print("\n🧠 NEXT RISK-CLOSURE ACTION")

        if next_action:
            print(
                f"   Target   : "
                f"{next_action['url']}"
            )
            print(
                f"   Goal     : "
                f"{next_action['goal']}"
            )
            print(
                f"   Behavior : "
                f"{next_action['behavior']}"
            )
            print(
                f"   Coverage : "
                f"{next_action['coverage']:.1f}%"
            )
            print(
                f"   Unknown  : "
                f"{next_action['unknown']}"
            )
            print(
                f"   Risk     : "
                f"{next_action['risk_score']:.2f}"
            )
        else:
            print(
                "   No remaining behavioral "
                "risk action selected."
            )

        if status == "COMPLETE":
            print("\n🏁 MISSION COMPLETE")
            print(
                "   STOP: risk closure criteria satisfied."
            )
        elif status == "BLOCKED":
            print("\n🚨 MISSION BLOCKED")
        else:
            print("\n🔄 MISSION IN PROGRESS")

        print("=" * 70)

        self.v71_risk_state = state
        self.v71_mission_status = status
        self.v71_mission_reason = reason
        self.v71_next_action = next_action

    def _v71_write_risk_artifact(self):
        import json
        from pathlib import Path

        candidates = []

        for attr in (
            "report_dir",
            "output_dir",
            "report_path",
        ):
            value = getattr(self, attr, None)
            if value:
                candidates.append(
                    Path(str(value))
                )

        if not candidates:
            return

        out_dir = candidates[0]

        if out_dir.suffix:
            out_dir = out_dir.parent

        try:
            out_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            payload = {
                "version": "7.1",
                "risk": self._v71_risk_state(),
                "mission_status":
                    self._v71_completion_decision()[0],
                "mission_reason":
                    self._v71_completion_decision()[1],
                "next_risk_action":
                    self._v71_next_risk_action(),
                "confirmed_defects":
                    int(
                        getattr(
                            self,
                            "confirmed_defects",
                            0
                        ) or 0
                    ),
                "credentials_stored": False,
            }

            path = (
                out_dir
                / "qa_risk_closure_v7_1.json"
            )

            path.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    default=str
                ),
                encoding="utf-8"
            )

            print(
                f"\n📄 Risk-closure artifact: {path}"
            )

        except Exception as exc:
            print(
                f"\n⚠️ Risk artifact could not "
                f"be written: {exc}"
            )

    def _v71_run_risk_closure(self):
        self._v71_print_risk_closure()
        self._v71_write_risk_artifact()



    # ========================================================
    # V7.2 FALSE-POSITIVE SUPPRESSION + RISK CALIBRATION
    # ========================================================
    #
    # V7.2 learns from investigated exploratory anomalies.
    #
    # Repeated non-reproduction is evidence. It must reduce the
    # likelihood of repeatedly selecting the same noisy pattern,
    # while NEVER suppressing a pattern that later becomes
    # reproducible or receives materially different evidence.
    #
    # Classification:
    #   known benign pattern -> suppress/deprioritize
    #   transient            -> retry
    #   ambiguous             -> investigate
    #   reproducible          -> confirmed defect
    #
    # Credentials are never persisted by this layer.
    # ========================================================

    def _v72_calibration(self):
        state = getattr(self, "v72_calibration_state", None)

        if not isinstance(state, dict):
            state = {
                "version": "7.2",
                "patterns": {},
                "total_findings": 0,
                "false_positives": 0,
                "confirmed_defects": 0,
                "suppressed_patterns": 0,
            }
            self.v72_calibration_state = state

        return state

    def _v72_pattern_key(self, candidate):
        import hashlib

        if isinstance(candidate, dict):
            semantic = str(
                candidate.get("semantic", "")
            )
            reason = str(
                candidate.get("reason", "")
            )
            url = str(
                candidate.get("url", "")
            )
            label = str(
                candidate.get("label", "")
            )
        else:
            semantic = ""
            reason = str(candidate)
            url = ""
            label = ""

        # Do not use screenshot paths, credentials, or volatile IDs.
        raw = "|".join(
            [url, semantic, label, reason]
        )

        return hashlib.sha1(
            raw.encode(
                "utf-8",
                errors="ignore"
            )
        ).hexdigest()[:20]

    def _v72_record_investigation(
        self,
        candidate,
        reproduced=False,
        confidence=0.0,
    ):
        state = self._v72_calibration()
        key = self._v72_pattern_key(candidate)

        pattern = state["patterns"].setdefault(
            key,
            {
                "key": key,
                "url": str(
                    candidate.get("url", "")
                    if isinstance(candidate, dict)
                    else ""
                ),
                "semantic": str(
                    candidate.get("semantic", "")
                    if isinstance(candidate, dict)
                    else ""
                ),
                "label": str(
                    candidate.get("label", "")
                    if isinstance(candidate, dict)
                    else ""
                ),
                "investigations": 0,
                "not_reproduced": 0,
                "reproduced": 0,
                "confidence": 0.0,
                "suppressed": False,
            },
        )

        pattern["investigations"] += 1

        if reproduced:
            pattern["reproduced"] += 1
        else:
            pattern["not_reproduced"] += 1

        # Reproduction evidence always dominates suppression.
        if pattern["reproduced"] > 0:
            pattern["suppressed"] = False
            pattern["confidence"] = round(
                min(
                    1.0,
                    (
                        pattern["reproduced"]
                        /
                        max(
                            1,
                            pattern["investigations"]
                        )
                    )
                    + float(confidence) * 0.25
                ),
                3
            )
        else:
            fp_ratio = (
                pattern["not_reproduced"]
                /
                max(
                    1,
                    pattern["investigations"]
                )
            )

            pattern["confidence"] = round(
                fp_ratio,
                3
            )

            # Suppress only after repeated independent
            # non-reproduction, never after a single observation.
            pattern["suppressed"] = (
                pattern["not_reproduced"] >= 2
                and fp_ratio >= 0.80
            )

        state["total_findings"] += 1

        if reproduced:
            state["confirmed_defects"] += 1
        else:
            state["false_positives"] += 1

        state["suppressed_patterns"] = sum(
            1
            for p in state["patterns"].values()
            if p.get("suppressed")
        )

        return pattern

    def _v72_should_suppress(self, candidate):
        state = self._v72_calibration()
        key = self._v72_pattern_key(candidate)
        pattern = state["patterns"].get(key)

        if not pattern:
            return False

        # Never suppress a previously reproduced pattern.
        if pattern.get("reproduced", 0) > 0:
            return False

        return bool(
            pattern.get("suppressed", False)
        )

    def _v72_calibration_metrics(self):
        state = self._v72_calibration()

        total = int(
            state.get("total_findings", 0)
        )
        false_positives = int(
            state.get("false_positives", 0)
        )
        confirmed = int(
            state.get("confirmed_defects", 0)
        )
        suppressed = int(
            state.get("suppressed_patterns", 0)
        )

        fp_rate = round(
            false_positives / max(1, total) * 100.0,
            1
        )

        reproduction_rate = round(
            confirmed / max(1, total) * 100.0,
            1
        )

        # Calibration quality rises when investigations become
        # more discriminating. This is a metric, not a probability.
        calibration = round(
            max(
                0.0,
                min(
                    100.0,
                    100.0 - fp_rate
                )
            ),
            1
        )

        return {
            "findings": total,
            "false_positives": false_positives,
            "confirmed_defects": confirmed,
            "suppressed_patterns": suppressed,
            "false_positive_rate": fp_rate,
            "reproduction_rate": reproduction_rate,
            "calibration_score": calibration,
        }

    def _v72_filter_candidates(self, candidates):
        if not candidates:
            return []

        filtered = []
        suppressed = []

        for candidate in candidates:
            if self._v72_should_suppress(candidate):
                suppressed.append(candidate)
            else:
                filtered.append(candidate)

        # If everything is suppressed, retain the highest-risk item
        # as a safety valve. Suppression is never absolute.
        if not filtered and suppressed:
            suppressed.sort(
                key=lambda x: float(
                    x.get(
                        "risk_score",
                        x.get(
                            "score",
                            0
                        )
                    )
                ),
                reverse=True
            )
            filtered = suppressed[:1]

        return filtered

    def _v72_print_calibration(self):
        metrics = self._v72_calibration_metrics()

        print("\n")
        print("=" * 70)
        print("🎯 V7.2 FALSE-POSITIVE CALIBRATION")
        print("=" * 70)

        print(
            f"\n   Exploratory findings : "
            f"{metrics['findings']}"
        )
        print(
            f"   False positives      : "
            f"{metrics['false_positives']}"
        )
        print(
            f"   Confirmed defects    : "
            f"{metrics['confirmed_defects']}"
        )
        print(
            f"   Suppressed patterns : "
            f"{metrics['suppressed_patterns']}"
        )
        print(
            f"   FP rate              : "
            f"{metrics['false_positive_rate']:.1f}%"
        )
        print(
            f"   Reproduction rate    : "
            f"{metrics['reproduction_rate']:.1f}%"
        )
        print(
            f"   Calibration score    : "
            f"{metrics['calibration_score']:.1f}%"
        )

        state = self._v72_calibration()

        if state.get("patterns"):
            print("\n🧠 LEARNED PATTERNS")

            for pattern in list(
                state["patterns"].values()
            )[-10:]:
                status = (
                    "SUPPRESSED"
                    if pattern.get("suppressed")
                    else "ACTIVE"
                )

                print(
                    f"   • {pattern.get('url', '')} "
                    f"| {pattern.get('semantic', '')} "
                    f"| {status} "
                    f"| NR={pattern.get('not_reproduced', 0)} "
                    f"R={pattern.get('reproduced', 0)}"
                )

        print("=" * 70)

        self.v72_metrics = metrics

    def _v72_write_calibration_artifact(self):
        import json
        from pathlib import Path

        candidates = []

        for attr in (
            "report_dir",
            "output_dir",
            "report_path",
        ):
            value = getattr(self, attr, None)
            if value:
                candidates.append(
                    Path(str(value))
                )

        if not candidates:
            return

        out_dir = candidates[0]

        if out_dir.suffix:
            out_dir = out_dir.parent

        try:
            out_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            state = self._v72_calibration()

            payload = {
                "version": "7.2",
                "metrics":
                    self._v72_calibration_metrics(),
                "patterns":
                    state.get("patterns", {}),
                "credentials_stored": False,
            }

            path = (
                out_dir
                / "qa_false_positive_calibration_v7_2.json"
            )

            path.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    default=str
                ),
                encoding="utf-8"
            )

            print(
                f"\n📄 Calibration artifact: {path}"
            )

        except Exception as exc:
            print(
                f"\n⚠️ Calibration artifact could not "
                f"be written: {exc}"
            )

    def _v72_run_calibration(self):
        self._v72_print_calibration()
        self._v72_write_calibration_artifact()



    # ========================================================
    # V7.3 RISK-ADAPTIVE EXPLORATION ENGINE
    # ========================================================
    def _v73_risk_state(self):
        matrix=getattr(self,"v65_coverage_matrix",[]) or []
        total=covered=0
        critical=medium=low=0
        candidates=[]
        for x in matrix:
            n=max(0,int(x.get("total_behavior_count",0)))
            c=max(0,int(x.get("effective_behavior_count",0)))
            u=list(x.get("unknown",[]))
            cov=float(x.get("coverage_percent",0))
            pri=float(x.get("risk_score",x.get("priority_score",x.get("score",0))))
            total+=n; covered+=min(c,n)
            if cov<50: critical+=len(u)
            elif cov<80: medium+=len(u)
            else: low+=len(u)
            if u:
                value=round(pri*.35+max(0,100-cov)*.45+min(len(u)*5,35),2)
                candidates.append({"url":str(x.get("url","")),"coverage":cov,"unknown":len(u),"priority":pri,"expected_value":value,"next_behavior":u[0]})
        candidates.sort(key=lambda z:z["expected_value"],reverse=True)
        risk_cov=round(covered/max(1,total)*100,1)
        return {"behavioral_coverage":risk_cov,"risk_weighted_coverage":risk_cov,"critical_unknown":critical,"medium_unknown":medium,"low_unknown":low,"unknown_total":critical+medium+low,"candidates":candidates}

    def _v73_select_next(self):
        s=self._v73_risk_state(); c=s["candidates"]
        if hasattr(self,"_v72_filter_candidates"):
            try: c=self._v72_filter_candidates(c) or c
            except Exception: pass
        return c[0] if c else None

    def _v73_print_risk_intelligence(self):
        s=self._v73_risk_state(); n=self._v73_select_next()
        print("\n"+"="*70+"\n🧠 V7.3 RISK-ADAPTIVE EXPLORATION\n"+"="*70)
        print("\n📊 RISK-ADJUSTED COVERAGE")
        print(f"   Behavioral coverage : {s['behavioral_coverage']:.1f}%")
        print(f"   Risk-weighted        : {s['risk_weighted_coverage']:.1f}%")
        print("\n🔍 REMAINING UNCERTAINTY")
        print(f"   Critical unknown     : {s['critical_unknown']}")
        print(f"   Medium unknown       : {s['medium_unknown']}")
        print(f"   Low unknown          : {s['low_unknown']}")
        print(f"   Total unknown        : {s['unknown_total']}")
        print("\n🎯 NEXT BEST EXPLORATION")
        if n:
            print(f"   Target       : {n['url']}")
            print(f"   Behavior     : {n['next_behavior']}")
            print(f"   Coverage     : {n['coverage']:.1f}%")
            print(f"   Expected risk reduction : {n['expected_value']:.2f}")
            print("   Reason       : highest expected risk reduction from remaining behavioral uncertainty")
        else: print("   No meaningful exploratory candidate remains.")
        print("\n🏁 EXPLORATION DECISION")
        print("   STOP — no unknown behavioral surface remains." if not s['unknown_total'] else "   CONTINUE — behavioral uncertainty remains.")
        print("="*70)
        self.v73_risk_state=s; self.v73_next_action=n

    def _v73_write_artifact(self):
        import json
        from pathlib import Path
        vals=[getattr(self,a,None) for a in ("report_dir","output_dir","report_path")]
        vals=[Path(str(v)) for v in vals if v]
        if not vals: return
        d=vals[0]; d=d.parent if d.suffix else d
        try:
            d.mkdir(parents=True,exist_ok=True)
            (d/"qa_risk_adaptive_v7_3.json").write_text(json.dumps({"version":"7.3","risk_state":self._v73_risk_state(),"next_action":self._v73_select_next(),"credentials_stored":False},indent=2,default=str),encoding="utf-8")
            print(f"\n📄 Risk-adaptive artifact: {d/'qa_risk_adaptive_v7_3.json'}")
        except Exception as e: print(f"\n⚠️ Risk-adaptive artifact could not be written: {e}")

    def _v73_run_risk_intelligence(self):
        self._v73_print_risk_intelligence(); self._v73_write_artifact()


    # ========================================================
    # V7.4 EVIDENCE-WEIGHTED RISK ENGINE
    # ========================================================
    def _v74_evidence_risk(self, item):
        cal=getattr(self,"v72_calibration_state",{}) or {}
        patterns=cal.get("patterns",{})
        url=str(item.get("url","")); coverage=float(item.get("coverage",0)); gap=int(item.get("unknown",0)); priority=float(item.get("priority",0))
        same=[p for p in patterns.values() if str(p.get("url",""))==url]
        investigations=sum(int(p.get("investigations",0)) for p in same)
        reproduced=sum(int(p.get("reproduced",0)) for p in same)
        not_reproduced=sum(int(p.get("not_reproduced",0)) for p in same)
        evidence=min(100.0,investigations*15.0)
        fp_penalty=min(25.0,(not_reproduced/max(1,investigations))*25.0)
        reproduction_conf=reproduced/max(1,investigations)*100.0
        criticality=min(100.0,max(0.0,priority*4.0))
        coverage_gap=min(100.0,max(0.0,100.0-coverage))
        behavior_gap=min(100.0,gap*12.0)
        consequence=min(100.0,25.0+behavior_gap*.35+criticality*.25)
        risk_before=max(0.0,min(100.0,criticality*.25+coverage_gap*.30+behavior_gap*.20+consequence*.15+evidence*.10-fp_penalty))
        expected=min(risk_before*.80,coverage_gap*.35+behavior_gap*.30+criticality*.20+consequence*.15)
        return {"risk_before":round(risk_before,2),"expected_risk_reduction":round(expected,2),"expected_risk_after":round(max(0,risk_before-expected),2),"criticality":round(criticality,2),"coverage_gap":round(coverage_gap,2),"behavior_gap":round(behavior_gap,2),"evidence_strength":round(evidence,2),"reproduction_confidence":round(reproduction_conf,1),"false_positive_penalty":round(fp_penalty,2),"historical_investigations":investigations}

    def _v74_rank_candidates(self):
        state=self._v73_risk_state() if hasattr(self,"_v73_risk_state") else {"candidates":[]}
        ranked=[]
        for item in state.get("candidates",[]):
            x=dict(item); x["evidence_risk"]=self._v74_evidence_risk(x)
            e=x["evidence_risk"]
            x["decision_score"]=round(e["expected_risk_reduction"]+e["risk_before"]*.25,2)
            ranked.append(x)
        ranked.sort(key=lambda x:x["decision_score"],reverse=True)
        return ranked

    def _v74_select_action(self):
        ranked=self._v74_rank_candidates()
        if hasattr(self,"_v72_filter_candidates"):
            filtered=self._v72_filter_candidates(ranked)
            if filtered: ranked=filtered
        return ranked[0] if ranked else None

    def _v74_run_evidence_risk(self):
        ranked=self._v74_rank_candidates(); selected=self._v74_select_action()
        print("\\n"+"="*70+"\\n⚖️ V7.4 EVIDENCE-WEIGHTED RISK ENGINE\\n"+"="*70)
        print(f"\\n   Risk candidates : {len(ranked)}")
        for n,x in enumerate(ranked[:5],1):
            e=x["evidence_risk"]
            print(f"   {n}. {x.get('url','')} | {x.get('next_behavior','')} | risk={e['risk_before']:.2f} | retire={e['expected_risk_reduction']:.2f}")
        if selected:
            e=selected["evidence_risk"]
            print("\\n🎯 SELECTED NEXT ACTION")
            print(f"   Target              : {selected.get('url','')}")
            print(f"   Behavior            : {selected.get('next_behavior','')}")
            print(f"   Risk before         : {e['risk_before']:.2f}")
            print(f"   Expected reduction  : {e['expected_risk_reduction']:.2f}")
            print(f"   Expected risk after : {e['expected_risk_after']:.2f}")
            print(f"   Evidence strength   : {e['evidence_strength']:.2f}")
            print(f"   Reproduction conf.  : {e['reproduction_confidence']:.1f}%")
            print(f"   FP penalty          : {e['false_positive_penalty']:.2f}")
        else: print("\\n   STOP — no meaningful risk action remains.")
        print("="*70)
        self.v74_ranked_candidates=ranked; self.v74_next_action=selected
        # Locate the active report directory without inventing paths.
        from pathlib import Path
        import json
        for attr in ("report_dir","output_dir","report_path"):
            value=getattr(self,attr,None)
            if value:
                out=Path(str(value)); out=out.parent if out.suffix else out
                try:
                    out.mkdir(parents=True,exist_ok=True)
                    (out/"qa_evidence_weighted_risk_v7_4.json").write_text(json.dumps({"version":"7.4","ranked_candidates":ranked,"selected_action":selected,"credentials_stored":False},indent=2,default=str),encoding="utf-8")
                    print(f"\\n📄 Evidence-risk artifact: {out/'qa_evidence_weighted_risk_v7_4.json'}")
                except Exception as exc: print(f"\\n⚠️ Evidence-risk artifact could not be written: {exc}")
                break


    # ========================================================
    # V7.5 CONTINUOUS RISK MEMORY
    # ========================================================
    #
    # V7.5 carries risk knowledge across executions.
    #
    # The persistent model records only QA metadata:
    #   - URL / behavior identity
    #   - observations
    #   - risk before/after
    #   - retired risk
    #   - confirmed defect history
    #   - false-positive history
    #
    # It NEVER persists credentials, passwords, tokens, cookies,
    # authorization headers, form values, or page secrets.
    #
    # Risk is recalculated from current evidence every run.
    # Historical risk is evidence, not truth.
    # ========================================================

    def _v75_memory_path(self):
        from pathlib import Path

        candidates = []
        for attr in (
            "report_dir",
            "output_dir",
            "report_path",
        ):
            value = getattr(self, attr, None)
            if value:
                candidates.append(Path(str(value)))

        if candidates:
            base = candidates[0]
            if base.suffix:
                base = base.parent
        else:
            base = Path.cwd() / "qa_v8_report"

        base.mkdir(parents=True, exist_ok=True)

        # Keep the memory outside the per-run report so it survives runs.
        return Path.home() / ".qa_agent_v7_5_risk_memory.json"

    def _v75_load_memory(self):
        import json

        path = self._v75_memory_path()

        if not path.exists():
            self.v75_memory = {
                "version": "7.5",
                "runs": 0,
                "behaviors": {},
                "retired_risk": 0.0,
                "last_run": None,
            }
            return self.v75_memory

        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )

            if not isinstance(data, dict):
                raise ValueError("invalid memory root")

            data.setdefault("version", "7.5")
            data.setdefault("runs", 0)
            data.setdefault("behaviors", {})
            data.setdefault("retired_risk", 0.0)
            data.setdefault("last_run", None)

            self.v75_memory = data
            return data

        except Exception as exc:
            # Corrupt memory must never prevent QA execution.
            print(
                f"\n⚠️ V7.5 memory reset safely: {exc}"
            )

            self.v75_memory = {
                "version": "7.5",
                "runs": 0,
                "behaviors": {},
                "retired_risk": 0.0,
                "last_run": None,
            }
            return self.v75_memory

    def _v75_behavior_key(self, item):
        import hashlib

        if not isinstance(item, dict):
            item = {"behavior": str(item)}

        # Stable identity only. Do not include volatile screenshots,
        # credentials, cookies or generated values.
        raw = "|".join([
            str(item.get("url", "")),
            str(
                item.get(
                    "next_behavior",
                    item.get(
                        "behavior",
                        ""
                    )
                )
            ),
        ])

        return hashlib.sha256(
            raw.encode(
                "utf-8",
                errors="ignore"
            )
        ).hexdigest()[:24]

    def _v75_update_memory(self):
        from datetime import datetime
        import json

        memory = self._v75_load_memory()

        ranked = (
            self._v74_rank_candidates()
            if hasattr(
                self,
                "_v74_rank_candidates"
            )
            else []
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        memory["runs"] = int(
            memory.get("runs", 0)
        ) + 1

        retired_this_run = 0.0

        for item in ranked:
            key = self._v75_behavior_key(item)

            evidence = item.get(
                "evidence_risk",
                {}
            )

            current_risk = float(
                evidence.get(
                    "risk_before",
                    0
                )
            )

            expected_reduction = float(
                evidence.get(
                    "expected_risk_reduction",
                    0
                )
            )

            existing = memory["behaviors"].get(
                key,
                {
                    "url": str(
                        item.get("url", "")
                    ),
                    "behavior": str(
                        item.get(
                            "next_behavior",
                            ""
                        )
                    ),
                    "observations": 0,
                    "risk_history": [],
                    "retired_risk": 0.0,
                    "confirmed": 0,
                    "not_reproduced": 0,
                },
            )

            existing["observations"] = int(
                existing.get(
                    "observations",
                    0
                )
            ) + 1

            history = list(
                existing.get(
                    "risk_history",
                    []
                )
            )

            history.append({
                "run": memory["runs"],
                "timestamp": now,
                "risk_before": round(
                    current_risk,
                    2
                ),
                "expected_reduction": round(
                    expected_reduction,
                    2
                ),
            })

            # Bound memory growth.
            existing["risk_history"] = history[-20:]

            # Historical risk should not be accumulated blindly.
            # Use a conservative EMA so new evidence matters more.
            previous_risk = float(
                existing.get(
                    "current_risk",
                    current_risk
                )
            )

            blended_risk = round(
                previous_risk * 0.35
                + current_risk * 0.65,
                2
            )

            existing["current_risk"] = blended_risk

            memory["behaviors"][key] = existing

        # Calculate retired risk from observed movement between runs.
        for record in memory["behaviors"].values():
            history = record.get(
                "risk_history",
                []
            )

            if len(history) >= 2:
                previous = float(
                    history[-2].get(
                        "risk_before",
                        0
                    )
                )
                current = float(
                    history[-1].get(
                        "risk_before",
                        0
                    )
                )

                if previous > current:
                    retired_this_run += (
                        previous - current
                    )

        memory["retired_risk"] = round(
            float(
                memory.get(
                    "retired_risk",
                    0
                )
            ) + retired_this_run,
            2
        )

        memory["last_run"] = now

        # Persist QA metadata only.
        safe_payload = {
            "version": "7.5",
            "runs": memory["runs"],
            "retired_risk": memory["retired_risk"],
            "last_run": memory["last_run"],
            "behaviors": memory["behaviors"],
            "credentials_stored": False,
        }

        path = self._v75_memory_path()

        try:
            path.write_text(
                json.dumps(
                    safe_payload,
                    indent=2,
                    default=str
                ),
                encoding="utf-8"
            )
        except Exception as exc:
            print(
                f"\n⚠️ V7.5 memory write skipped: {exc}"
            )

        self.v75_memory = safe_payload
        self.v75_retired_this_run = round(
            retired_this_run,
            2
        )

        return safe_payload

    def _v75_continuous_risk(self):
        memory = (
            getattr(
                self,
                "v75_memory",
                None
            )
            or self._v75_load_memory()
        )

        records = list(
            memory.get(
                "behaviors",
                {}
            ).values()
        )

        unresolved = []
        retired = []

        for record in records:
            risk = float(
                record.get(
                    "current_risk",
                    0
                )
            )

            if risk > 0:
                unresolved.append(risk)
            else:
                retired.append(record)

        total_current_risk = round(
            sum(unresolved),
            2
        )

        max_risk = max(
            unresolved,
            default=0.0
        )

        return {
            "memory_runs": int(
                memory.get("runs", 0)
            ),
            "known_behavior_records": len(
                records
            ),
            "unresolved_behavior_records": len(
                unresolved
            ),
            "retired_behavior_records": len(
                retired
            ),
            "current_risk": total_current_risk,
            "highest_behavior_risk": round(
                max_risk,
                2
            ),
            "retired_risk_all_runs": round(
                float(
                    memory.get(
                        "retired_risk",
                        0
                    )
                ),
                2
            ),
            "retired_risk_this_run": float(
                getattr(
                    self,
                    "v75_retired_this_run",
                    0
                )
            ),
        }

    def _v75_select_continuous_action(self):
        ranked = (
            self._v74_rank_candidates()
            if hasattr(
                self,
                "_v74_rank_candidates"
            )
            else []
        )

        if not ranked:
            return None

        memory = (
            getattr(
                self,
                "v75_memory",
                {}
            )
            or {}
        )

        enriched = []

        for item in ranked:
            key = self._v75_behavior_key(item)
            history = memory.get(
                "behaviors",
                {}
            ).get(
                key,
                {}
            )

            ev = item.get(
                "evidence_risk",
                {}
            )

            # Historical risk adds weight only when the same behavior
            # has remained risky across runs.
            historical_risk = float(
                history.get(
                    "current_risk",
                    0
                )
            )

            persistence_bonus = min(
                20.0,
                historical_risk * 0.20
            )

            score = round(
                float(
                    item.get(
                        "decision_score",
                        0
                    )
                )
                + persistence_bonus,
                2
            )

            candidate = dict(item)
            candidate["continuous_score"] = score
            candidate["historical_risk"] = round(
                historical_risk,
                2
            )
            candidate["historical_observations"] = int(
                history.get(
                    "observations",
                    0
                )
            )

            enriched.append(candidate)

        enriched.sort(
            key=lambda x: x["continuous_score"],
            reverse=True
        )

        if not enriched:
            return None

        selected = enriched[0]

        selected["decision_reason"] = (
            "highest current risk reduction "
            "after incorporating persistent "
            "cross-run evidence"
        )

        return selected

    def _v75_print_continuous_memory(self):
        state = self._v75_continuous_risk()
        selected = self._v75_select_continuous_action()

        print("\n")
        print("=" * 70)
        print("🧠 V7.5 CONTINUOUS RISK MEMORY")
        print("=" * 70)

        print("\n📚 CROSS-RUN MEMORY")
        print(
            f"   Memory runs          : "
            f"{state['memory_runs']}"
        )
        print(
            f"   Behavior records     : "
            f"{state['known_behavior_records']}"
        )
        print(
            f"   Unresolved records   : "
            f"{state['unresolved_behavior_records']}"
        )
        print(
            f"   Retired records      : "
            f"{state['retired_behavior_records']}"
        )

        print("\n📉 CONTINUOUS RISK")
        print(
            f"   Current risk         : "
            f"{state['current_risk']:.2f}"
        )
        print(
            f"   Highest behavior risk: "
            f"{state['highest_behavior_risk']:.2f}"
        )
        print(
            f"   Risk retired/all runs: "
            f"{state['retired_risk_all_runs']:.2f}"
        )
        print(
            f"   Risk retired this run: "
            f"{state['retired_risk_this_run']:.2f}"
        )

        print("\n🎯 NEXT CONTINUOUS ACTION")

        if selected:
            print(
                f"   Target          : "
                f"{selected.get('url', '')}"
            )
            print(
                f"   Behavior        : "
                f"{selected.get('next_behavior', '')}"
            )
            print(
                f"   Current risk    : "
                f"{selected.get('evidence_risk', {}).get('risk_before', 0):.2f}"
            )
            print(
                f"   Historical risk : "
                f"{selected.get('historical_risk', 0):.2f}"
            )
            print(
                f"   Observations    : "
                f"{selected.get('historical_observations', 0)}"
            )
            print(
                f"   Continuous score: "
                f"{selected.get('continuous_score', 0):.2f}"
            )
            print(
                f"   Reason          : "
                f"{selected.get('decision_reason', '')}"
            )
        else:
            print(
                "   No unresolved risk action remains."
            )

        print("\n🔐 MEMORY SAFETY")
        print(
            "   Credentials stored : NO"
        )
        print(
            "   Tokens stored      : NO"
        )
        print(
            "   Cookies stored     : NO"
        )

        print("=" * 70)

        self.v75_continuous_state = state
        self.v75_next_action = selected

    def _v75_write_artifact(self):
        import json
        from pathlib import Path

        candidates = []

        for attr in (
            "report_dir",
            "output_dir",
            "report_path",
        ):
            value = getattr(
                self,
                attr,
                None
            )
            if value:
                candidates.append(
                    Path(str(value))
                )

        if not candidates:
            return

        out_dir = candidates[0]

        if out_dir.suffix:
            out_dir = out_dir.parent

        try:
            out_dir.mkdir(
                parents=True,
                exist_ok=True
            )

            payload = {
                "version": "7.5",
                "continuous_risk":
                    self._v75_continuous_risk(),
                "next_action":
                    self._v75_select_continuous_action(),
                "memory_path":
                    str(
                        self._v75_memory_path()
                    ),
                "credentials_stored": False,
                "tokens_stored": False,
                "cookies_stored": False,
            }

            path = (
                out_dir
                / "qa_continuous_risk_v7_5.json"
            )

            path.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    default=str
                ),
                encoding="utf-8"
            )

            print(
                f"\n📄 Continuous-risk artifact: {path}"
            )

        except Exception as exc:
            print(
                f"\n⚠️ Continuous-risk artifact "
                f"could not be written: {exc}"
            )

    def _v75_run_continuous_memory(self):
        self._v75_load_memory()
        self._v75_update_memory()
        self._v75_print_continuous_memory()
        self._v75_write_artifact()


    # ========================================================
    # V7.6 CHANGE-AWARE AUTONOMOUS QA
    # ========================================================
    # Compares application models across runs and targets QA at
    # meaningful changes. Persistent state contains QA metadata only.

    def _v76_memory_path(self):
        return Path(os.environ.get(
            "QA_CHANGE_MEMORY_FILE",
            str(Path.home() / ".qa_agent_v7_6_change_memory.json")
        ))

    def _v76_element_key(self, e):
        # Stable enough for cross-run comparison without storing values.
        ident = clean(e.get("id")) or clean(e.get("data_testid")) or clean(e.get("name"))
        if not ident:
            ident = clean(e.get("aria_label")) or clean(e.get("text"))
        return "|".join((
            clean(e.get("semantic")), clean(e.get("tag")), ident,
            clean(e.get("input_type")), clean(e.get("placeholder"))
        ))[:500]

    def _v76_element_fingerprint(self, e):
        return {
            "semantic": clean(e.get("semantic")),
            "tag": clean(e.get("tag")),
            "id": clean(e.get("id")),
            "name": clean(e.get("name")),
            "data_testid": clean(e.get("data_testid")),
            "aria_label": clean(e.get("aria_label")),
            "placeholder": clean(e.get("placeholder")),
            "input_type": clean(e.get("input_type")),
            "text": clean(e.get("text"))[:200],
            "disabled": bool(e.get("disabled")),
            "required": bool(e.get("required")),
            "readonly": bool(e.get("readonly")),
        }

    def _v76_snapshot(self):
        import hashlib
        snap = {"urls": {}}
        for page in self.pages:
            url = normalize_url(page.get("url", ""))
            elements = {}
            for e in page.get("elements", []):
                key = self._v76_element_key(e)
                if not key:
                    continue
                elements[key] = self._v76_element_fingerprint(e)
            payload = json.dumps(elements, sort_keys=True, separators=(",", ":"))
            snap["urls"][url] = {
                "title": clean(page.get("title")),
                "elements": elements,
                "fingerprint": hashlib.sha256(payload.encode()).hexdigest(),
            }
        return snap

    def _v76_load(self):
        path = self._v76_memory_path()
        if not path.exists():
            return {"version": "7.6", "runs": 0, "snapshot": {}, "history": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid change memory")
            data.setdefault("version", "7.6")
            data.setdefault("runs", 0)
            data.setdefault("snapshot", {})
            data.setdefault("history", [])
            return data
        except Exception as exc:
            print(f"\n⚠️ V7.6 change memory unavailable; starting safe baseline: {exc}")
            return {"version": "7.6", "runs": 0, "snapshot": {}, "history": []}

    def _v76_canonical_url(self, url):
        # Treat the origin root and its trailing-slash form as the same page.
        u = normalize_url(url)
        parsed = urlparse(u)
        if parsed.path == "/":
            u = parsed._replace(path="").geturl()
        return u

    def _v76_diff(self, old, new):
        # Canonicalize snapshots before comparison so harmless URL formatting
        # differences (e.g. / vs no trailing slash) are not reported as change.
        old_urls = {}
        for url, value in old.get("urls", {}).items():
            old_urls[self._v76_canonical_url(url)] = value
        new_urls = {}
        for url, value in new.get("urls", {}).items():
            new_urls[self._v76_canonical_url(url)] = value

        # A first observation establishes the baseline. It is not an
        # application change and must not produce synthetic risk.
        if not old_urls:
            return {
                "added_urls": [],
                "removed_urls": [],
                "changed_urls": [],
                "added_elements": [],
                "removed_elements": [],
                "modified_elements": [],
                "total_changes": 0,
                "baseline": True,
            }

        ou = set(old_urls); nu = set(new_urls)
        added_urls = sorted(nu - ou); removed_urls = sorted(ou - nu)
        changed = []; added_elements = []; removed_elements = []; modified_elements = []
        for url in sorted(ou & nu):
            oe = old_urls[url].get("elements", {}); ne = new_urls[url].get("elements", {})
            for key in sorted(set(ne) - set(oe)):
                added_elements.append({"url": url, "element": ne[key], "key": key})
            for key in sorted(set(oe) - set(ne)):
                removed_elements.append({"url": url, "element": oe[key], "key": key})
            for key in sorted(set(oe) & set(ne)):
                if oe[key] != ne[key]:
                    modified_elements.append({"url": url, "before": oe[key], "after": ne[key], "key": key})
            if (old_urls[url].get("fingerprint") != new_urls[url].get("fingerprint")
                    or old_urls[url].get("title") != new_urls[url].get("title")):
                changed.append(url)
        return {
            "added_urls": added_urls,
            "removed_urls": removed_urls,
            "changed_urls": sorted(set(changed)),
            "added_elements": added_elements,
            "removed_elements": removed_elements,
            "modified_elements": modified_elements,
            "total_changes": len(added_urls) + len(removed_urls) + len(set(changed)) + len(added_elements) + len(removed_elements) + len(modified_elements),
            "baseline": not bool(old.get("urls")),
        }

    def _v76_score(self, d):
        if d.get("baseline"):
            return 0.0
        score = (len(d["added_urls"]) * 20 + len(d["removed_urls"]) * 15
                 + len(d["changed_urls"]) * 12 + len(d["added_elements"]) * 4
                 + len(d["removed_elements"]) * 5 + len(d["modified_elements"]) * 7)
        return round(min(100.0, float(score)), 2)

    def _v76_targets(self, d):
        targets = []
        for u in d["added_urls"]:
            targets.append({"url": u, "action": "discover_and_regress", "risk": 90.0, "reason": "new page"})
        for u in d["changed_urls"]:
            targets.append({"url": u, "action": "targeted_regression", "risk": 88.0, "reason": "application model changed"})
        for x in d["added_elements"]:
            targets.append({"url": x["url"], "action": "exercise_new_control", "risk": 82.0, "reason": "new interactive surface"})
        for x in d["modified_elements"]:
            targets.append({"url": x["url"], "action": "exercise_modified_control", "risk": 86.0, "reason": "control behavior/identity changed"})
        for x in d["removed_elements"]:
            targets.append({"url": x["url"], "action": "validate_removed_control", "risk": 70.0, "reason": "interactive surface removed"})
        for u in d["removed_urls"]:
            targets.append({"url": u, "action": "validate_page_removal", "risk": 65.0, "reason": "page removed"})
        return sorted(targets, key=lambda x: x["risk"], reverse=True)

    def _v76_prepare(self):
        memory = self._v76_load(); current = self._v76_snapshot()
        self.v76_previous_snapshot = memory.get("snapshot", {})
        self.v76_current_snapshot = current
        self.v76_diff = self._v76_diff(self.v76_previous_snapshot, current)
        self.v76_change_risk = self._v76_score(self.v76_diff)
        self.v76_targets = self._v76_targets(self.v76_diff)
        self.v76_memory = memory

        # Change-aware ordering: changed-page tests first, while preserving
        # every generated test and never deleting the deterministic suite.
        changed = set(self.v76_diff["changed_urls"] + self.v76_diff["added_urls"])
        self.v76_changed_urls = changed

    def _v76_reorder_plan(self):
        if not getattr(self, "v76_changed_urls", None) or not self.plan:
            return
        changed = self.v76_changed_urls
        self.plan.sort(key=lambda t: 0 if normalize_url(t.get("url", "")) in changed else 1)

    def _v76_finalize(self):
        from datetime import datetime
        memory = self._v76_load()
        memory["runs"] = int(memory.get("runs", 0)) + 1
        memory["snapshot"] = getattr(self, "v76_current_snapshot", self._v76_snapshot())
        hist = list(memory.get("history", []))
        hist.append({
            "run": memory["runs"],
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "change_risk": getattr(self, "v76_change_risk", 0),
            "total_changes": getattr(self, "v76_diff", {}).get("total_changes", 0),
        })
        memory["history"] = hist[-30:]
        memory["credentials_stored"] = False
        memory["tokens_stored"] = False
        memory["cookies_stored"] = False
        try:
            self._v76_memory_path().write_text(json.dumps(memory, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"\n⚠️ V7.6 memory write skipped: {exc}")
        self.v76_memory = memory

    def _v76_print(self):
        d = getattr(self, "v76_diff", {})
        targets = getattr(self, "v76_targets", [])
        print("\n" + "=" * 70)
        print("🔄 V8 AUTONOMOUS E2E QUALITY INTELLIGENCE")
        print("=" * 70)
        print(f"\n   New pages            : {len(d.get('added_urls', []))}")
        print(f"   Removed pages        : {len(d.get('removed_urls', []))}")
        print(f"   Changed pages        : {len(d.get('changed_urls', []))}")
        print(f"   New elements         : {len(d.get('added_elements', []))}")
        print(f"   Removed elements     : {len(d.get('removed_elements', []))}")
        print(f"   Modified elements    : {len(d.get('modified_elements', []))}")
        print(f"   Total changes        : {d.get('total_changes', 0)}")
        print(f"\n⚠️ Change risk score    : {getattr(self, 'v76_change_risk', 0):.2f}")
        print("\n🎯 TARGETED QA")
        if targets:
            for i, t in enumerate(targets[:10], 1):
                print(f"   {i}. {t['action']} → {t['url']} | risk={t['risk']:.1f} | {t['reason']}")
        else:
            print("   No material application change detected.")
        if d.get("baseline"):
            print("\n   Baseline established — no synthetic changes reported.")
        print("\n🔐 Change memory stores credentials/tokens/cookies: NO")
        print("=" * 70)

    def _v76_write_artifact(self):
        try:
            (REPORT_DIR / "qa_change_intelligence_v7_8.json").write_text(
                json.dumps({
                    "version": "7.6",
                    "change_risk": getattr(self, "v76_change_risk", 0),
                    "diff": getattr(self, "v76_diff", {}),
                    "targeted_actions": getattr(self, "v76_targets", []),
                    "credentials_stored": False,
                    "tokens_stored": False,
                    "cookies_stored": False,
                }, indent=2), encoding="utf-8")
            print(f"\n📄 Change-intelligence artifact: {(REPORT_DIR / 'qa_change_intelligence_v7_8.json').absolute()}")
        except Exception as exc:
            print(f"\n⚠️ Change artifact could not be written: {exc}")

    def _v76_run_change_intelligence(self):
        self._v76_finalize(); self._v76_print(); self._v76_write_artifact()

    async def run(self):
        self._print_preferences()
        self._v61_start()
        self._v70_run_mission()
        self._v67_capture_before()
        self._v68_replan()
        self._v69_run_command_center()
        self._v65_replan_coverage()
        self._v64_replan()
        self._v63_replan()
        self._v62_plan_exploration()
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
                headless=getattr(self, "v79_headless", True)
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

        # V7.6: establish the current application model and diff it
        # against the previous run before planning execution.
        self._v76_prepare()

        # Proven V3.5.6 test generation.
        self.generate_tests()

        # New V4 planning layer.
        self.build_quality_plan()
        self._v76_reorder_plan()

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
                headless=getattr(self, "v79_headless", True)
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

        self._v76_run_change_intelligence()
        self._v77_run_failure_classification()
        self._v78_print_quality_truth()
        self._v78_write_quality_truth()
        self._v8_print_e2e_quality()
        self._v8_write_e2e_artifact()
        self.report()


    # ========================================================
    # REPORT
    # ========================================================


    # ========================================================
    # V7.7 FAILURE CLASSIFICATION / DEFECT GATE
    # ========================================================
    #
    # A failed automated test is not automatically a product defect.
    # V7.7 classifies execution failures as:
    #   APPLICATION_DEFECT_CANDIDATE
    #   AUTOMATION_DEFECT
    #   TRANSIENT_FAILURE
    #   ENVIRONMENT_FAILURE
    #   UNCLASSIFIED
    #
    # The existing investigation/reproduction loop remains the
    # authoritative gate for confirmed application defects.
    #
    # No credentials, tokens, cookies, passwords, or submitted
    # secret values are persisted by this layer.
    # ========================================================

    def _v77_failure_classification(self, result):
        import re

        if not isinstance(result, dict):
            result = {"reason": str(result)}

        reason = str(
            result.get(
                "reason",
                result.get(
                    "error",
                    result.get("message", ""),
                ),
            )
        ).lower()

        semantic = str(
            result.get(
                "semantic",
                "",
            )
        ).lower()

        combined = f"{reason} {semantic}"

        if any(
            x in combined
            for x in (
                "could not be resolved uniquely",
                "strict mode violation",
                "locator",
                "selector",
                "element not found",
                "no element",
                "semantic element",
                "safe_candidate_locator",
            )
        ):
            return (
                "AUTOMATION_DEFECT",
                0.92,
                "Failure is dominated by locator or "
                "element-resolution behavior.",
            )

        if any(
            x in combined
            for x in (
                "connection refused",
                "dns",
                "net::err_",
                "browser disconnected",
                "target closed",
                "page crashed",
                "connection reset",
            )
        ):
            return (
                "ENVIRONMENT_FAILURE",
                0.94,
                "Failure indicates browser, network, "
                "or runtime instability.",
            )

        if any(
            x in combined
            for x in (
                "timed out",
                "did not become enabled",
                "not visible",
                "not stable",
                "detached from dom",
                "waiting for",
            )
        ):
            return (
                "TRANSIENT_FAILURE",
                0.70,
                "Failure may be timing, hydration, "
                "or transient UI state.",
            )

        if any(
            x in combined
            for x in (
                "expected",
                "actual",
                "got",
                "mismatch",
                "assertion",
                "incorrect",
            )
        ):
            return (
                "APPLICATION_DEFECT_CANDIDATE",
                0.68,
                "Observed result differs from the "
                "expected result.",
            )

        return (
            "UNCLASSIFIED",
            0.25,
            "Insufficient evidence for safe classification.",
        )

    def _v77_failure_results(self):
        # Prefer the authoritative execution result collection.
        results = getattr(
            self,
            "results",
            [],
        )

        if not isinstance(results, list):
            return []

        return [
            x for x in results
            if isinstance(x, dict)
            and str(x.get("status", "")).upper() == "FAIL"
        ]

    def _v77_run_failure_classification(self):
        import hashlib
        import json
        from pathlib import Path

        failures = self._v77_failure_results()
        matrix = []

        for failure in failures:
            classification, confidence, reason = (
                self._v77_failure_classification(
                    failure
                )
            )

            raw = "|".join([
                str(failure.get("url", "")),
                str(
                    failure.get(
                        "test",
                        failure.get(
                            "name",
                            failure.get(
                                "action",
                                "",
                            ),
                        ),
                    )
                ),
                str(
                    failure.get(
                        "reason",
                        failure.get(
                            "error",
                            "",
                        ),
                    )
                ),
            ])

            signature = hashlib.sha256(
                raw.encode(
                    "utf-8",
                    errors="ignore",
                )
            ).hexdigest()[:24]

            matrix.append({
                "signature": signature,
                "url": str(
                    failure.get("url", "")
                ),
                "test": str(
                    failure.get(
                        "test",
                        failure.get(
                            "name",
                            failure.get(
                                "action",
                                "",
                            ),
                        ),
                    )
                ),
                "classification": classification,
                "confidence": confidence,
                "reason": reason,
            })

        counts = {}
        for item in matrix:
            name = item["classification"]
            counts[name] = counts.get(name, 0) + 1

        print("\n")
        print("=" * 70)
        print("🧠 V8 AUTONOMOUS E2E QUALITY INTELLIGENCE")
        print("=" * 70)

        print("\n📊 FAILURE CLASSES")

        if not counts:
            print("   No execution failures to classify.")
        else:
            for name, count in sorted(counts.items()):
                print(
                    f"   {name:<32}: {count}"
                )

        if matrix:
            print("\n🔎 CLASSIFICATION EVIDENCE")
            for number, item in enumerate(
                matrix,
                start=1,
            ):
                print(
                    f"\n   {number}. "
                    f"{item['test'] or '<unnamed>'}"
                )
                print(
                    f"      URL            : {item['url']}"
                )
                print(
                    f"      Classification : "
                    f"{item['classification']}"
                )
                print(
                    f"      Confidence     : "
                    f"{item['confidence'] * 100:.1f}%"
                )
                print(
                    f"      Reason         : "
                    f"{item['reason']}"
                )

        print("\n🛡️ DEFECT GATE")
        print(
            "   A failure is not reported as a confirmed "
            "application defect without reproducible evidence."
        )

        print("\n🔐 SECRET SAFETY")
        print("   Credentials stored : NO")
        print("   Tokens stored      : NO")
        print("   Cookies stored     : NO")

        print("=" * 70)

        self.v77_failure_matrix = matrix

        # Per-run artifact. Deliberately contains metadata only.
        report_dir = globals().get(
            "REPORT_DIR",
            None,
        )

        if report_dir:
            try:
                out_dir = Path(str(report_dir))
                out_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                artifact = (
                    out_dir
                    / "qa_quality_truth_v8.json"
                )

                artifact.write_text(
                    json.dumps(
                        {
                            "version": "7.7",
                            "failure_matrix": matrix,
                            "credentials_stored": False,
                            "tokens_stored": False,
                            "cookies_stored": False,
                        },
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )

                print(
                    f"\n📄 Failure-classification artifact: "
                    f"{artifact.absolute()}"
                )

            except Exception as exc:
                print(
                    f"\n⚠️ V7.7 artifact write skipped: {exc}"
                )


    # ========================================================
    # V8 QUALITY TRUTH ENGINE
    # ========================================================
    #
    # Separates four independent truths:
    #
    #   1. REGRESSION HEALTH
    #   2. AUTOMATION HEALTH
    #   3. APPLICATION CHANGE
    #   4. CONFIRMED PRODUCT DEFECTS
    #
    # A change is not automatically a defect.
    # A failed test is not automatically a product defect.
    # A passed regression suite does not mean zero risk.
    #
    # V7.8 produces an auditable quality decision without
    # storing credentials, tokens, cookies, passwords, or secrets.
    # ========================================================

    def _v78_execution_truth(self):
        results = getattr(self, "results", [])
        if not isinstance(results, list):
            results = []

        passed = sum(
            1 for x in results
            if isinstance(x, dict)
            and str(x.get("status", "")).upper() == "PASS"
        )
        failed = sum(
            1 for x in results
            if isinstance(x, dict)
            and str(x.get("status", "")).upper() == "FAIL"
        )

        return {
            "tests_executed": len(results),
            "passed": passed,
            "failed": failed,
            "regression_healthy": (
                len(results) > 0 and failed == 0
            ),
        }

    def _v78_application_change_truth(self):
        diff = getattr(
            self,
            "v76_current_diff",
            None,
        )

        if not isinstance(diff, dict):
            # V7.7 may expose its change data under another name.
            diff = getattr(
                self,
                "v77_change_diff",
                {},
            )

        if not isinstance(diff, dict):
            diff = {}

        return {
            "new_pages": len(
                diff.get("added_urls", [])
            ),
            "removed_pages": len(
                diff.get("removed_urls", [])
            ),
            "changed_pages": len(
                diff.get("changed_urls", [])
            ),
            "new_elements": len(
                diff.get("added_elements", [])
            ),
            "removed_elements": len(
                diff.get("removed_elements", [])
            ),
            "modified_elements": len(
                diff.get("modified_elements", [])
            ),
            "new_behaviors": len(
                diff.get("added_behaviors", [])
            ),
            "removed_behaviors": len(
                diff.get("removed_behaviors", [])
            ),
            "total_changes": int(
                diff.get("total_changes", 0)
            ),
            "change_risk": float(
                getattr(
                    self,
                    "v76_change_risk",
                    0,
                )
                or 0
            ),
        }

    def _v78_automation_truth(self):
        matrix = getattr(
            self,
            "v77_failure_matrix",
            [],
        )

        if not isinstance(matrix, list):
            matrix = []

        automation = sum(
            1 for x in matrix
            if x.get("classification")
            == "AUTOMATION_DEFECT"
        )

        transient = sum(
            1 for x in matrix
            if x.get("classification")
            == "TRANSIENT_FAILURE"
        )

        environment = sum(
            1 for x in matrix
            if x.get("classification")
            == "ENVIRONMENT_FAILURE"
        )

        unclassified = sum(
            1 for x in matrix
            if x.get("classification")
            == "UNCLASSIFIED"
        )

        candidate = sum(
            1 for x in matrix
            if x.get("classification")
            == "APPLICATION_DEFECT_CANDIDATE"
        )

        if automation > 0:
            health = "DEGRADED"
        elif environment > 0:
            health = "ENVIRONMENT_UNSTABLE"
        elif transient > 0:
            health = "TRANSIENT"
        elif unclassified > 0:
            health = "UNCERTAIN"
        else:
            health = "HEALTHY"

        return {
            "health": health,
            "automation_failures": automation,
            "transient_failures": transient,
            "environment_failures": environment,
            "unclassified_failures": unclassified,
            "application_candidates": candidate,
        }

    def _v78_defect_truth(self):
        # Confirmed defects must come from the agent's investigation
        # state, not from raw execution failures.
        confirmed = getattr(
            self,
            "confirmed_defects",
            None,
        )

        if isinstance(confirmed, list):
            count = len(confirmed)
        elif isinstance(confirmed, int):
            count = confirmed
        else:
            count = 0

        investigated = getattr(
            self,
            "investigated",
            None,
        )
        if not isinstance(investigated, int):
            investigated = int(
                getattr(
                    self,
                    "investigated_count",
                    0,
                )
                or 0
            )

        not_reproduced = getattr(
            self,
            "not_reproduced",
            None,
        )
        if not isinstance(not_reproduced, int):
            not_reproduced = int(
                getattr(
                    self,
                    "not_reproduced_count",
                    0,
                )
                or 0
            )

        return {
            "confirmed_defects": max(
                0,
                count,
            ),
            "investigated": max(
                0,
                investigated,
            ),
            "not_reproduced": max(
                0,
                not_reproduced,
            ),
        }

    def _v78_quality_truth(self):
        execution = self._v78_execution_truth()
        automation = self._v78_automation_truth()
        change = self._v78_application_change_truth()
        defects = self._v78_defect_truth()

        if defects["confirmed_defects"] > 0:
            release_signal = "BLOCKED_BY_CONFIRMED_DEFECT"
        elif not execution["regression_healthy"]:
            release_signal = "REGRESSION_FAILURE"
        elif automation["health"] in (
            "DEGRADED",
            "ENVIRONMENT_UNSTABLE",
        ):
            release_signal = "AUTOMATION_OR_ENVIRONMENT_RISK"
        elif change["total_changes"] > 0:
            release_signal = "CHANGED_SURFACES_REQUIRE_TARGETED_REVIEW"
        else:
            release_signal = "NO_CONFIRMED_QUALITY_BLOCKER"

        return {
            "regression_health": (
                "HEALTHY"
                if execution["regression_healthy"]
                else "FAILED"
            ),
            "automation_health":
                automation["health"],
            "application_change":
                "CHANGED"
                if change["total_changes"] > 0
                else "UNCHANGED",
            "confirmed_defects":
                defects["confirmed_defects"],
            "release_signal":
                release_signal,
            "execution":
                execution,
            "automation":
                automation,
            "change":
                change,
            "defects":
                defects,
        }

    def _v78_print_quality_truth(self):
        truth = self._v78_quality_truth()

        print("\n")
        print("=" * 70)
        print("🧭 V8 QUALITY TRUTH")
        print("=" * 70)

        print("\n🧪 REGRESSION HEALTH")
        print(
            f"   Status          : "
            f"{truth['regression_health']}"
        )
        print(
            f"   Executed        : "
            f"{truth['execution']['tests_executed']}"
        )
        print(
            f"   Passed          : "
            f"{truth['execution']['passed']}"
        )
        print(
            f"   Failed          : "
            f"{truth['execution']['failed']}"
        )

        print("\n🤖 AUTOMATION HEALTH")
        print(
            f"   Status          : "
            f"{truth['automation_health']}"
        )
        print(
            f"   Automation      : "
            f"{truth['automation']['automation_failures']}"
        )
        print(
            f"   Transient       : "
            f"{truth['automation']['transient_failures']}"
        )
        print(
            f"   Environment     : "
            f"{truth['automation']['environment_failures']}"
        )
        print(
            f"   Unclassified    : "
            f"{truth['automation']['unclassified_failures']}"
        )

        print("\n🔄 APPLICATION CHANGE")
        change = truth["change"]
        print(
            f"   Status          : "
            f"{truth['application_change']}"
        )
        print(
            f"   New pages       : "
            f"{change['new_pages']}"
        )
        print(
            f"   Removed pages   : "
            f"{change['removed_pages']}"
        )
        print(
            f"   Changed pages   : "
            f"{change['changed_pages']}"
        )
        print(
            f"   New elements    : "
            f"{change['new_elements']}"
        )
        print(
            f"   Removed elements: "
            f"{change['removed_elements']}"
        )
        print(
            f"   Modified        : "
            f"{change['modified_elements']}"
        )
        print(
            f"   Total changes   : "
            f"{change['total_changes']}"
        )
        print(
            f"   Change risk     : "
            f"{change['change_risk']:.2f}"
        )

        print("\n🚨 PRODUCT DEFECTS")
        print(
            f"   Confirmed       : "
            f"{truth['confirmed_defects']}"
        )
        print(
            f"   Investigated    : "
            f"{truth['defects']['investigated']}"
        )
        print(
            f"   Not reproduced  : "
            f"{truth['defects']['not_reproduced']}"
        )

        print("\n🎯 QUALITY DECISION")
        print(
            f"   {truth['release_signal']}"
        )

        print("\n🔐 SECRET SAFETY")
        print("   Credentials : NO")
        print("   Tokens      : NO")
        print("   Cookies     : NO")

        print("=" * 70)

        self.v78_quality_truth = truth

    def _v78_write_quality_truth(self):
        import json
        from pathlib import Path

        report_dir = globals().get(
            "REPORT_DIR",
            None,
        )

        if not report_dir:
            return

        out_dir = Path(str(report_dir))
        out_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = (
            out_dir
            / "qa_quality_truth_v8.json"
        )

        payload = {
            "version": "7.8",
            "quality_truth":
                getattr(
                    self,
                    "v78_quality_truth",
                    self._v78_quality_truth(),
                ),
            "credentials_stored": False,
            "tokens_stored": False,
            "cookies_stored": False,
        }

        try:
            path.write_text(
                json.dumps(
                    payload,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            print(
                f"\n📄 Quality-truth artifact: "
                f"{path.absolute()}"
            )
        except Exception as exc:
            print(
                f"\n⚠️ V7.8 quality-truth artifact "
                f"write skipped: {exc}"
            )


    # ============================================================
    # V7.9 PERFORMANCE / QUALITY TRUTH
    # ============================================================

    def _v79_performance_truth(self):
        import time

        started = getattr(
            self,
            "v79_started_at",
            None,
        )

        elapsed = None
        if started is not None:
            try:
                elapsed = round(
                    time.monotonic() - started,
                    2,
                )
            except Exception:
                elapsed = None

        return {
            "headless": bool(
                getattr(
                    self,
                    "v79_headless",
                    True,
                )
            ),
            "slow_mode": bool(
                getattr(
                    self,
                    "v79_slow_mode",
                    False,
                )
            ),
            "elapsed_seconds": elapsed,
        }

    def _v79_print_performance_truth(self):
        truth = self._v79_performance_truth()

        print("\n")
        print("=" * 70)
        print("⚡ V7.9 HEADLESS PERFORMANCE")
        print("=" * 70)

        print(
            f"\n   Browser mode : "
            f"{'HEADLESS' if truth['headless'] else 'HEADED'}"
        )

        print(
            f"   Slow mode    : "
            f"{'ON' if truth['slow_mode'] else 'OFF'}"
        )

        if truth["elapsed_seconds"] is not None:
            print(
                f"   Runtime      : "
                f"{truth['elapsed_seconds']:.2f}s"
            )

        print(
            "\n   Headless is the default execution mode "
            "for faster CI/local regression runs."
        )
        print(
            "   Use --headed when visual debugging is required."
        )

        print("=" * 70)


    # ============================================================
    # V8 AUTONOMOUS E2E QUALITY GATES
    # ============================================================

    def _v8_classify_test(self, item):
        if not isinstance(item, dict):
            return "REGRESSION"

        explicit = str(
            item.get(
                "test_type",
                item.get(
                    "type",
                    item.get("category", ""),
                ),
            )
        ).upper()

        if explicit in (
            "SMOKE",
            "SANITY",
            "REGRESSION",
            "E2E",
        ):
            return explicit

        name = " ".join(
            str(item.get(k, ""))
            for k in ("name", "test", "action", "description")
        ).lower()

        if any(
            x in name
            for x in (
                "end to end",
                "e2e",
                "workflow",
                "journey",
                "checkout",
                "registration",
                "login",
            )
        ):
            return "E2E"

        if any(
            x in name
            for x in (
                "smoke",
                "health",
                "homepage",
                "navigation",
            )
        ):
            return "SMOKE"

        if any(
            x in name
            for x in (
                "sanity",
                "changed",
                "impacted",
                "targeted",
            )
        ):
            return "SANITY"

        return "REGRESSION"

    def _v8_quality_gates(self):
        results = getattr(self, "results", [])
        if not isinstance(results, list):
            results = []

        groups = {
            "SMOKE": [],
            "SANITY": [],
            "REGRESSION": [],
            "E2E": [],
        }

        for item in results:
            if not isinstance(item, dict):
                continue
            groups[self._v8_classify_test(item)].append(item)

        gates = {}

        for kind, items in groups.items():
            passed = sum(
                1
                for x in items
                if str(
                    x.get("status", "")
                ).upper() == "PASS"
            )
            failed = sum(
                1
                for x in items
                if str(
                    x.get("status", "")
                ).upper() == "FAIL"
            )

            gates[kind.lower()] = {
                "status": (
                    "HEALTHY"
                    if items and failed == 0
                    else (
                        "FAILED"
                        if failed > 0
                        else "NOT_EXPLICITLY_COVERED"
                    )
                ),
                "executed": len(items),
                "passed": passed,
                "failed": failed,
            }

        return gates

    def _v8_print_e2e_quality(self):
        gates = self._v8_quality_gates()

        print("\n")
        print("=" * 70)
        print("🧭 V8 AUTONOMOUS E2E QUALITY")
        print("=" * 70)

        for key, label in (
            ("smoke", "🔥 SMOKE"),
            ("sanity", "🧪 SANITY"),
            ("regression", "🔁 REGRESSION"),
            ("e2e", "🧭 END-TO-END"),
        ):
            g = gates[key]
            print(f"\n{label}")
            print(f"   Status   : {g['status']}")
            print(f"   Executed : {g['executed']}")
            print(f"   Passed   : {g['passed']}")
            print(f"   Failed   : {g['failed']}")

        covered = sum(
            1
            for g in gates.values()
            if g["status"] != "NOT_EXPLICITLY_COVERED"
        )

        print("\n📐 COVERAGE TRUTH")
        print(
            f"   Explicit test-intent classes: "
            f"{covered}/4"
        )

        if covered < 4:
            print(
                "   ⚠️ This run does not explicitly "
                "demonstrate all four test intents."
            )

        print(
            "\n   E2E is only claimed when tests are "
            "explicitly classified as E2E."
        )
        print("=" * 70)

        self.v8_quality_gates = gates

    def _v8_write_e2e_artifact(self):
        import json
        from pathlib import Path

        report_dir = globals().get(
            "REPORT_DIR",
            None,
        )
        if not report_dir:
            return

        out_dir = Path(str(report_dir))
        out_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = (
            out_dir
            / "qa_e2e_quality_v8.json"
        )

        payload = {
            "version": "8.0",
            "gates": getattr(
                self,
                "v8_quality_gates",
                self._v8_quality_gates(),
            ),
            "credentials_stored": False,
            "tokens_stored": False,
            "cookies_stored": False,
        }

        path.write_text(
            json.dumps(
                payload,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        print(
            f"\n📄 E2E quality artifact: {path.absolute()}"
        )

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
                        "7.6",

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

                    "v7_2_false_positive_calibration": {
                        "enabled": True,
                        "requires_repeated_non_reproduction": True,
                        "reproduction_overrides_suppression": True,
                        "safety_valve_keeps_highest_risk": True,
                        "tracks_fp_rate": True,
                        "tracks_reproduction_rate": True,
                        "tracks_suppressed_patterns": True,
                        "credentials_stored": False,
                        "preserves_deterministic_regression": True,
                    },

                    "v7_1_risk_closure": {
                        "enabled": True,
                        "risk_remaining": True,
                        "risk_retired": True,
                        "unknown_behavior": True,
                        "uninvestigated_anomalies": True,
                        "confirmed_defects": True,
                        "mission_completion_percent": True,
                        "never_complete_on_regression_pass_only": True,
                        "credentials_stored": False,
                        "preserves_deterministic_regression": True,
                    },

                    "v7_0_mission_engine": {
                        "enabled": True,
                        "mission_driven": True,
                        "completion_criteria": [
                            "no_high_value_unknown_behavior",
                            "no_confirmed_unresolved_defect",
                        ],
                        "adaptive_next_action": True,
                        "bounded_artifact": True,
                        "credentials_stored": False,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_9_command_center": {
                        "enabled": True,
                        "answers": [
                            "what_was_tested",
                            "what_was_learned",
                            "why_next_test",
                            "what_remains_unknown",
                        ],
                        "writes_bounded_json_artifact": True,
                        "credentials_stored": False,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_8_evidence_graph": {
                        "enabled": True,
                        "nodes": [
                            "page",
                            "goal",
                            "behavior",
                            "decision",
                            "finding",
                            "evidence",
                            "outcome",
                            "verdict",
                        ],
                        "relationships": [
                            "has_goal",
                            "has_behavior_gap",
                            "targets",
                            "for_page",
                            "has_finding",
                            "supported_by",
                            "resulted_in",
                            "has_verdict",
                        ],
                        "bounded": True,
                        "credentials_stored": False,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_7_decision_memory_integrity": {
                        "enabled": True,
                        "audit_before_after": True,
                        "tracks_score_delta": True,
                        "tracks_coverage_delta": True,
                        "tracks_decision_change": True,
                        "bounded_memory": True,
                        "credentials_stored": False,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_6_closed_loop": {
                        "enabled": True,
                        "loop": [
                            "observe",
                            "understand",
                            "choose",
                            "act",
                            "evaluate",
                            "learn",
                            "replan",
                        ],
                        "behavioral_gap_driven": True,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_5_behavioral_coverage": {
                        "enabled": True,
                        "principle": "pass_is_not_coverage",
                        "coverage_model": [
                            "page_load",
                            "navigation",
                            "controls",
                            "normal_behavior",
                            "boundary_behavior",
                            "state_transition",
                            "surface_specific_behavior",
                        ],
                        "adaptive_unknown_behavior_bonus": True,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_4_explainable_planner": {
                        "enabled": True,
                        "decision_model": "goal_reason_plan_action_observe",
                        "plan_is_advisory": True,
                        "preserves_deterministic_regression": True,
                    },

                    "v6_3_reasoning": {
                        "enabled": True,
                        "planner": "risk_history_adaptive",
                        "tiers": [
                            "CRITICAL",
                            "HIGH",
                            "MEDIUM",
                            "LOW",
                        ],
                        "reasoning_factors": [
                            "novelty",
                            "previous_failures",
                            "confirmed_defects",
                            "exploration_coverage",
                            "false_positive_history",
                            "business_risk_vocabulary",
                            "stability_history",
                        ],
                    },

                    "v6_2_1_resolver": {
                        "enabled": True,
                        "resolution_order": [
                            "exact_id",
                            "exact_name",
                            "exact_aria_label",
                            "exact_role_name",
                            "unique_semantic",
                            "unique_visible_semantic",
                        ],
                    },

                    "v6_2_adaptive": {
                        "priority_queue": getattr(
                            self,
                            "v62_priority_queue",
                            []
                        ),
                        "adaptive_enabled": True,
                    },

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

                    "v7_6_change_intelligence": {
                        "enabled": True,
                        "change_risk": getattr(self, "v76_change_risk", 0),
                        "diff": getattr(self, "v76_diff", {}),
                        "targeted_actions": getattr(self, "v76_targets", []),
                        "credentials_stored": False,
                        "tokens_stored": False,
                        "cookies_stored": False,
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
            "📊 V8 AUTONOMOUS E2E QUALITY REPORT"
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
            f"V8 confidence      : "
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
    args = _v79_parse_cli()
    url = args.url
    print("\n*** RUNNING qa_agent_v5_1_DEFECT_INVESTIGATION_FINAL.py — VERSION 7.6 FINAL ***\n")

    print("\n*** RUNNING qa_agent_v5_1_DEFECT_INVESTIGATION_FINAL.py — VERSION 7.6 FINAL ***\n")
    print("\n*** RUNNING qa_agent_v5_1_DEFECT_INVESTIGATION_FINAL.py — VERSION 7.6 FINAL ***\n")


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

    agent.v79_headless = args.headless

    agent.v79_slow_mode = args.slow

    agent.v79_started_at = __import__('time').monotonic()

    await agent.run()
    agent._v61_learn()
if __name__ == "__main__":
    asyncio.run(main())
