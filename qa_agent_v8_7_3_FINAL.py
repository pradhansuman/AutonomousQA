#!/usr/bin/env python3
"""
V8.7.3 — TRUTH ISOLATION + TARGETED CLOSURE

Purpose
-------
1. Discover the target live, from scratch, on every run.
2. Never read prior V8.x artifacts as test truth.
3. Canonicalize URLs and reject invalid routes such as /None.
4. Build a current-run behavioral model.
5. Rank unknown behaviors and execute targeted closure.
6. Reproduce failed journeys without auto-passing them.
7. Treat backend/business outcomes as UNKNOWN unless directly observed.
8. Produce exactly one current-run canonical truth artifact.
9. Fail closed when truth requirements are not met.

This version intentionally does NOT import or read:
    qa_v8_5_report/
    qa_v8_6_report/
    qa_v8_7_report/
    any previous canonical truth file

Only the current process/run creates evidence.
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
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


VERSION = "8.7.3"
DEFAULT_URL = "https://demoqa.com"
DEFAULT_MAX_PAGES = 50
DEFAULT_TIMEOUT_MS = 20000
DEFAULT_ACTION_TIMEOUT_MS = 5000
DEFAULT_JOURNEY_TIMEOUT_MS = 15000
DEFAULT_UNKNOWN_BUDGET = 60

# IMPORTANT: this is a new directory. No previous report is consumed.
REPORT_DIR = Path("qa_v8_7_3_report")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def canonical_url(raw: Any, base: str | None = None) -> str | None:
    """Canonicalize an HTTP(S) URL. Invalid/null hrefs become None."""
    if raw is None:
        return None

    value = str(raw).strip()
    if not value or value.lower() in {
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

        # Do not preserve default ports.
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

        return urlunparse((scheme, netloc, path, "", p.query, ""))

    except Exception:
        return None


def same_host(url: str, target: str) -> bool:
    return (urlparse(url).hostname or "").lower() == (
        urlparse(target).hostname or ""
    ).lower()


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
class Page:
    url: str
    title: str
    elements: list[Element]
    links: list[str]
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
        timeout_ms: int,
        journey_timeout_ms: int,
        unknown_budget: int,
    ):
        target = canonical_url(target)
        if not target:
            raise ValueError(f"Invalid target URL: {target}")

        self.target = target
        self.headless = headless
        self.max_pages = max(1, max_pages)
        self.timeout_ms = max(1000, timeout_ms)
        self.action_timeout_ms = min(
            DEFAULT_ACTION_TIMEOUT_MS, self.timeout_ms
        )
        self.journey_timeout_ms = max(1000, journey_timeout_ms)
        self.unknown_budget = max(1, unknown_budget)

        self.run_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()

        self.report_dir = REPORT_DIR
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self.pages: list[Page] = []
        self.behaviors: list[Behavior] = []
        self.journeys: list[Journey] = []
        self.journey_results: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

        self.seen_urls: set[str] = set()
        self.queued_urls: set[str] = set()
        self.queue: list[str] = []

        # Explicit isolation markers.
        self.legacy_state_reused = False
        self.foreign_evidence = False
        self.synthetic_evidence = False
        self.fabricated_backend_claims = False

    def log(self, text: str) -> None:
        elapsed = time.monotonic() - self.started_monotonic
        print(f"[{elapsed:7.1f}s] {text}", flush=True)

    def error(self, phase: str, message: str, url: str | None = None) -> None:
        self.errors.append(
            {
                "phase": phase,
                "message": message,
                "url": url,
                "timestamp": utc_now(),
                "current_run": True,
            }
        )

    def evidence_record(
        self,
        evidence_type: str,
        *,
        url: str | None = None,
        behavior_id: str | None = None,
        journey_id: str | None = None,
        observed: bool = True,
        details: dict[str, Any] | None = None,
    ) -> str:
        evidence_id = sha(
            f"{self.run_id}|{len(self.evidence)}|{evidence_type}|"
            f"{url}|{behavior_id}|{journey_id}"
        )
        record = {
            "evidence_id": evidence_id,
            "run_id": self.run_id,
            "version": VERSION,
            "type": evidence_type,
            "url": url,
            "behavior_id": behavior_id,
            "journey_id": journey_id,
            "observed": bool(observed),
            "current_run": True,
            "timestamp": utc_now(),
            "details": details or {},
        }
        self.evidence.append(record)
        return evidence_id

    async def discover_page(self, page, url: str) -> Page | None:
        self.log(
            f"🔎 DISCOVER [{len(self.seen_urls)}/{self.max_pages}] {url}"
        )

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            status = response.status if response else None
            if status is not None and status >= 400:
                self.log(f"⚠️ HTTP {status} | {url}")

            await page.wait_for_timeout(200)

            final_url = canonical_url(page.url, url)
            if not final_url or not same_host(final_url, self.target):
                self.error(
                    "discovery",
                    "invalid_or_foreign_final_url",
                    url,
                )
                return None

            title = await page.title()

            raw = await page.locator(
                "button,input,textarea,select,a,[role],[contenteditable='true']"
            ).evaluate_all(
                """
                els => els.map((e, i) => {
                    const r = e.getBoundingClientRect();
                    const s = getComputedStyle(e);
                    return {
                        i,
                        tag: (e.tagName || '').toLowerCase(),
                        role: e.getAttribute('role') || '',
                        text: (e.innerText || e.textContent || '').trim().slice(0,200),
                        name: e.getAttribute('name') || '',
                        aria: e.getAttribute('aria-label') || '',
                        placeholder: e.getAttribute('placeholder') || '',
                        type: e.getAttribute('type') || '',
                        id: e.id || '',
                        href: e.getAttribute('href'),
                        disabled: !!e.disabled,
                        visible: !!(r.width && r.height) &&
                            s.visibility !== 'hidden' &&
                            s.display !== 'none'
                    };
                })
                """
            )

            elements: list[Element] = []

            for item in raw or []:
                tag = str(item.get("tag") or "").lower()
                role = str(item.get("role") or "").lower()
                text = str(item.get("text") or "").strip()
                name = str(item.get("name") or "").strip()
                aria = str(item.get("aria") or "").strip()
                placeholder = str(item.get("placeholder") or "").strip()
                input_type = str(item.get("type") or "").lower()
                element_id = str(item.get("id") or "").strip()

                visible = bool(item.get("visible"))
                disabled = bool(item.get("disabled"))

                label = aria or name or placeholder or text

                if tag == "a":
                    semantic = "navigation"
                    action = "navigate"
                elif tag in {"input", "textarea", "select"} or role in {
                    "textbox", "combobox"
                }:
                    semantic = "input"
                    action = "fill"
                elif role in {
                    "button", "checkbox", "radio", "tab", "slider"
                } or tag == "button":
                    semantic = "action"
                    action = "click"
                else:
                    semantic = "interactive"
                    action = "click"

                if element_id:
                    selector = f"#{element_id}"
                elif name:
                    selector = f'[name="{name.replace(chr(34), chr(92)+chr(34))}"]'
                elif tag:
                    selector = tag
                else:
                    selector = "*"

                href = canonical_url(item.get("href"), final_url)

                element_key = (
                    f"{final_url}|{tag}|{role}|{element_id}|{name}|"
                    f"{label[:100]}|{selector}"
                )

                elements.append(
                    Element(
                        element_id=sha(element_key),
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

            hrefs = await page.locator("a[href]").evaluate_all(
                "els => els.map(e => e.getAttribute('href'))"
            )

            links: set[str] = set()
            for href in hrefs or []:
                candidate = canonical_url(href, final_url)
                if candidate and same_host(candidate, self.target):
                    links.add(candidate)

            result = Page(
                url=final_url,
                title=title,
                elements=elements,
                links=sorted(links),
            )

            self.log(
                f"✅ DISCOVERED | {result.url} | "
                f"elements={len(result.elements)} | links={len(result.links)}"
            )
            return result

        except PlaywrightTimeoutError:
            self.error("discovery", "timeout", url)
            self.log(f"❌ DISCOVERY TIMEOUT | {url}")
            return None
        except Exception as exc:
            self.error(
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
        self.log("🗺️ CURRENT-RUN DISCOVERY")
        self.log("Legacy artifacts: NOT READ")
        self.log("Current-run evidence: REQUIRED")

        page = await browser.new_page()
        page.set_default_timeout(self.timeout_ms)

        start = canonical_url(self.target)
        assert start

        self.queue = [start]
        self.queued_urls = {start}

        try:
            while self.queue and len(self.seen_urls) < self.max_pages:
                raw = self.queue.pop(0)
                self.queued_urls.discard(raw)

                url = canonical_url(raw, self.target)

                if not url:
                    self.log(f"🧹 SKIP INVALID URL | {raw!r}")
                    continue

                if not same_host(url, self.target):
                    self.log(f"🧹 SKIP FOREIGN URL | {url}")
                    continue

                if url in self.seen_urls:
                    continue

                self.seen_urls.add(url)

                model = await self.discover_page(page, url)
                if model is None:
                    continue

                if model.url in {p.url for p in self.pages}:
                    continue

                self.pages.append(model)

                for link in model.links:
                    if (
                        link not in self.seen_urls
                        and link not in self.queued_urls
                        and len(self.seen_urls) + len(self.queue)
                        < self.max_pages * 2
                    ):
                        self.queue.append(link)
                        self.queued_urls.add(link)

            canonical_urls = [p.url for p in self.pages]

            if not self.pages:
                self.error("discovery", "CURRENT_DISCOVERY_EMPTY")
                return False

            if any(
                not p.current_run or canonical_url(p.url) is None
                for p in self.pages
            ):
                self.error("discovery", "INVALID_CURRENT_MODEL")
                return False

            if len(canonical_urls) != len(set(canonical_urls)):
                self.error("discovery", "DUPLICATE_CANONICAL_URLS")
                return False

            self.log(
                f"📚 DISCOVERY COMPLETE | pages={len(self.pages)} | "
                f"unique={len(set(canonical_urls))}"
            )
            return True

        finally:
            await page.close()

    def build_behaviors(self) -> None:
        self.log("=" * 70)
        self.log("🧠 BUILD CURRENT-RUN BEHAVIOR MODEL")

        behaviors: dict[str, Behavior] = {}

        for page in self.pages:
            for element in page.elements:
                if not element.current_run:
                    continue
                if not element.visible or element.disabled:
                    continue

                if element.semantic == "navigation":
                    action = "navigate"
                    base_risk = 35.0
                elif element.semantic == "input":
                    action = "fill"
                    base_risk = 55.0
                else:
                    action = "click"
                    base_risk = 50.0

                risk = base_risk

                if any(
                    x in (
                        element.label
                        + " "
                        + element.text
                        + " "
                        + page.url
                    ).lower()
                    for x in (
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

                key = (
                    f"{page.url}|{element.element_id}|"
                    f"{action}|{element.selector}"
                )
                behavior_id = sha(key)

                behaviors[behavior_id] = Behavior(
                    behavior_id=behavior_id,
                    page_url=page.url,
                    semantic=element.semantic,
                    action=action,
                    selector=element.selector,
                    label=element.label,
                    risk=min(100.0, risk),
                )

        self.behaviors = sorted(
            behaviors.values(),
            key=lambda b: (-b.risk, b.page_url, b.behavior_id),
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
            by_page.setdefault(behavior.page_url, []).append(behavior)

        journeys: list[Journey] = []

        for page_url, behaviors in by_page.items():
            inputs = [b for b in behaviors if b.action == "fill"]
            clicks = [b for b in behaviors if b.action == "click"]
            navigations = [b for b in behaviors if b.action == "navigate"]

            if inputs and clicks:
                goal = "Submit valid user information"
                selected = inputs[:2] + clicks[:1]
            elif "upload-download" in page_url and clicks:
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
            source_ids = [b.behavior_id for b in selected]

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
                    max(b.risk for b in selected)
                    + len(selected) * 3.0,
                ),
                2,
            )

            journey_id = sha(
                f"{page_url}|{goal}|{','.join(source_ids)}"
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
            key=lambda j: (-j.score, j.url, j.journey_id),
        )

        self.log(
            f"🧭 JOURNEY MODEL COMPLETE | journeys={len(self.journeys)}"
        )

    async def execute_behavior(
        self,
        page,
        behavior: Behavior,
        journey_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Execute one behavior and return (passed, evidence_id)."""
        locator = page.locator(behavior.selector).first

        try:
            await locator.wait_for(
                state="visible",
                timeout=self.action_timeout_ms,
            )

            if behavior.action == "fill":
                await locator.fill("QA_AUTONOMOUS_TEST")

            elif behavior.action == "click":
                await locator.click(
                    timeout=self.action_timeout_ms,
                    no_wait_after=True,
                )

            elif behavior.action == "navigate":
                href = await locator.get_attribute("href")
                target = canonical_url(href, page.url)
                if not target or not same_host(target, self.target):
                    raise RuntimeError("INVALID_NAVIGATION_TARGET")
                await page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )

            else:
                raise RuntimeError(
                    f"UNSUPPORTED_ACTION:{behavior.action}"
                )

            evidence_id = self.evidence_record(
                "UI_BEHAVIOR_OBSERVED",
                url=page.url,
                behavior_id=behavior.behavior_id,
                journey_id=journey_id,
                observed=True,
                details={
                    "action": behavior.action,
                    "selector": behavior.selector,
                    "label": behavior.label,
                },
            )

            behavior.status = "COVERED"
            behavior.evidence_ids.append(evidence_id)
            return True, evidence_id

        except Exception as exc:
            self.evidence_record(
                "BEHAVIOR_EXECUTION_ERROR",
                url=page.url,
                behavior_id=behavior.behavior_id,
                journey_id=journey_id,
                observed=True,
                details={
                    "action": behavior.action,
                    "selector": behavior.selector,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return False, None

    async def execute_journey(
        self,
        context,
        journey: Journey,
        ordinal: int,
        retry: bool = False,
    ) -> dict[str, Any]:
        label = f"[{ordinal}/{len(self.journeys)}]"
        retry_text = " RETRY" if retry else ""

        self.log(
            f"{label} ▶ {journey.goal}{retry_text} | "
            f"{journey.url}"
        )

        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)

        started = time.monotonic()
        step_results: list[dict[str, Any]] = []
        error: str | None = None

        async def run_steps():
            nonlocal error

            response = await page.goto(
                journey.url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            status = response.status if response else None
            if status is not None and status >= 400:
                raise RuntimeError(f"HTTP_STATUS_{status}")

            self.evidence_record(
                "UI_ENTRY_OBSERVED",
                url=page.url,
                journey_id=journey.journey_id,
                observed=True,
                details={"http_status": status},
            )

            for step in journey.steps:
                behavior = next(
                    (
                        b for b in self.behaviors
                        if b.behavior_id == step["behavior_id"]
                    ),
                    None,
                )

                if behavior is None:
                    raise RuntimeError(
                        f"MISSING_CURRENT_BEHAVIOR:{step['behavior_id']}"
                    )

                passed, evidence_id = await self.execute_behavior(
                    page,
                    behavior,
                    journey.journey_id,
                )

                step_results.append(
                    {
                        "step": step["step"],
                        "behavior_id": behavior.behavior_id,
                        "action": behavior.action,
                        "selector": behavior.selector,
                        "status": "PASS" if passed else "FAIL",
                        "evidence_id": evidence_id,
                    }
                )

                if not passed:
                    raise RuntimeError(
                        f"BEHAVIOR_FAILED:{behavior.behavior_id}"
                    )

        try:
            await asyncio.wait_for(
                run_steps(),
                timeout=self.journey_timeout_ms / 1000,
            )
            status = "PASS"
            self.log(f"{label}   ✅ JOURNEY PASS")
        except asyncio.TimeoutError:
            status = "FAIL"
            error = "JOURNEY_TIMEOUT"
            self.log(f"{label}   ❌ JOURNEY FAIL | {error}")
        except Exception as exc:
            status = "FAIL"
            error = f"{type(exc).__name__}: {exc}"
            self.log(f"{label}   ❌ JOURNEY FAIL | {error}")
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
                (time.monotonic() - started) * 1000, 2
            ),
            "steps": step_results,
            "current_run": True,
            "backend_verified": False,
            "business_verified": False,
            "backend_outcome": "UNKNOWN",
            "business_outcome": "UNKNOWN",
            "timestamp": utc_now(),
        }

    async def execute(self, browser) -> None:
        self.log("=" * 70)
        self.log("🚀 EXECUTE CURRENT-RUN JOURNEYS")

        context = await browser.new_context()

        try:
            for i, journey in enumerate(self.journeys, 1):
                result = await self.execute_journey(
                    context, journey, i
                )
                self.journey_results.append(result)

                # Failed journeys are NOT auto-passed.
                if result["status"] == "FAIL":
                    journey.status = "FAIL"
                else:
                    journey.status = "PASS"

            failed = [
                r for r in self.journey_results
                if r["status"] == "FAIL"
            ]

            # Targeted reproduction of failed journeys.
            if failed:
                self.log("=" * 70)
                self.log(
                    f"🔬 TARGETED REPRODUCTION | failed={len(failed)}"
                )

                original_by_id = {
                    j.journey_id: j for j in self.journeys
                }

                for idx, failed_result in enumerate(failed, 1):
                    journey = original_by_id.get(
                        failed_result["journey_id"]
                    )
                    if not journey:
                        continue

                    retry_result = await self.execute_journey(
                        context,
                        journey,
                        idx,
                        retry=True,
                    )

                    retry_result["reproduces_original_failure"] = (
                        retry_result["status"] == "FAIL"
                    )
                    self.journey_results.append(retry_result)

                    if retry_result["status"] == "PASS":
                        self.log(
                            f"🔬 RETRY NOT REPRODUCED | "
                            f"{journey.url}"
                        )
                    else:
                        self.log(
                            f"🔴 RETRY REPRODUCED FAILURE | "
                            f"{journey.url}"
                        )

        finally:
            await context.close()

    async def targeted_unknown_closure(self, browser) -> None:
        """Exercise highest-risk unknown behaviors not already covered."""
        self.log("=" * 70)
        self.log("🎯 TARGETED UNKNOWN-BEHAVIOR CLOSURE")

        unknown = [
            b for b in self.behaviors
            if b.status == "UNKNOWN"
        ]

        unknown.sort(
            key=lambda b: (-b.risk, b.page_url, b.behavior_id)
        )

        target_count = min(self.unknown_budget, len(unknown))

        if target_count == 0:
            self.log("🎯 No unknown behaviors available for closure")
            return

        context = await browser.new_context()

        try:
            for i, behavior in enumerate(
                unknown[:target_count], 1
            ):
                self.log(
                    f"🎯 UNKNOWN [{i}/{target_count}] "
                    f"risk={behavior.risk:.1f} | "
                    f"{behavior.page_url} | "
                    f"{behavior.action} | "
                    f"{behavior.label[:80]}"
                )

                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)

                try:
                    await page.goto(
                        behavior.page_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )

                    passed, evidence_id = await self.execute_behavior(
                        page,
                        behavior,
                    )

                    if passed:
                        self.log(
                            f"   🟢 CLOSED | evidence={evidence_id}"
                        )
                    else:
                        self.log(
                            "   🔴 NOT CLOSED | execution evidence exists"
                        )

                except Exception as exc:
                    self.error(
                        "unknown_closure",
                        f"{type(exc).__name__}: {exc}",
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

    def truth(self) -> dict[str, Any]:
        discovered = len(self.behaviors)
        covered = sum(
            1 for b in self.behaviors
            if b.status == "COVERED"
        )
        unknown = discovered - covered

        # IMPORTANT:
        # Only directly observed evidence can produce an observed outcome.
        observed_outcomes = sum(
            1
            for r in self.journey_results
            if r.get("backend_outcome") == "OBSERVED"
            or r.get("business_outcome") == "OBSERVED"
        )

        inferred_outcomes = 0

        # Backend/business are deliberately never inferred here.
        # Any non-UNKNOWN value would be a violation unless directly
        # populated by an explicit observation mechanism.
        backend_verified = sum(
            1 for r in self.journey_results
            if r.get("backend_verified") is True
        )
        business_verified = sum(
            1 for r in self.journey_results
            if r.get("business_verified") is True
        )

        fabricated_backend = any(
            (
                r.get("backend_verified") is True
                and not any(
                    e.get("type") == "BACKEND_OBSERVED"
                    and e.get("journey_id") == r.get("journey_id")
                    and e.get("current_run") is True
                    for e in self.evidence
                )
            )
            for r in self.journey_results
        )

        # There should be no foreign evidence in a current-only run.
        foreign = any(
            e.get("current_run") is not True
            or e.get("run_id") != self.run_id
            for e in self.evidence
        )

        # No synthetic evidence is generated as a substitute for observation.
        synthetic = any(
            e.get("type") in {
                "SYNTHETIC",
                "INFERRED_PASS",
                "ASSUMED_BACKEND_PASS",
            }
            for e in self.evidence
        )

        # Current-run execution results only.
        actual_journeys = [
            r for r in self.journey_results
            if r.get("retry") is not True
        ]

        journey_pass = sum(
            1 for r in actual_journeys if r["status"] == "PASS"
        )
        journey_fail = sum(
            1 for r in actual_journeys if r["status"] == "FAIL"
        )

        execution_total = len(actual_journeys)
        execution_fail = journey_fail

        coverage = (
            round((covered / discovered) * 100, 2)
            if discovered else None
        )

        gates = {
            "current_discovery": len(self.pages) > 0,
            "canonical_model": (
                discovered > 0
                and all(b.current_run for b in self.behaviors)
            ),
            "legacy_state_reused": not self.legacy_state_reused,
            "synthetic_evidence": not synthetic,
            "foreign_evidence": not foreign,
            "execution_failures": execution_fail == 0,
            "journey_failures": journey_fail == 0,
            "unknown_behaviors": unknown == 0,
            "evidence_exists": (
                len(self.evidence) > 0
                if execution_total > 0
                else True
            ),
            "fabricated_backend_claims": (
                not fabricated_backend
                and backend_verified == 0
            ),
        }

        release_pass = all(gates.values())

        return {
            "version": VERSION,
            "run_id": self.run_id,
            "target": self.target,
            "generated_at": utc_now(),

            "pages_discovered": len(self.pages),
            "behavioral_surfaces": discovered,
            "behaviors_covered": covered,
            "unknown_behaviors": unknown,
            "behavioral_coverage_percent": coverage,

            "business_journeys": len(self.journeys),
            "journey_pass": journey_pass,
            "journey_fail": journey_fail,
            "journey_execution_coverage_percent": (
                round(
                    execution_total / len(self.journeys) * 100,
                    2,
                )
                if self.journeys
                else None
            ),

            "executions": execution_total,
            "execution_pass": execution_total - execution_fail,
            "execution_fail": execution_fail,

            "evidence_records": len(self.evidence),
            "observed_outcomes": observed_outcomes,
            "inferred_outcomes": inferred_outcomes,
            "unknown_outcomes": (
                len(self.journeys)
                - observed_outcomes
                - inferred_outcomes
            ),
            "backend_verified": backend_verified,
            "business_verified": business_verified,

            "legacy_state_reused": self.legacy_state_reused,
            "synthetic_evidence": synthetic,
            "foreign_evidence": foreign,
            "fabricated_backend_claims": fabricated_backend,
            "evidence_violations": (
                int(foreign) + int(synthetic) + int(fabricated_backend)
            ),

            "truth_gates": gates,
            "release_truth": "PASS" if release_pass else "FAIL",
            "release_blocked": not release_pass,
            "fail_closed": True,

            "secret_safety": {
                "credentials": False,
                "tokens": False,
                "cookies": False,
            },

            "errors": self.errors,
        }

    def save(self, truth: dict[str, Any]) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self.save_json(
            "qa_current_application_model_v8_7_3.json",
            {
                "run_id": self.run_id,
                "version": VERSION,
                "current_run": True,
                "pages": [asdict(p) for p in self.pages],
            },
        )

        self.save_json(
            "qa_current_behavior_model_v8_7_3.json",
            {
                "run_id": self.run_id,
                "version": VERSION,
                "current_run": True,
                "behaviors": [asdict(b) for b in self.behaviors],
            },
        )

        self.save_json(
            "qa_current_journey_model_v8_7_3.json",
            {
                "run_id": self.run_id,
                "version": VERSION,
                "current_run": True,
                "journeys": [asdict(j) for j in self.journeys],
            },
        )

        self.save_json(
            "qa_current_execution_evidence_v8_7_3.json",
            {
                "run_id": self.run_id,
                "version": VERSION,
                "current_run": True,
                "journey_results": self.journey_results,
                "evidence": self.evidence,
            },
        )

        self.save_json(
            "qa_v8_7_3_canonical_truth.json",
            truth,
        )

        self.save_json(
            "qa_v8_7_3_execution_integrity.json",
            {
                "run_id": self.run_id,
                "version": VERSION,
                "current_run_only": True,
                "legacy_state_reused": False,
                "legacy_artifacts_read": [],
                "synthetic_evidence": False,
                "foreign_evidence": False,
                "fabricated_backend_claims": False,
                "truth": truth,
            },
        )

    def save_json(self, name: str, data: Any) -> None:
        (self.report_dir / name).write_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

    def report(self, truth: dict[str, Any]) -> None:
        print()
        print("=" * 70)
        print("🧭 V8.7.3 SINGLE CANONICAL TRUTH")
        print("=" * 70)
        print(f"Version                 : {VERSION}")
        print(f"Run ID                  : {self.run_id}")
        print(f"Target                  : {self.target}")
        print()
        print(f"Discovery pages         : {truth['pages_discovered']}")
        print(f"Behavioral surfaces     : {truth['behavioral_surfaces']}")
        print(f"Covered behaviors       : {truth['behaviors_covered']}")
        print(f"Unknown behaviors       : {truth['unknown_behaviors']}")
        print(
            f"Behavioral coverage     : "
            f"{truth['behavioral_coverage_percent']}%"
        )
        print()
        print(f"Business journeys       : {truth['business_journeys']}")
        print(f"Journey PASS            : {truth['journey_pass']}")
        print(f"Journey FAIL            : {truth['journey_fail']}")
        print(
            f"Journey execution cov.  : "
            f"{truth['journey_execution_coverage_percent']}%"
        )
        print()
        print(f"Executions              : {truth['executions']}")
        print(f"Execution PASS          : {truth['execution_pass']}")
        print(f"Execution FAIL          : {truth['execution_fail']}")
        print()
        print(f"Evidence records        : {truth['evidence_records']}")
        print(f"Observed outcomes       : {truth['observed_outcomes']}")
        print(f"Inferred outcomes       : {truth['inferred_outcomes']}")
        print(f"Unknown outcomes        : {truth['unknown_outcomes']}")
        print(f"Backend verified        : {truth['backend_verified']}")
        print(f"Business verified       : {truth['business_verified']}")

        print()
        print("-" * 70)
        print("🛡️ V8.7.3 TRUTH GATES")
        print("-" * 70)

        for key, value in truth["truth_gates"].items():
            print(
                f"{key:30s}: "
                f"{'PASS' if value else 'FAIL'}"
            )

        print("-" * 70)

        if truth["release_truth"] == "PASS":
            print("🟢 V8.7.3 RELEASE TRUTH GATE: PASS")
        else:
            print("🔴 V8.7.3 RELEASE TRUTH GATE: FAIL")
            print("   FAIL-CLOSED: release claims are NOT authorized.")

        print()
        print(
            f"📄 Canonical truth: "
            f"{(self.report_dir / 'qa_v8_7_3_canonical_truth.json').resolve()}"
        )
        print(
            f"📄 Report directory: {self.report_dir.resolve()}"
        )

    async def run(self) -> int:
        self.log("=" * 70)
        self.log("🚀 V8.7.3 TRUTH ISOLATION + TARGETED CLOSURE")
        self.log("=" * 70)
        self.log(f"Version : {VERSION}")
        self.log(f"Run ID  : {self.run_id}")
        self.log(f"Target  : {self.target}")
        self.log("Legacy V8.x state: BLOCKED FROM READ")

        browser = None

        try:
            async with async_playwright() as p:
                try:
                    self.log("🌐 STARTING CHROMIUM")
                    browser = await p.chromium.launch(
                        headless=self.headless
                    )
                    self.log("🟢 CHROMIUM READY")
                except Exception as exc:
                    self.error(
                        "browser",
                        f"{type(exc).__name__}: {exc}",
                    )
                    self.log(
                        f"🔴 BROWSER START FAILED | "
                        f"{type(exc).__name__}: {exc}"
                    )
                    truth = self.truth()
                    self.save(truth)
                    self.report(truth)
                    return 3

                discovery_ok = await self.discover(browser)

                if not discovery_ok:
                    self.log(
                        "🔴 STOP: current discovery did not "
                        "establish a valid application model."
                    )
                    truth = self.truth()
                    self.save(truth)
                    self.report(truth)
                    return 4

                self.build_behaviors()

                if not self.behaviors:
                    self.error(
                        "behavior_model",
                        "CURRENT_BEHAVIOR_MODEL_EMPTY",
                    )
                    self.log(
                        "🔴 STOP: no executable current-run behaviors."
                    )
                    truth = self.truth()
                    self.save(truth)
                    self.report(truth)
                    return 5

                self.build_journeys()

                if not self.journeys:
                    self.error(
                        "journey_model",
                        "CURRENT_JOURNEY_MODEL_EMPTY",
                    )
                    self.log(
                        "🔴 STOP: no executable current-run journeys."
                    )
                    truth = self.truth()
                    self.save(truth)
                    self.report(truth)
                    return 6

                await self.execute(browser)

                # Closure happens AFTER baseline journey execution.
                await self.targeted_unknown_closure(browser)

                truth = self.truth()
                self.save(truth)
                self.report(truth)

                return 0 if truth["release_truth"] == "PASS" else 7

        except KeyboardInterrupt:
            self.error("runtime", "KeyboardInterrupt")
            truth = self.truth()
            self.save(truth)
            self.report(truth)
            return 130

        except Exception as exc:
            self.error(
                "runtime",
                f"{type(exc).__name__}: {exc}",
            )
            self.log(
                f"🔴 FATAL | {type(exc).__name__}: {exc}"
            )
            truth = self.truth()
            self.save(truth)
            self.report(truth)
            return 99


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--headless",
        action="store_true",
    )
    group.add_argument(
        "--headed",
        action="store_true",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
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
        help="Maximum high-risk unknown behaviors to target.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    agent = Agent(
        target=args.url,
        headless=not args.headed,
        max_pages=args.max_pages,
        timeout_ms=args.timeout,
        journey_timeout_ms=args.journey_timeout,
        unknown_budget=args.unknown_budget,
    )

    return asyncio.run(agent.run())


if __name__ == "__main__":
    raise SystemExit(main())
