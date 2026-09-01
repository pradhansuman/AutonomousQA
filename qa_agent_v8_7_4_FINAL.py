#!/usr/bin/env python3
"""
V8.7.4 — LIVE DISCOVERY TIMEOUT FIX + SINGLE CANONICAL TRUTH

Fixes V8.7.3:
- Separates navigation timeout from DOM extraction timeout.
- Uses a fast `commit` fallback when DOMContentLoaded is slow.
- Continues discovery when the document is usable even if navigation lifecycle
  does not complete within the configured navigation timeout.
- Never reads prior V8.x truth/report artifacts.
- Rejects invalid /None/javascript/foreign URLs.
- Current-run evidence only.
- Backend/business outcomes remain UNKNOWN unless directly observed.
- Failed journeys remain failed and receive targeted reproduction.
- Unknown behaviors are targeted by risk.
- Release remains fail-closed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

VERSION = "8.7.4"
DEFAULT_URL = "https://demoqa.com"
DEFAULT_MAX_PAGES = 50
DEFAULT_NAV_TIMEOUT_MS = 15000
DEFAULT_DOM_TIMEOUT_MS = 8000
DEFAULT_ACTION_TIMEOUT_MS = 5000
DEFAULT_JOURNEY_TIMEOUT_MS = 15000
DEFAULT_UNKNOWN_BUDGET = 60

# New run-specific directory. This program NEVER reads previous report dirs.
REPORT_DIR = Path("qa_v8_7_4_report")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def canonical_url(raw: Any, base: str | None = None) -> str | None:
    if raw is None:
        return None

    value = str(raw).strip()
    if not value:
        return None

    if value.lower() in {
        "none", "null", "undefined", "javascript:void(0)", "#"
    }:
        return None

    if base:
        value = urljoin(base, value)

    try:
        value, _fragment = urldefrag(value)
        p = urlparse(value)

        scheme = p.scheme.lower()
        host = (p.hostname or "").lower()

        if scheme not in {"http", "https"} or not host:
            return None

        netloc = host
        if p.port and not (
            (scheme == "http" and p.port == 80)
            or (scheme == "https" and p.port == 443)
        ):
            netloc = f"{host}:{p.port}"

        path = p.path or "/"
        path = re.sub(r"/{2,}", "/", path)

        if not path.startswith("/"):
            path = "/" + path

        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        return urlunparse(
            (scheme, netloc, path, "", p.query, "")
        )

    except Exception:
        return None


def same_host(a: str, b: str) -> bool:
    return (
        (urlparse(a).hostname or "").lower()
        == (urlparse(b).hostname or "").lower()
    )


@dataclass
class Element:
    element_id: str
    tag: str
    role: str
    text: str
    label: str
    name: str
    placeholder: str
    input_type: str
    selector: str
    href: str | None
    visible: bool
    disabled: bool
    semantic: str
    current_run: bool = True


@dataclass
class PageModel:
    url: str
    title: str
    elements: list[Element]
    links: list[str]
    http_status: int | None
    navigation_mode: str
    current_run: bool = True


@dataclass
class Behavior:
    behavior_id: str
    page_url: str
    semantic: str
    action: str
    selector: str
    label: str
    risk: float
    status: str = "UNKNOWN"
    evidence_ids: list[str] = field(default_factory=list)
    current_run: bool = True


@dataclass
class Journey:
    journey_id: str
    goal: str
    url: str
    score: float
    steps: list[dict[str, Any]]
    source_behavior_ids: list[str]
    status: str = "UNEXECUTED"


class Agent:
    def __init__(
        self,
        target: str,
        headless: bool,
        max_pages: int,
        nav_timeout_ms: int,
        dom_timeout_ms: int,
        journey_timeout_ms: int,
        unknown_budget: int,
    ):
        normalized = canonical_url(target)
        if not normalized:
            raise ValueError(f"Invalid target URL: {target}")

        self.target = normalized
        self.headless = headless
        self.max_pages = max(1, max_pages)
        self.nav_timeout_ms = max(1000, nav_timeout_ms)
        self.dom_timeout_ms = max(1000, dom_timeout_ms)
        self.action_timeout_ms = min(
            DEFAULT_ACTION_TIMEOUT_MS,
            self.dom_timeout_ms,
        )
        self.journey_timeout_ms = max(1000, journey_timeout_ms)
        self.unknown_budget = max(1, unknown_budget)

        self.run_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()

        self.report_dir = REPORT_DIR
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self.pages: list[PageModel] = []
        self.behaviors: list[Behavior] = []
        self.journeys: list[Journey] = []
        self.journey_results: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

        self.seen_urls: set[str] = set()
        self.queue: list[str] = []
        self.queued_urls: set[str] = set()

        # Explicit integrity state.
        self.legacy_state_reused = False
        self.legacy_artifacts_read: list[str] = []
        self.synthetic_evidence = False
        self.foreign_evidence = False
        self.fabricated_backend_claims = False

    def log(self, message: str) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        print(f"[{elapsed:7.1f}s] {message}", flush=True)

    def add_error(
        self,
        phase: str,
        message: str,
        url: str | None = None,
    ) -> None:
        self.errors.append(
            {
                "phase": phase,
                "message": message,
                "url": url,
                "timestamp": utc_now(),
                "run_id": self.run_id,
                "current_run": True,
            }
        )

    def add_evidence(
        self,
        evidence_type: str,
        *,
        url: str | None = None,
        behavior_id: str | None = None,
        journey_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        evidence_id = digest(
            f"{self.run_id}|{len(self.evidence)}|"
            f"{evidence_type}|{url}|{behavior_id}|{journey_id}"
        )

        self.evidence.append(
            {
                "evidence_id": evidence_id,
                "run_id": self.run_id,
                "version": VERSION,
                "type": evidence_type,
                "url": url,
                "behavior_id": behavior_id,
                "journey_id": journey_id,
                "observed": True,
                "current_run": True,
                "timestamp": utc_now(),
                "details": details or {},
            }
        )

        return evidence_id

    async def navigate_live(
        self,
        page,
        url: str,
    ) -> tuple[Any, int | None, str]:
        """
        Navigation strategy:

        1. Try DOMContentLoaded with a bounded navigation timeout.
        2. If lifecycle timeout occurs, use the already-created document when
           available; otherwise retry with `commit`.
        3. A slow lifecycle event must not hide a usable current DOM.
        """
        response = None
        mode = "domcontentloaded"

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.nav_timeout_ms,
            )
        except PlaywrightTimeoutError:
            mode = "timeout_fallback"

            # The browser may already have a usable document.
            try:
                current = canonical_url(page.url, url)
                body_count = await page.locator(
                    "body"
                ).count()

                if current and body_count > 0:
                    self.log(
                        "   ⚠️ DOMContentLoaded timeout; "
                        "using usable current document"
                    )
                else:
                    # Force navigation to commit quickly.
                    mode = "commit_fallback"
                    response = await page.goto(
                        url,
                        wait_until="commit",
                        timeout=self.nav_timeout_ms,
                    )

                    await page.wait_for_selector(
                        "body",
                        state="attached",
                        timeout=self.dom_timeout_ms,
                    )
            except Exception:
                raise

        status = None
        if response is not None:
            status = response.status

        # DOM readiness is independently bounded.
        try:
            await page.wait_for_selector(
                "body",
                state="attached",
                timeout=self.dom_timeout_ms,
            )
        except PlaywrightTimeoutError:
            # A document can exist without a normal body selector becoming
            # available within the DOM timeout. Treat as navigation failure.
            raise RuntimeError("USABLE_DOM_TIMEOUT")

        return response, status, mode

    async def discover_page(
        self,
        page,
        url: str,
    ) -> PageModel | None:
        self.log(
            f"🔎 DISCOVER [{len(self.seen_urls)}/{self.max_pages}] {url}"
        )

        try:
            response, status, mode = await self.navigate_live(
                page, url
            )

            final_url = canonical_url(page.url, url)

            if not final_url:
                self.add_error(
                    "discovery",
                    "INVALID_FINAL_URL",
                    url,
                )
                return None

            if not same_host(final_url, self.target):
                self.add_error(
                    "discovery",
                    "FOREIGN_FINAL_URL",
                    final_url,
                )
                return None

            if status is not None and status >= 400:
                self.log(
                    f"⚠️ HTTP={status} | {final_url}"
                )

            title = await page.title()

            raw_elements = await page.locator(
                "button,input,textarea,select,a,[role],"
                "[contenteditable='true']"
            ).evaluate_all(
                """
                els => els.map((e, i) => {
                    const r = e.getBoundingClientRect();
                    const s = getComputedStyle(e);
                    return {
                        index: i,
                        tag: (e.tagName || '').toLowerCase(),
                        role: e.getAttribute('role') || '',
                        text: (
                            e.innerText ||
                            e.textContent ||
                            ''
                        ).trim().slice(0, 200),
                        name: e.getAttribute('name') || '',
                        aria: e.getAttribute('aria-label') || '',
                        placeholder:
                            e.getAttribute('placeholder') || '',
                        type:
                            e.getAttribute('type') || '',
                        id: e.id || '',
                        href: e.getAttribute('href'),
                        disabled: !!e.disabled,
                        visible:
                            !!(r.width && r.height) &&
                            s.visibility !== 'hidden' &&
                            s.display !== 'none'
                    };
                })
                """
            )

            elements: list[Element] = []

            for item in raw_elements or []:
                tag = str(item.get("tag") or "").lower()
                role = str(item.get("role") or "").lower()
                text = str(item.get("text") or "").strip()
                name = str(item.get("name") or "").strip()
                aria = str(item.get("aria") or "").strip()
                placeholder = str(
                    item.get("placeholder") or ""
                ).strip()
                input_type = str(
                    item.get("type") or ""
                ).lower()
                element_id = str(
                    item.get("id") or ""
                ).strip()

                visible = bool(item.get("visible"))
                disabled = bool(item.get("disabled"))

                label = (
                    aria
                    or name
                    or placeholder
                    or text
                )

                if tag == "a":
                    semantic = "navigation"
                    action = "navigate"
                elif (
                    tag in {"input", "textarea", "select"}
                    or role in {"textbox", "combobox"}
                ):
                    semantic = "input"
                    action = "fill"
                elif (
                    tag == "button"
                    or role in {
                        "button",
                        "checkbox",
                        "radio",
                        "tab",
                        "slider",
                    }
                ):
                    semantic = "action"
                    action = "click"
                else:
                    semantic = "interactive"
                    action = "click"

                if element_id:
                    selector = f"#{element_id}"
                elif name:
                    safe_name = name.replace('"', '\\"')
                    selector = f'[name="{safe_name}"]'
                else:
                    selector = tag or "*"

                href = canonical_url(
                    item.get("href"),
                    final_url,
                )

                key = (
                    f"{final_url}|{tag}|{role}|{element_id}|"
                    f"{name}|{label[:100]}|{selector}"
                )

                elements.append(
                    Element(
                        element_id=digest(key),
                        tag=tag,
                        role=role,
                        text=text,
                        label=label[:200],
                        name=name,
                        placeholder=placeholder,
                        input_type=input_type,
                        selector=selector,
                        href=href,
                        visible=visible,
                        disabled=disabled,
                        semantic=semantic,
                    )
                )

            hrefs = await page.locator(
                "a[href]"
            ).evaluate_all(
                "els => els.map(e => e.getAttribute('href'))"
            )

            links: set[str] = set()

            for href in hrefs or []:
                candidate = canonical_url(
                    href,
                    final_url,
                )

                if (
                    candidate
                    and same_host(candidate, self.target)
                ):
                    links.add(candidate)

            result = PageModel(
                url=final_url,
                title=title,
                elements=elements,
                links=sorted(links),
                http_status=status,
                navigation_mode=mode,
            )

            self.add_evidence(
                "DISCOVERY_PAGE_OBSERVED",
                url=final_url,
                details={
                    "http_status": status,
                    "navigation_mode": mode,
                    "element_count": len(elements),
                    "link_count": len(links),
                },
            )

            self.log(
                f"✅ DISCOVERED | {final_url} | "
                f"elements={len(elements)} | "
                f"links={len(links)} | mode={mode}"
            )

            return result

        except PlaywrightTimeoutError:
            self.add_error(
                "discovery",
                "NAVIGATION_OR_DOM_TIMEOUT",
                url,
            )
            self.log(
                f"❌ DISCOVERY TIMEOUT | {url}"
            )
            return None

        except Exception as exc:
            self.add_error(
                "discovery",
                f"{type(exc).__name__}: {exc}",
                url,
            )
            self.log(
                f"❌ DISCOVERY ERROR | {url} | "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    async def discover(self, browser) -> bool:
        self.log("=" * 70)
        self.log("🗺️ V8.7.4 CURRENT-RUN DISCOVERY")
        self.log(f"Target     : {self.target}")
        self.log(f"Max pages  : {self.max_pages}")
        self.log(
            f"Nav timeout: {self.nav_timeout_ms} ms"
        )
        self.log(
            f"DOM timeout: {self.dom_timeout_ms} ms"
        )
        self.log("Legacy artifacts: NOT READ")

        page = await browser.new_page()
        page.set_default_timeout(
            self.dom_timeout_ms
        )

        start = canonical_url(self.target)
        assert start

        self.queue = [start]
        self.queued_urls = {start}

        try:
            while (
                self.queue
                and len(self.pages) < self.max_pages
            ):
                raw = self.queue.pop(0)
                self.queued_urls.discard(raw)

                url = canonical_url(
                    raw,
                    self.target,
                )

                if not url:
                    self.log(
                        f"🧹 SKIP INVALID URL | {raw!r}"
                    )
                    continue

                if not same_host(url, self.target):
                    self.log(
                        f"🧹 SKIP FOREIGN URL | {url}"
                    )
                    continue

                if url in self.seen_urls:
                    continue

                self.seen_urls.add(url)

                model = await self.discover_page(
                    page,
                    url,
                )

                if model is None:
                    continue

                if any(
                    p.url == model.url
                    for p in self.pages
                ):
                    continue

                self.pages.append(model)

                for link in model.links:
                    if (
                        link not in self.seen_urls
                        and link not in self.queued_urls
                    ):
                        self.queue.append(link)
                        self.queued_urls.add(link)

            urls = [p.url for p in self.pages]

            if not self.pages:
                self.add_error(
                    "discovery",
                    "CURRENT_DISCOVERY_EMPTY",
                )
                return False

            if len(urls) != len(set(urls)):
                self.add_error(
                    "discovery",
                    "DUPLICATE_CANONICAL_URLS",
                )
                return False

            if any(
                not p.current_run
                or canonical_url(p.url) is None
                or not same_host(p.url, self.target)
                for p in self.pages
            ):
                self.add_error(
                    "discovery",
                    "INVALID_CURRENT_APPLICATION_MODEL",
                )
                return False

            self.log(
                f"📚 DISCOVERY COMPLETE | pages={len(self.pages)}"
            )
            return True

        finally:
            await page.close()

    def build_behaviors(self) -> None:
        self.log("=" * 70)
        self.log("🧠 BUILD CURRENT-RUN BEHAVIOR MODEL")

        result: dict[str, Behavior] = {}

        for model in self.pages:
            for element in model.elements:
                if not element.current_run:
                    continue
                if not element.visible:
                    continue
                if element.disabled:
                    continue

                if element.semantic == "navigation":
                    action = "navigate"
                    risk = 35.0
                elif element.semantic == "input":
                    action = "fill"
                    risk = 55.0
                else:
                    action = "click"
                    risk = 50.0

                text_blob = (
                    element.label
                    + " "
                    + element.text
                    + " "
                    + model.url
                ).lower()

                if any(
                    token in text_blob
                    for token in (
                        "submit",
                        "save",
                        "delete",
                        "upload",
                        "login",
                        "register",
                        "add",
                    )
                ):
                    risk += 15.0

                behavior_id = digest(
                    f"{self.run_id}|{model.url}|"
                    f"{element.element_id}|{action}"
                )

                result[behavior_id] = Behavior(
                    behavior_id=behavior_id,
                    page_url=model.url,
                    semantic=element.semantic,
                    action=action,
                    selector=element.selector,
                    label=element.label,
                    risk=min(100.0, risk),
                )

        self.behaviors = sorted(
            result.values(),
            key=lambda b: (
                -b.risk,
                b.page_url,
                b.behavior_id,
            ),
        )

        self.log(
            f"🧠 BEHAVIOR MODEL COMPLETE | "
            f"surfaces={len(self.behaviors)}"
        )

    def build_journeys(self) -> None:
        self.log("=" * 70)
        self.log("🧭 BUILD CURRENT-RUN BUSINESS JOURNEYS")

        by_page: dict[str, list[Behavior]] = {}

        for behavior in self.behaviors:
            by_page.setdefault(
                behavior.page_url,
                [],
            ).append(behavior)

        journeys: list[Journey] = []

        for page_url, behaviors in by_page.items():
            inputs = [
                b for b in behaviors
                if b.action == "fill"
            ]
            clicks = [
                b for b in behaviors
                if b.action == "click"
            ]
            navigations = [
                b for b in behaviors
                if b.action == "navigate"
            ]

            if inputs and clicks:
                goal = "Submit valid user information"
                selected = inputs[:2] + clicks[:1]
            elif (
                "upload-download" in page_url
                and clicks
            ):
                goal = "Transfer a file successfully"
                selected = clicks[:2]
            elif navigations:
                goal = "Navigate application surface"
                selected = navigations[:2]
            elif clicks:
                goal = "Create or manage application data"
                selected = clicks[:2]
            elif inputs:
                goal = "Exercise input behavior"
                selected = inputs[:2]
            else:
                continue

            selected = selected[:3]
            source_ids = [
                b.behavior_id
                for b in selected
            ]

            steps = [
                {
                    "step": i + 1,
                    "behavior_id": b.behavior_id,
                    "action": b.action,
                    "selector": b.selector,
                    "label": b.label,
                    "semantic": b.semantic,
                }
                for i, b in enumerate(selected)
            ]

            score = round(
                min(
                    100.0,
                    max(
                        b.risk
                        for b in selected
                    ) + len(selected) * 3.0,
                ),
                2,
            )

            journey_id = digest(
                f"{self.run_id}|{page_url}|"
                f"{goal}|{','.join(source_ids)}"
            )

            journeys.append(
                Journey(
                    journey_id=journey_id,
                    goal=goal,
                    url=page_url,
                    score=score,
                    steps=steps,
                    source_behavior_ids=source_ids,
                )
            )

        self.journeys = sorted(
            journeys,
            key=lambda j: (
                -j.score,
                j.url,
                j.journey_id,
            ),
        )

        self.log(
            f"🧭 JOURNEY MODEL COMPLETE | "
            f"journeys={len(self.journeys)}"
        )

    async def execute_behavior(
        self,
        page,
        behavior: Behavior,
        journey_id: str | None = None,
    ) -> bool:
        locator = page.locator(
            behavior.selector
        ).first

        try:
            await locator.wait_for(
                state="visible",
                timeout=self.action_timeout_ms,
            )

            if behavior.action == "fill":
                await locator.fill(
                    "QA_AUTONOMOUS_TEST"
                )

            elif behavior.action == "click":
                await locator.click(
                    timeout=self.action_timeout_ms,
                    no_wait_after=True,
                )

            elif behavior.action == "navigate":
                href = await locator.get_attribute(
                    "href"
                )

                target = canonical_url(
                    href,
                    page.url,
                )

                if not target:
                    raise RuntimeError(
                        "INVALID_NAVIGATION_TARGET"
                    )

                if not same_host(
                    target,
                    self.target,
                ):
                    raise RuntimeError(
                        "FOREIGN_NAVIGATION_TARGET"
                    )

                await self.navigate_live(
                    page,
                    target,
                )

            else:
                raise RuntimeError(
                    f"UNSUPPORTED_ACTION:{behavior.action}"
                )

            evidence_id = self.add_evidence(
                "UI_BEHAVIOR_OBSERVED",
                url=page.url,
                behavior_id=behavior.behavior_id,
                journey_id=journey_id,
                details={
                    "action": behavior.action,
                    "selector": behavior.selector,
                    "label": behavior.label,
                },
            )

            behavior.status = "COVERED"
            behavior.evidence_ids.append(
                evidence_id
            )

            return True

        except Exception as exc:
            self.add_evidence(
                "BEHAVIOR_EXECUTION_ERROR",
                url=page.url,
                behavior_id=behavior.behavior_id,
                journey_id=journey_id,
                details={
                    "action": behavior.action,
                    "selector": behavior.selector,
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                },
            )

            return False

    async def execute_journey(
        self,
        context,
        journey: Journey,
        ordinal: int,
        retry: bool = False,
    ) -> dict[str, Any]:
        retry_text = " RETRY" if retry else ""

        self.log(
            f"▶ JOURNEY {ordinal} | "
            f"{journey.goal}{retry_text} | "
            f"{journey.url}"
        )

        page = await context.new_page()
        page.set_default_timeout(
            self.dom_timeout_ms
        )

        started = time.monotonic()
        step_results: list[dict[str, Any]] = []
        error: str | None = None

        async def run_steps():
            response, status, mode = await self.navigate_live(
                page,
                journey.url,
            )

            self.add_evidence(
                "UI_ENTRY_OBSERVED",
                url=page.url,
                journey_id=journey.journey_id,
                details={
                    "http_status": status,
                    "navigation_mode": mode,
                },
            )

            if status is not None and status >= 400:
                raise RuntimeError(
                    f"HTTP_STATUS_{status}"
                )

            for step in journey.steps:
                behavior = next(
                    (
                        b
                        for b in self.behaviors
                        if b.behavior_id
                        == step["behavior_id"]
                    ),
                    None,
                )

                if behavior is None:
                    raise RuntimeError(
                        "MISSING_CURRENT_BEHAVIOR:"
                        + step["behavior_id"]
                    )

                passed = await self.execute_behavior(
                    page,
                    behavior,
                    journey.journey_id,
                )

                step_results.append(
                    {
                        "step": step["step"],
                        "behavior_id": (
                            behavior.behavior_id
                        ),
                        "action": behavior.action,
                        "selector": behavior.selector,
                        "status": (
                            "PASS"
                            if passed
                            else "FAIL"
                        ),
                    }
                )

                if not passed:
                    raise RuntimeError(
                        "BEHAVIOR_FAILED:"
                        + behavior.behavior_id
                    )

        try:
            await asyncio.wait_for(
                run_steps(),
                timeout=(
                    self.journey_timeout_ms / 1000
                ),
            )

            status = "PASS"
            self.log("   🟢 JOURNEY PASS")

        except asyncio.TimeoutError:
            status = "FAIL"
            error = "JOURNEY_TIMEOUT"
            self.log(
                "   🔴 JOURNEY FAIL | JOURNEY_TIMEOUT"
            )

        except Exception as exc:
            status = "FAIL"
            error = (
                f"{type(exc).__name__}: {exc}"
            )
            self.log(
                f"   🔴 JOURNEY FAIL | {error}"
            )

        finally:
            await page.close()

        return {
            "journey_id": journey.journey_id,
            "goal": journey.goal,
            "url": journey.url,
            "status": status,
            "error": error,
            "retry": retry,
            "duration_ms": round(
                (
                    time.monotonic()
                    - started
                ) * 1000,
                2,
            ),
            "steps": step_results,
            "current_run": True,

            # These remain UNKNOWN because this script does not
            # make direct backend/business observations.
            "backend_verified": False,
            "business_verified": False,
            "backend_outcome": "UNKNOWN",
            "business_outcome": "UNKNOWN",

            "timestamp": utc_now(),
        }

    async def execute_journeys(
        self,
        browser,
    ) -> None:
        self.log("=" * 70)
        self.log("🚀 CURRENT-RUN JOURNEY EXECUTION")

        context = await browser.new_context()

        try:
            for i, journey in enumerate(
                self.journeys,
                1,
            ):
                result = await self.execute_journey(
                    context,
                    journey,
                    i,
                )

                self.journey_results.append(
                    result
                )

                journey.status = result["status"]

            failed = [
                r
                for r in self.journey_results
                if r["status"] == "FAIL"
                and r.get("retry") is not True
            ]

            if failed:
                self.log("=" * 70)
                self.log(
                    f"🔬 TARGETED FAILURE REPRODUCTION | "
                    f"failed={len(failed)}"
                )

                journey_by_id = {
                    j.journey_id: j
                    for j in self.journeys
                }

                for i, failed_result in enumerate(
                    failed,
                    1,
                ):
                    journey = journey_by_id.get(
                        failed_result["journey_id"]
                    )

                    if not journey:
                        continue

                    retry_result = (
                        await self.execute_journey(
                            context,
                            journey,
                            i,
                            retry=True,
                        )
                    )

                    self.journey_results.append(
                        retry_result
                    )

                    if retry_result["status"] == "PASS":
                        self.log(
                            "   🟡 FAILURE NOT REPRODUCED"
                        )
                    else:
                        self.log(
                            "   🔴 FAILURE REPRODUCED"
                        )

        finally:
            await context.close()

    async def close_unknowns(
        self,
        browser,
    ) -> None:
        self.log("=" * 70)
        self.log("🎯 TARGETED UNKNOWN-BEHAVIOR CLOSURE")

        unknown = [
            b
            for b in self.behaviors
            if b.status == "UNKNOWN"
        ]

        unknown.sort(
            key=lambda b: (
                -b.risk,
                b.page_url,
                b.behavior_id,
            )
        )

        selected = unknown[
            : min(
                self.unknown_budget,
                len(unknown),
            )
        ]

        self.log(
            f"Unknown before closure : {len(unknown)}"
        )
        self.log(
            f"Closure budget         : {len(selected)}"
        )

        if not selected:
            return

        context = await browser.new_context()

        try:
            for i, behavior in enumerate(
                selected,
                1,
            ):
                self.log(
                    f"🎯 UNKNOWN [{i}/{len(selected)}] "
                    f"risk={behavior.risk:.1f} | "
                    f"{behavior.page_url} | "
                    f"{behavior.action} | "
                    f"{behavior.label[:80]}"
                )

                page = await context.new_page()
                page.set_default_timeout(
                    self.dom_timeout_ms
                )

                try:
                    await self.navigate_live(
                        page,
                        behavior.page_url,
                    )

                    passed = await self.execute_behavior(
                        page,
                        behavior,
                    )

                    if passed:
                        self.log(
                            "   🟢 UNKNOWN CLOSED"
                        )
                    else:
                        self.log(
                            "   🔴 UNKNOWN REMAINS"
                        )

                except Exception as exc:
                    self.add_error(
                        "unknown_closure",
                        (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        behavior.page_url,
                    )
                    self.log(
                        f"   ❌ CLOSURE ERROR | "
                        f"{type(exc).__name__}: {exc}"
                    )

                finally:
                    await page.close()

        finally:
            await context.close()

    def calculate_truth(self) -> dict[str, Any]:
        total_behaviors = len(self.behaviors)

        covered = sum(
            1
            for b in self.behaviors
            if b.status == "COVERED"
        )

        unknown = (
            total_behaviors - covered
        )

        actual_journeys = [
            r
            for r in self.journey_results
            if not r.get("retry")
        ]

        journey_pass = sum(
            r["status"] == "PASS"
            for r in actual_journeys
        )

        journey_fail = sum(
            r["status"] == "FAIL"
            for r in actual_journeys
        )

        # Direct backend/business observation does not exist in this engine.
        backend_verified = sum(
            r.get("backend_verified") is True
            for r in actual_journeys
        )

        business_verified = sum(
            r.get("business_verified") is True
            for r in actual_journeys
        )

        # This MUST remain false unless a direct BACKEND_OBSERVED
        # evidence record exists for the same journey.
        fabricated_backend = False

        for result in self.journey_results:
            if result.get("backend_verified") is True:
                direct = any(
                    e.get("type")
                    == "BACKEND_OBSERVED"
                    and e.get("journey_id")
                    == result.get("journey_id")
                    and e.get("run_id")
                    == self.run_id
                    for e in self.evidence
                )

                if not direct:
                    fabricated_backend = True

        foreign_evidence = any(
            e.get("run_id") != self.run_id
            or e.get("current_run") is not True
            for e in self.evidence
        )

        synthetic_evidence = any(
            e.get("type")
            in {
                "SYNTHETIC",
                "INFERRED_PASS",
                "ASSUMED_BACKEND_PASS",
            }
            for e in self.evidence
        )

        # No legacy state is ever loaded by this program.
        legacy_reused = self.legacy_state_reused

        coverage = (
            round(
                covered
                / total_behaviors
                * 100,
                2,
            )
            if total_behaviors
            else None
        )

        gates = {
            "current_discovery": (
                len(self.pages) > 0
            ),
            "canonical_model": (
                total_behaviors > 0
                and all(
                    b.current_run
                    for b in self.behaviors
                )
            ),
            "legacy_state_reused": (
                not legacy_reused
            ),
            "synthetic_evidence": (
                not synthetic_evidence
            ),
            "foreign_evidence": (
                not foreign_evidence
            ),
            "execution_failures": (
                journey_fail == 0
            ),
            "journey_failures": (
                journey_fail == 0
            ),
            "unknown_behaviors": (
                unknown == 0
            ),
            "evidence_exists": (
                bool(self.evidence)
                if actual_journeys
                else True
            ),
            "fabricated_backend_claims": (
                not fabricated_backend
                and backend_verified == 0
            ),
        }

        release_pass = all(
            gates.values()
        )

        observed_outcomes = sum(
            (
                r.get("backend_outcome")
                == "OBSERVED"
                or r.get("business_outcome")
                == "OBSERVED"
            )
            for r in actual_journeys
        )

        # This engine deliberately does not infer backend/business outcomes.
        inferred_outcomes = 0

        unknown_outcomes = max(
            0,
            len(actual_journeys)
            - observed_outcomes
            - inferred_outcomes,
        )

        return {
            "version": VERSION,
            "run_id": self.run_id,
            "target": self.target,
            "generated_at": utc_now(),

            "pages_discovered": len(self.pages),
            "behavioral_surfaces": total_behaviors,
            "behaviors_covered": covered,
            "unknown_behaviors": unknown,
            "behavioral_coverage_percent": coverage,

            "business_journeys": len(self.journeys),
            "journey_pass": journey_pass,
            "journey_fail": journey_fail,
            "journey_execution_coverage_percent": (
                round(
                    len(actual_journeys)
                    / len(self.journeys)
                    * 100,
                    2,
                )
                if self.journeys
                else None
            ),

            "executions": len(actual_journeys),
            "execution_pass": (
                len(actual_journeys)
                - journey_fail
            ),
            "execution_fail": journey_fail,

            "evidence_records": len(
                self.evidence
            ),

            "observed_outcomes": observed_outcomes,
            "inferred_outcomes": inferred_outcomes,
            "unknown_outcomes": unknown_outcomes,

            "backend_verified": backend_verified,
            "business_verified": business_verified,

            "legacy_state_reused": legacy_reused,
            "legacy_artifacts_read": (
                self.legacy_artifacts_read
            ),
            "synthetic_evidence": (
                synthetic_evidence
            ),
            "foreign_evidence": foreign_evidence,
            "fabricated_backend_claims": (
                fabricated_backend
            ),

            "evidence_violations": (
                int(synthetic_evidence)
                + int(foreign_evidence)
                + int(fabricated_backend)
            ),

            "truth_gates": gates,

            "release_truth": (
                "PASS"
                if release_pass
                else "FAIL"
            ),

            "release_blocked": (
                not release_pass
            ),

            "fail_closed": True,

            "secret_safety": {
                "credentials": False,
                "tokens": False,
                "cookies": False,
            },

            "errors": self.errors,
        }

    def save_json(
        self,
        filename: str,
        data: Any,
    ) -> None:
        path = (
            self.report_dir
            / filename
        )

        path.write_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

    def save(self, truth: dict[str, Any]) -> None:
        self.save_json(
            "qa_current_application_model_v8_7_4.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "pages": [
                    asdict(p)
                    for p in self.pages
                ],
            },
        )

        self.save_json(
            "qa_current_behavior_model_v8_7_4.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "behaviors": [
                    asdict(b)
                    for b in self.behaviors
                ],
            },
        )

        self.save_json(
            "qa_current_journey_model_v8_7_4.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "journeys": [
                    asdict(j)
                    for j in self.journeys
                ],
            },
        )

        self.save_json(
            "qa_current_execution_evidence_v8_7_4.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "journey_results": (
                    self.journey_results
                ),
                "evidence": self.evidence,
            },
        )

        # ONE canonical truth artifact for this run.
        self.save_json(
            "qa_v8_7_4_canonical_truth.json",
            truth,
        )

        self.save_json(
            "qa_v8_7_4_execution_integrity.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run_only": True,
                "legacy_artifacts_read": [],
                "legacy_state_reused": False,
                "synthetic_evidence": (
                    truth["synthetic_evidence"]
                ),
                "foreign_evidence": (
                    truth["foreign_evidence"]
                ),
                "fabricated_backend_claims": (
                    truth[
                        "fabricated_backend_claims"
                    ]
                ),
            },
        )

    def report(
        self,
        truth: dict[str, Any],
    ) -> None:
        print()
        print("=" * 70)
        print("🧭 V8.7.4 SINGLE CANONICAL TRUTH")
        print("=" * 70)

        print(
            f"Version                 : {VERSION}"
        )
        print(
            f"Run ID                  : {self.run_id}"
        )
        print(
            f"Target                  : {self.target}"
        )

        print()
        print(
            f"Discovery pages         : "
            f"{truth['pages_discovered']}"
        )
        print(
            f"Behavioral surfaces     : "
            f"{truth['behavioral_surfaces']}"
        )
        print(
            f"Covered behaviors       : "
            f"{truth['behaviors_covered']}"
        )
        print(
            f"Unknown behaviors       : "
            f"{truth['unknown_behaviors']}"
        )
        print(
            f"Behavioral coverage     : "
            f"{truth['behavioral_coverage_percent']}%"
        )

        print()
        print(
            f"Business journeys       : "
            f"{truth['business_journeys']}"
        )
        print(
            f"Journey PASS            : "
            f"{truth['journey_pass']}"
        )
        print(
            f"Journey FAIL            : "
            f"{truth['journey_fail']}"
        )
        print(
            f"Journey execution cov.  : "
            f"{truth['journey_execution_coverage_percent']}%"
        )

        print()
        print(
            f"Executions              : "
            f"{truth['executions']}"
        )
        print(
            f"Execution PASS          : "
            f"{truth['execution_pass']}"
        )
        print(
            f"Execution FAIL          : "
            f"{truth['execution_fail']}"
        )

        print()
        print(
            f"Evidence records        : "
            f"{truth['evidence_records']}"
        )
        print(
            f"Observed outcomes       : "
            f"{truth['observed_outcomes']}"
        )
        print(
            f"Inferred outcomes       : "
            f"{truth['inferred_outcomes']}"
        )
        print(
            f"Unknown outcomes        : "
            f"{truth['unknown_outcomes']}"
        )
        print(
            f"Backend verified        : "
            f"{truth['backend_verified']}"
        )
        print(
            f"Business verified       : "
            f"{truth['business_verified']}"
        )

        print()
        print("-" * 70)
        print("🛡️ V8.7.4 TRUTH GATES")
        print("-" * 70)

        for key, value in (
            truth["truth_gates"].items()
        ):
            print(
                f"{key:30s}: "
                f"{'PASS' if value else 'FAIL'}"
            )

        print("-" * 70)

        if truth["release_truth"] == "PASS":
            print(
                "🟢 V8.7.4 RELEASE TRUTH GATE: PASS"
            )
        else:
            print(
                "🔴 V8.7.4 RELEASE TRUTH GATE: FAIL"
            )
            print(
                "   FAIL-CLOSED: release claims "
                "are NOT authorized."
            )

        print()
        print(
            "📄 Canonical truth: "
            f"{(self.report_dir / 'qa_v8_7_4_canonical_truth.json').resolve()}"
        )
        print(
            "📄 Report directory: "
            f"{self.report_dir.resolve()}"
        )

    async def run(self) -> int:
        self.log("=" * 70)
        self.log(
            "🚀 V8.7.4 LIVE DISCOVERY TIMEOUT FIX"
        )
        self.log("=" * 70)
        self.log(
            f"Version : {VERSION}"
        )
        self.log(
            f"Run ID  : {self.run_id}"
        )
        self.log(
            f"Target  : {self.target}"
        )
        self.log(
            "Legacy V8.x state: BLOCKED FROM READ"
        )

        try:
            async with async_playwright() as p:
                self.log(
                    "🌐 STARTING CHROMIUM"
                )

                try:
                    browser = await p.chromium.launch(
                        headless=self.headless
                    )
                except Exception as exc:
                    self.add_error(
                        "browser",
                        (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                    )

                    truth = self.calculate_truth()
                    self.save(truth)
                    self.report(truth)
                    return 3

                self.log(
                    "🟢 CHROMIUM READY"
                )

                try:
                    discovery_ok = (
                        await self.discover(
                            browser
                        )
                    )

                    if not discovery_ok:
                        self.log(
                            "🔴 STOP: current discovery "
                            "did not establish a valid "
                            "application model."
                        )

                        truth = (
                            self.calculate_truth()
                        )
                        self.save(truth)
                        self.report(truth)
                        return 4

                    self.build_behaviors()

                    if not self.behaviors:
                        self.add_error(
                            "behavior_model",
                            "CURRENT_BEHAVIOR_MODEL_EMPTY",
                        )

                        truth = (
                            self.calculate_truth()
                        )
                        self.save(truth)
                        self.report(truth)
                        return 5

                    self.build_journeys()

                    if not self.journeys:
                        self.add_error(
                            "journey_model",
                            "CURRENT_JOURNEY_MODEL_EMPTY",
                        )

                        truth = (
                            self.calculate_truth()
                        )
                        self.save(truth)
                        self.report(truth)
                        return 6

                    await self.execute_journeys(
                        browser
                    )

                    await self.close_unknowns(
                        browser
                    )

                    truth = (
                        self.calculate_truth()
                    )

                    self.save(truth)
                    self.report(truth)

                    return (
                        0
                        if truth["release_truth"]
                        == "PASS"
                        else 7
                    )

                finally:
                    await browser.close()

        except KeyboardInterrupt:
            self.add_error(
                "runtime",
                "KeyboardInterrupt",
            )

            truth = self.calculate_truth()
            self.save(truth)
            self.report(truth)
            return 130

        except Exception as exc:
            self.add_error(
                "runtime",
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
            )

            self.log(
                f"🔴 FATAL | {type(exc).__name__}: {exc}"
            )

            truth = self.calculate_truth()
            self.save(truth)
            self.report(truth)
            return 99


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "V8.7.4 live discovery + "
            "fail-closed QA truth engine"
        )
    )

    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
    )

    mode = parser.add_mutually_exclusive_group()

    mode.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless.",
    )

    mode.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium.",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
    )

    parser.add_argument(
        "--nav-timeout",
        type=int,
        default=DEFAULT_NAV_TIMEOUT_MS,
        help="Navigation lifecycle timeout.",
    )

    parser.add_argument(
        "--dom-timeout",
        type=int,
        default=DEFAULT_DOM_TIMEOUT_MS,
        help="DOM availability timeout.",
    )

    # Backward-compatible alias.
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Sets navigation and DOM timeout.",
    )

    parser.add_argument(
        "--journey-timeout",
        type=int,
        default=DEFAULT_JOURNEY_TIMEOUT_MS,
    )

    parser.add_argument(
        "--unknown-budget",
        type=int,
        default=DEFAULT_UNKNOWN_BUDGET,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    nav_timeout = (
        args.timeout
        if args.timeout is not None
        else args.nav_timeout
    )

    dom_timeout = (
        args.timeout
        if args.timeout is not None
        else args.dom_timeout
    )

    agent = Agent(
        target=args.url,
        headless=not args.headed,
        max_pages=args.max_pages,
        nav_timeout_ms=nav_timeout,
        dom_timeout_ms=dom_timeout,
        journey_timeout_ms=args.journey_timeout,
        unknown_budget=args.unknown_budget,
    )

    return asyncio.run(
        agent.run()
    )


if __name__ == "__main__":
    raise SystemExit(main())
