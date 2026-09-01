#!/usr/bin/env python3
"""
V8.7.5
SEMANTIC BEHAVIOR MODEL + DEDUPLICATION + SPECIALIZED EXECUTORS

Design goals:
  1. Current-run discovery only.
  2. Normalize raw DOM controls into semantic behavior classes.
  3. Deduplicate equivalent controls/URLs.
  4. Treat query-string variants as one application surface where appropriate.
  5. Use specialized executors for select, slider, checkbox, radio, tabs,
     accordions, alerts, uploads, and new tabs/windows.
  6. Never convert UNKNOWN to PASS without direct current-run evidence.
  7. Backend/business outcomes stay UNKNOWN unless directly observed.
  8. Failed journeys remain failed until reproduced or disproved.
  9. Release is fail-closed.

This is a standalone replacement for V8.7.4.
It intentionally does not read prior V8.x reports, maps, models, or truth files.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urldefrag, urlparse, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


VERSION = "8.7.5"
REPORT_DIR = Path("qa_v8_7_5_report")

DEFAULT_MAX_PAGES = 50
DEFAULT_NAV_TIMEOUT = 12000
DEFAULT_DOM_TIMEOUT = 6000
DEFAULT_ACTION_TIMEOUT = 3500
DEFAULT_JOURNEY_TIMEOUT = 12000
DEFAULT_UNKNOWN_BUDGET = 60


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def canonical_url(raw: Any, base: str | None = None, drop_query: bool = False) -> str | None:
    if raw is None:
        return None

    value = str(raw).strip()

    if not value:
        return None

    if value.lower() in {"none", "null", "undefined", "javascript:void(0)", "#"}:
        return None

    if base:
        value = urljoin(base, value)

    try:
        value, _ = urldefrag(value)
        p = urlparse(value)

        scheme = p.scheme.lower()
        host = (p.hostname or "").lower()

        if scheme not in {"http", "https"} or not host:
            return None

        path = re.sub(r"/{2,}", "/", p.path or "/")

        if not path.startswith("/"):
            path = "/" + path

        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        query = ""
        if not drop_query:
            pairs = parse_qsl(p.query, keep_blank_values=True)
            pairs.sort()
            query = urlencode(pairs, doseq=True)

        netloc = host
        if p.port and not (
            (scheme == "http" and p.port == 80)
            or (scheme == "https" and p.port == 443)
        ):
            netloc = f"{host}:{p.port}"

        return urlunparse((scheme, netloc, path, "", query, ""))

    except Exception:
        return None


def surface_url(url: str) -> str:
    """Application-surface identity: path, not transient search/query state."""
    result = canonical_url(url, drop_query=True)
    return result or url


def same_host(a: str, b: str) -> bool:
    return (urlparse(a).hostname or "").lower() == (urlparse(b).hostname or "").lower()


@dataclass
class Element:
    element_id: str
    tag: str
    role: str
    label: str
    text: str
    name: str
    placeholder: str
    input_type: str
    selector: str
    href: str | None
    visible: bool
    disabled: bool
    semantic: str
    behavior_key: str
    current_run: bool = True


@dataclass
class PageModel:
    url: str
    surface: str
    title: str
    elements: list[Element]
    links: list[str]
    http_status: int | None
    navigation_mode: str
    current_run: bool = True


@dataclass
class Behavior:
    behavior_id: str
    page_surface: str
    semantic: str
    action: str
    selector: str
    label: str
    behavior_key: str
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


class QAAgent:
    def __init__(self, args):
        target = canonical_url(args.url)
        if not target:
            raise ValueError(f"Invalid URL: {args.url}")

        self.target = target
        self.headless = not args.headed
        self.max_pages = max(1, args.max_pages)
        self.nav_timeout = max(1000, args.nav_timeout)
        self.dom_timeout = max(1000, args.dom_timeout)
        self.action_timeout = max(1000, args.action_timeout)
        self.journey_timeout = max(1000, args.journey_timeout)
        self.unknown_budget = max(1, args.unknown_budget)

        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()

        self.report_dir = REPORT_DIR
        self.report_dir.mkdir(parents=True, exist_ok=True)

        self.pages: list[PageModel] = []
        self.behaviors: list[Behavior] = []
        self.journeys: list[Journey] = []
        self.journey_results: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

        self.queue: list[str] = []
        self.queued: set[str] = set()
        self.seen: set[str] = set()

        self.legacy_artifacts_read: list[str] = []
        self.legacy_state_reused = False
        self.synthetic_evidence = False
        self.foreign_evidence = False
        self.fabricated_backend_claims = False

    def log(self, msg: str):
        print(f"[{time.monotonic() - self.started:7.1f}s] {msg}", flush=True)

    def error(self, phase: str, message: str, url: str | None = None):
        self.errors.append(
            {
                "run_id": self.run_id,
                "version": VERSION,
                "current_run": True,
                "timestamp": now(),
                "phase": phase,
                "message": message,
                "url": url,
            }
        )

    def evidence_add(
        self,
        kind: str,
        url: str | None = None,
        behavior_id: str | None = None,
        journey_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        eid = sha(
            f"{self.run_id}|{len(self.evidence)}|{kind}|"
            f"{url}|{behavior_id}|{journey_id}"
        )

        self.evidence.append(
            {
                "evidence_id": eid,
                "run_id": self.run_id,
                "version": VERSION,
                "current_run": True,
                "type": kind,
                "url": url,
                "behavior_id": behavior_id,
                "journey_id": journey_id,
                "observed": True,
                "timestamp": now(),
                "details": details or {},
            }
        )
        return eid

    async def navigate(self, page, url: str):
        response = None
        mode = "domcontentloaded"

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.nav_timeout,
            )
        except PlaywrightTimeoutError:
            mode = "usable-document-fallback"

            try:
                if await page.locator("body").count() == 0:
                    response = await page.goto(
                        url,
                        wait_until="commit",
                        timeout=self.nav_timeout,
                    )
            except Exception:
                raise

        await page.wait_for_selector(
            "body",
            state="attached",
            timeout=self.dom_timeout,
        )

        status = response.status if response else None
        return response, status, mode

    async def collect_page(self, page, url: str) -> PageModel | None:
        self.log(
            f"🔎 DISCOVER [{len(self.pages) + 1}/{self.max_pages}] {url}"
        )

        try:
            response, status, mode = await self.navigate(page, url)

            final = canonical_url(page.url, url)
            if not final or not same_host(final, self.target):
                self.error("discovery", "INVALID_OR_FOREIGN_FINAL_URL", url)
                return None

            title = await page.title()

            raw = await page.locator(
                "button,input,textarea,select,a,"
                "[role='button'],[role='checkbox'],[role='radio'],"
                "[role='tab'],[role='slider'],[role='combobox'],"
                "[contenteditable='true']"
            ).evaluate_all(
                """
                els => els.map((e, i) => {
                    const r = e.getBoundingClientRect();
                    const s = getComputedStyle(e);
                    return {
                      i,
                      tag: (e.tagName || '').toLowerCase(),
                      role: e.getAttribute('role') || '',
                      text: (e.innerText || e.textContent || '').trim().slice(0,160),
                      aria: e.getAttribute('aria-label') || '',
                      name: e.getAttribute('name') || '',
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

            for x in raw or []:
                tag = str(x.get("tag") or "").lower()
                role = str(x.get("role") or "").lower()
                text = str(x.get("text") or "").strip()
                aria = str(x.get("aria") or "").strip()
                name = str(x.get("name") or "").strip()
                placeholder = str(x.get("placeholder") or "").strip()
                input_type = str(x.get("type") or "").lower()
                ident = str(x.get("id") or "").strip()

                label = aria or placeholder or name or text

                if role in {"checkbox"} or input_type == "checkbox":
                    semantic = "CHECKBOX"
                    action = "check"
                elif role in {"radio"} or input_type == "radio":
                    semantic = "RADIO"
                    action = "check"
                elif role == "slider" or input_type == "range":
                    semantic = "SLIDER"
                    action = "adjust"
                elif role in {"combobox"} or tag == "select":
                    semantic = "SELECT"
                    action = "select"
                elif tag == "textarea":
                    semantic = "TEXTAREA"
                    action = "fill"
                elif tag == "input" and input_type == "file":
                    semantic = "FILE_UPLOAD"
                    action = "upload"
                elif tag == "input" or role == "textbox":
                    semantic = "TEXT_INPUT"
                    action = "fill"
                elif role == "tab":
                    semantic = "TAB"
                    action = "click"
                elif tag == "button" or role == "button":
                    semantic = "BUTTON"
                    action = "click"
                elif tag == "a":
                    semantic = "NAVIGATION"
                    action = "navigate"
                else:
                    semantic = "INTERACTIVE"
                    action = "click"

                if ident:
                    selector = f"#{ident}"
                elif name:
                    selector = f'[name="{name.replace(chr(34), chr(92) + chr(34))}"]'
                elif role:
                    selector = f'[role="{role}"]'
                else:
                    selector = tag or "*"

                href = canonical_url(x.get("href"), final)

                # Stable semantic identity, not DOM-count identity.
                behavior_key = "|".join(
                    [
                        surface_url(final),
                        semantic,
                        action,
                        label.lower()[:100],
                        name.lower(),
                        placeholder.lower(),
                        input_type,
                        href or "",
                    ]
                )

                elements.append(
                    Element(
                        element_id=sha(
                            f"{self.run_id}|{behavior_key}"
                        ),
                        tag=tag,
                        role=role,
                        label=label[:200],
                        text=text,
                        name=name,
                        placeholder=placeholder,
                        input_type=input_type,
                        selector=selector,
                        href=href,
                        visible=bool(x.get("visible")),
                        disabled=bool(x.get("disabled")),
                        semantic=semantic,
                        behavior_key=behavior_key,
                    )
                )

            # Deduplicate elements at semantic level.
            unique: dict[str, Element] = {}
            for e in elements:
                if not e.visible or e.disabled:
                    continue
                unique.setdefault(e.behavior_key, e)

            hrefs = await page.locator("a[href]").evaluate_all(
                "els => els.map(e => e.getAttribute('href'))"
            )

            links: set[str] = set()
            for href in hrefs or []:
                candidate = canonical_url(href, final)
                if candidate and same_host(candidate, self.target):
                    links.add(candidate)

            model = PageModel(
                url=final,
                surface=surface_url(final),
                title=title,
                elements=list(unique.values()),
                links=sorted(links),
                http_status=status,
                navigation_mode=mode,
            )

            self.pages.append(model)

            self.evidence_add(
                "DISCOVERY_PAGE_OBSERVED",
                final,
                details={
                    "status": status,
                    "mode": mode,
                    "elements": len(model.elements),
                    "links": len(model.links),
                    "surface": model.surface,
                },
            )

            self.log(
                f"✅ DISCOVERED | {final} | "
                f"surface={model.surface} | "
                f"semantic_elements={len(model.elements)}"
            )

            return model

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
        self.log("🗺️ V8.7.5 CURRENT-RUN DISCOVERY")
        self.log("Legacy artifacts: NOT READ")

        page = await browser.new_page()
        page.set_default_timeout(self.dom_timeout)

        start = canonical_url(self.target)
        assert start

        self.queue = [start]
        self.queued = {start}

        try:
            while self.queue and len(self.pages) < self.max_pages:
                raw = self.queue.pop(0)
                self.queued.discard(raw)

                url = canonical_url(raw, self.target)
                if not url:
                    continue

                if not same_host(url, self.target):
                    continue

                # Query variants of the same path are not new application
                # surfaces, but the first observed variant remains evidence.
                key = surface_url(url)
                if key in {
                    p.surface for p in self.pages
                }:
                    continue

                if key in self.seen:
                    continue

                self.seen.add(key)

                model = await self.collect_page(page, url)
                if not model:
                    continue

                for link in model.links:
                    link_surface = surface_url(link)
                    if (
                        link_surface not in self.seen
                        and link_surface not in self.queued
                    ):
                        self.queue.append(link)
                        self.queued.add(link_surface)

            if not self.pages:
                self.error(
                    "discovery",
                    "CURRENT_DISCOVERY_EMPTY",
                )
                return False

            if any(
                not p.current_run
                or not same_host(p.url, self.target)
                for p in self.pages
            ):
                self.error(
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

    def build_behaviors(self):
        self.log("=" * 70)
        self.log("🧠 V8.7.5 SEMANTIC BEHAVIOR NORMALIZATION")

        unique: dict[str, Behavior] = {}

        for p in self.pages:
            for e in p.elements:
                key = e.behavior_key

                risk = {
                    "FILE_UPLOAD": 70,
                    "TEXT_INPUT": 60,
                    "TEXTAREA": 60,
                    "SELECT": 60,
                    "SLIDER": 55,
                    "CHECKBOX": 55,
                    "RADIO": 55,
                    "TAB": 50,
                    "BUTTON": 50,
                    "NAVIGATION": 35,
                    "INTERACTIVE": 40,
                }.get(e.semantic, 40)

                blob = (
                    f"{e.label} {e.text} "
                    f"{p.url}"
                ).lower()

                if any(
                    word in blob
                    for word in (
                        "submit",
                        "save",
                        "delete",
                        "login",
                        "register",
                        "upload",
                    )
                ):
                    risk += 15

                unique.setdefault(
                    key,
                    Behavior(
                        behavior_id=sha(
                            f"{self.run_id}|{key}"
                        ),
                        page_surface=p.surface,
                        semantic=e.semantic,
                        action={
                            "TEXT_INPUT": "fill",
                            "TEXTAREA": "fill",
                            "SELECT": "select",
                            "SLIDER": "adjust",
                            "CHECKBOX": "check",
                            "RADIO": "check",
                            "FILE_UPLOAD": "upload",
                            "TAB": "click",
                            "BUTTON": "click",
                            "NAVIGATION": "navigate",
                            "INTERACTIVE": "click",
                        }.get(e.semantic, "click"),
                        selector=e.selector,
                        label=e.label,
                        behavior_key=key,
                        risk=min(risk, 100),
                    ),
                )

        self.behaviors = sorted(
            unique.values(),
            key=lambda b: (-b.risk, b.page_surface, b.behavior_id),
        )

        self.log(
            f"🧠 NORMALIZATION COMPLETE | "
            f"raw surfaces reduced to {len(self.behaviors)} semantic behaviors"
        )

    def build_journeys(self):
        self.log("=" * 70)
        self.log("🧭 V8.7.5 BUSINESS JOURNEY MODEL")

        by_surface: dict[str, list[Behavior]] = {}
        for b in self.behaviors:
            by_surface.setdefault(b.page_surface, []).append(b)

        journeys: list[Journey] = []

        for surface, behaviors in by_surface.items():
            def pick(kind: str):
                return next(
                    (
                        b for b in behaviors
                        if b.semantic == kind
                    ),
                    None,
                )

            selected: list[Behavior] = []

            if any(
                b.semantic in {"TEXT_INPUT", "TEXTAREA"}
                for b in behaviors
            ):
                for kind in (
                    "TEXT_INPUT",
                    "TEXTAREA",
                    "SELECT",
                    "RADIO",
                    "CHECKBOX",
                    "BUTTON",
                ):
                    b = pick(kind)
                    if b:
                        selected.append(b)
                        if len(selected) >= 3:
                            break
                goal = "Submit valid user information"

            elif any(
                b.semantic == "FILE_UPLOAD"
                for b in behaviors
            ):
                selected = [
                    b for b in behaviors
                    if b.semantic == "FILE_UPLOAD"
                ][:1]
                b = pick("BUTTON")
                if b:
                    selected.append(b)
                goal = "Transfer a file successfully"

            elif "alerts" in surface:
                selected = [
                    b for b in behaviors
                    if b.semantic == "BUTTON"
                ][:1]
                goal = "Handle browser dialog behavior"

            elif "browser-windows" in surface:
                selected = [
                    b for b in behaviors
                    if b.semantic == "BUTTON"
                ][:1]
                goal = "Open a new browser context"

            elif any(
                b.semantic in {
                    "CHECKBOX",
                    "RADIO",
                    "SELECT",
                    "SLIDER",
                    "TAB",
                }
                for b in behaviors
            ):
                selected = [
                    b for b in behaviors
                    if b.semantic in {
                        "CHECKBOX",
                        "RADIO",
                        "SELECT",
                        "SLIDER",
                        "TAB",
                    }
                ][:2]
                goal = "Exercise interactive control behavior"

            elif any(
                b.semantic == "NAVIGATION"
                for b in behaviors
            ):
                selected = [
                    b for b in behaviors
                    if b.semantic == "NAVIGATION"
                ][:2]
                goal = "Navigate application surface"

            elif behaviors:
                selected = behaviors[:2]
                goal = "Create or manage application data"

            if not selected:
                continue

            steps = [
                {
                    "step": i + 1,
                    "behavior_id": b.behavior_id,
                    "semantic": b.semantic,
                    "action": b.action,
                    "selector": b.selector,
                    "label": b.label,
                }
                for i, b in enumerate(selected)
            ]

            score = round(
                min(100, max(b.risk for b in selected) + len(selected) * 3),
                2,
            )

            jid = sha(
                f"{self.run_id}|{surface}|{goal}|"
                f"{','.join(b.behavior_id for b in selected)}"
            )

            journeys.append(
                Journey(
                    journey_id=jid,
                    goal=goal,
                    url=surface,
                    score=score,
                    steps=steps,
                    source_behavior_ids=[
                        b.behavior_id for b in selected
                    ],
                )
            )

        self.journeys = sorted(
            journeys,
            key=lambda j: (-j.score, j.url, j.journey_id),
        )

        self.log(
            f"🧭 JOURNEY MODEL COMPLETE | journeys={len(self.journeys)}"
        )

    async def resolve_locator(self, page, behavior: Behavior):
        locator = page.locator(behavior.selector).first

        if await locator.count():
            return locator

        # Fallback by semantic attributes/text when generated selector
        # is not unique after a dynamic page transition.
        if behavior.label:
            text_locator = page.get_by_text(
                behavior.label,
                exact=True,
            ).first
            if await text_locator.count():
                return text_locator

        return None

    async def specialized_execute(
        self,
        page,
        behavior: Behavior,
        journey_id: str | None = None,
    ) -> bool:
        locator = await self.resolve_locator(page, behavior)
        if locator is None:
            raise RuntimeError("CONTROL_NOT_FOUND")

        if behavior.semantic == "TEXT_INPUT":
            await locator.fill("QA_AUTONOMOUS_TEST")

        elif behavior.semantic == "TEXTAREA":
            await locator.fill("QA_AUTONOMOUS_TEST")

        elif behavior.semantic == "SELECT":
            tag = await locator.evaluate(
                "e => (e.tagName || '').toLowerCase()"
            )

            if tag == "select":
                options = await locator.locator(
                    "option"
                ).evaluate_all(
                    "els => els.map(e => e.value).filter(Boolean)"
                )
                if options:
                    await locator.select_option(options[0])
                else:
                    raise RuntimeError("NO_SELECT_OPTION")
            else:
                await locator.click()

        elif behavior.semantic == "CHECKBOX":
            await locator.check()

        elif behavior.semantic == "RADIO":
            await locator.check()

        elif behavior.semantic == "SLIDER":
            await locator.focus()
            await locator.press("Home")
            await locator.press("ArrowRight")

        elif behavior.semantic == "TAB":
            await locator.click()
            await asyncio.sleep(0.15)

            selected = await locator.get_attribute(
                "aria-selected"
            )

            if selected not in {None, "true"}:
                raise RuntimeError(
                    "TAB_SELECTED_STATE_NOT_OBSERVED"
                )

        elif behavior.semantic == "FILE_UPLOAD":
            # Deliberately do not fabricate an upload. A real file is created
            # for this current run and evidence is tied to this run.
            temp = (
                self.report_dir
                / f"qa_upload_{self.run_id}.txt"
            )
            temp.write_text(
                "V8.7.5 current-run upload evidence\n",
                encoding="utf-8",
            )
            await locator.set_input_files(str(temp))

        elif behavior.semantic == "NAVIGATION":
            href = await locator.get_attribute("href")
            target = canonical_url(href, page.url)

            if not target:
                raise RuntimeError("INVALID_NAVIGATION_TARGET")

            if not same_host(target, self.target):
                raise RuntimeError("FOREIGN_NAVIGATION_TARGET")

            await self.navigate(page, target)

        elif behavior.semantic == "BUTTON":
            # Alerts are handled through a dialog listener in the journey
            # executor; regular buttons are clicked without waiting for a
            # potentially long navigation.
            await locator.click(
                timeout=self.action_timeout,
                no_wait_after=True,
            )

        else:
            await locator.click(
                timeout=self.action_timeout,
                no_wait_after=True,
            )

        eid = self.evidence_add(
            "SEMANTIC_BEHAVIOR_OBSERVED",
            url=page.url,
            behavior_id=behavior.behavior_id,
            journey_id=journey_id,
            details={
                "semantic": behavior.semantic,
                "action": behavior.action,
                "selector": behavior.selector,
                "label": behavior.label,
            },
        )

        behavior.status = "COVERED"
        behavior.evidence_ids.append(eid)

        return True

    async def execute_journey(
        self,
        context,
        journey: Journey,
        retry: bool = False,
    ) -> dict[str, Any]:
        page = await context.new_page()
        page.set_default_timeout(self.action_timeout)

        dialog_events: list[dict[str, Any]] = []

        async def on_dialog(dialog):
            dialog_events.append(
                {
                    "type": dialog.type,
                    "message": dialog.message[:300],
                }
            )
            try:
                await dialog.accept()
            except Exception:
                try:
                    await dialog.dismiss()
                except Exception:
                    pass

        page.on("dialog", on_dialog)

        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        error: str | None = None

        async def work():
            await self.navigate(page, journey.url)

            self.evidence_add(
                "UI_ENTRY_OBSERVED",
                url=page.url,
                journey_id=journey.journey_id,
                details={"surface": surface_url(page.url)},
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

                # New-tab/new-window controls.
                if (
                    behavior.semantic == "BUTTON"
                    and "new window" in behavior.label.lower()
                ):
                    async with context.expect_page(
                        timeout=self.action_timeout
                    ) as page_info:
                        locator = await self.resolve_locator(
                            page, behavior
                        )
                        if locator is None:
                            raise RuntimeError("CONTROL_NOT_FOUND")
                        await locator.click(
                            timeout=self.action_timeout
                        )

                    new_page = await page_info.value
                    try:
                        await new_page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=self.dom_timeout,
                        )
                    except Exception:
                        pass

                    self.evidence_add(
                        "NEW_PAGE_OBSERVED",
                        url=new_page.url,
                        behavior_id=behavior.behavior_id,
                        journey_id=journey.journey_id,
                        details={"title": await new_page.title()},
                    )
                    await new_page.close()
                    behavior.status = "COVERED"
                    steps.append(
                        {
                            "step": step["step"],
                            "behavior_id": behavior.behavior_id,
                            "status": "PASS",
                            "specialized": "new-page",
                        }
                    )
                    continue

                passed = await self.specialized_execute(
                    page,
                    behavior,
                    journey.journey_id,
                )

                steps.append(
                    {
                        "step": step["step"],
                        "behavior_id": behavior.behavior_id,
                        "semantic": behavior.semantic,
                        "status": "PASS" if passed else "FAIL",
                    }
                )

                if not passed:
                    raise RuntimeError(
                        f"BEHAVIOR_FAILED:{behavior.behavior_id}"
                    )

        try:
            await asyncio.wait_for(
                work(),
                timeout=self.journey_timeout / 1000,
            )
            status = "PASS"

        except Exception as exc:
            status = "FAIL"
            error = f"{type(exc).__name__}: {exc}"

        finally:
            if dialog_events:
                self.evidence_add(
                    "DIALOG_OBSERVED",
                    url=page.url,
                    journey_id=journey.journey_id,
                    details={"dialogs": dialog_events},
                )
            await page.close()

        return {
            "run_id": self.run_id,
            "version": VERSION,
            "current_run": True,
            "journey_id": journey.journey_id,
            "goal": journey.goal,
            "url": journey.url,
            "status": status,
            "error": error,
            "retry": retry,
            "steps": steps,
            "duration_ms": round(
                (time.monotonic() - started) * 1000,
                2,
            ),

            # No backend/business assertion is made.
            "backend_verified": False,
            "business_verified": False,
            "backend_outcome": "UNKNOWN",
            "business_outcome": "UNKNOWN",

            "timestamp": now(),
        }

    async def execute_journeys(self, browser):
        self.log("=" * 70)
        self.log("🚀 V8.7.5 JOURNEY EXECUTION")

        context = await browser.new_context()

        try:
            for journey in self.journeys:
                result = await self.execute_journey(
                    context,
                    journey,
                )

                journey.status = result["status"]
                self.journey_results.append(result)

                self.log(
                    f"   {'🟢' if result['status'] == 'PASS' else '🔴'} "
                    f"{journey.goal} | {journey.url}"
                    + (
                        f" | {result['error']}"
                        if result["error"]
                        else ""
                    )
                )

            failures = [
                r for r in self.journey_results
                if r["status"] == "FAIL"
            ]

            if failures:
                self.log("=" * 70)
                self.log(
                    f"🔬 TARGETED FAILURE REPRODUCTION | "
                    f"{len(failures)} failures"
                )

                lookup = {
                    j.journey_id: j
                    for j in self.journeys
                }

                for failed in failures:
                    journey = lookup.get(
                        failed["journey_id"]
                    )
                    if not journey:
                        continue

                    retry = await self.execute_journey(
                        context,
                        journey,
                        retry=True,
                    )
                    self.journey_results.append(retry)

                    if retry["status"] == "PASS":
                        self.log(
                            f"   🟡 NOT REPRODUCED | "
                            f"{journey.url}"
                        )
                    else:
                        self.log(
                            f"   🔴 REPRODUCED | "
                            f"{journey.url}"
                        )

        finally:
            await context.close()

    async def close_unknowns(self, browser):
        self.log("=" * 70)
        self.log("🎯 V8.7.5 TARGETED UNKNOWN-BEHAVIOR CLOSURE")

        unknown = [
            b for b in self.behaviors
            if b.status == "UNKNOWN"
        ]

        unknown.sort(
            key=lambda b: (-b.risk, b.page_surface, b.behavior_id)
        )

        selected = unknown[:self.unknown_budget]

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
            for i, behavior in enumerate(selected, 1):
                self.log(
                    f"🎯 UNKNOWN [{i}/{len(selected)}] "
                    f"risk={behavior.risk:.1f} | "
                    f"{behavior.semantic} | "
                    f"{behavior.page_surface} | "
                    f"{behavior.label[:70]}"
                )

                page = await context.new_page()
                page.set_default_timeout(self.action_timeout)

                try:
                    await self.navigate(
                        page,
                        behavior.page_surface,
                    )

                    await self.specialized_execute(
                        page,
                        behavior,
                    )

                    self.log("   🟢 UNKNOWN CLOSED")

                except Exception as exc:
                    self.log(
                        f"   🔴 UNKNOWN REMAINS | "
                        f"{type(exc).__name__}: {exc}"
                    )
                    self.error(
                        "unknown_closure",
                        f"{type(exc).__name__}: {exc}",
                        behavior.page_surface,
                    )

                finally:
                    await page.close()

        finally:
            await context.close()

    def truth(self) -> dict[str, Any]:
        total = len(self.behaviors)
        covered = sum(
            b.status == "COVERED"
            for b in self.behaviors
        )
        unknown = total - covered

        primary = [
            r for r in self.journey_results
            if not r.get("retry")
        ]

        jp = sum(
            r["status"] == "PASS"
            for r in primary
        )
        jf = sum(
            r["status"] == "FAIL"
            for r in primary
        )

        backend_verified = sum(
            r.get("backend_verified") is True
            for r in primary
        )
        business_verified = sum(
            r.get("business_verified") is True
            for r in primary
        )

        # Hard integrity checks.
        fabricated_backend = False
        for r in primary:
            if r.get("backend_verified") is True:
                direct = any(
                    e["type"] == "BACKEND_OBSERVED"
                    and e["journey_id"] == r["journey_id"]
                    and e["run_id"] == self.run_id
                    for e in self.evidence
                )
                if not direct:
                    fabricated_backend = True

        self.fabricated_backend_claims = fabricated_backend

        self.foreign_evidence = any(
            e.get("run_id") != self.run_id
            or e.get("current_run") is not True
            for e in self.evidence
        )

        self.synthetic_evidence = any(
            e.get("type") in {
                "SYNTHETIC",
                "ASSUMED_PASS",
                "INFERRED_PASS",
            }
            for e in self.evidence
        )

        coverage = (
            round(covered / total * 100, 2)
            if total else None
        )

        gates = {
            "current_discovery": len(self.pages) > 0,
            "canonical_model": total > 0,
            "legacy_state_reused": not self.legacy_state_reused,
            "synthetic_evidence": not self.synthetic_evidence,
            "foreign_evidence": not self.foreign_evidence,
            "execution_failures": jf == 0,
            "journey_failures": jf == 0,
            "unknown_behaviors": unknown == 0,
            "evidence_exists": bool(self.evidence),
            "fabricated_backend_claims": (
                not self.fabricated_backend_claims
                and backend_verified == 0
            ),
        }

        release = all(gates.values())

        return {
            "version": VERSION,
            "run_id": self.run_id,
            "target": self.target,
            "generated_at": now(),

            "discovery_pages": len(self.pages),
            "behavioral_surfaces": total,
            "covered_behaviors": covered,
            "unknown_behaviors": unknown,
            "behavioral_coverage_percent": coverage,

            "business_journeys": len(self.journeys),
            "journey_pass": jp,
            "journey_fail": jf,
            "journey_execution_coverage_percent": (
                round(len(primary) / len(self.journeys) * 100, 2)
                if self.journeys else None
            ),

            "executions": len(primary),
            "execution_pass": len(primary) - jf,
            "execution_fail": jf,

            "evidence_records": len(self.evidence),

            "observed_outcomes": 0,
            "inferred_outcomes": 0,
            "unknown_outcomes": len(primary),

            "backend_verified": backend_verified,
            "business_verified": business_verified,

            "legacy_state_reused": self.legacy_state_reused,
            "legacy_artifacts_read": self.legacy_artifacts_read,
            "synthetic_evidence": self.synthetic_evidence,
            "foreign_evidence": self.foreign_evidence,
            "fabricated_backend_claims": self.fabricated_backend_claims,

            "truth_gates": gates,
            "release_truth": "PASS" if release else "FAIL",
            "release_blocked": not release,
            "fail_closed": True,

            "secret_safety": {
                "credentials": False,
                "tokens": False,
                "cookies": False,
            },

            "errors": self.errors,
        }

    def save(self, truth: dict[str, Any]):
        def dump(name: str, obj: Any):
            (self.report_dir / name).write_text(
                json.dumps(
                    obj,
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                ),
                encoding="utf-8",
            )

        dump(
            "qa_current_application_model_v8_7_5.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "pages": [asdict(p) for p in self.pages],
            },
        )

        dump(
            "qa_current_behavior_model_v8_7_5.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "behaviors": [asdict(b) for b in self.behaviors],
            },
        )

        dump(
            "qa_current_journey_model_v8_7_5.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "journeys": [asdict(j) for j in self.journeys],
            },
        )

        dump(
            "qa_current_execution_evidence_v8_7_5.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run": True,
                "journey_results": self.journey_results,
                "evidence": self.evidence,
            },
        )

        dump(
            "qa_v8_7_5_canonical_truth.json",
            truth,
        )

        dump(
            "qa_v8_7_5_execution_integrity.json",
            {
                "version": VERSION,
                "run_id": self.run_id,
                "current_run_only": True,
                "legacy_artifacts_read": [],
                "legacy_state_reused": False,
                "synthetic_evidence": self.synthetic_evidence,
                "foreign_evidence": self.foreign_evidence,
                "fabricated_backend_claims": self.fabricated_backend_claims,
            },
        )

    def report(self, t: dict[str, Any]):
        print()
        print("=" * 70)
        print("🧭 V8.7.5 SINGLE CANONICAL TRUTH")
        print("=" * 70)

        rows = [
            ("Version", VERSION),
            ("Run ID", self.run_id),
            ("Target", self.target),
            ("Discovery pages", t["discovery_pages"]),
            ("Behavioral surfaces", t["behavioral_surfaces"]),
            ("Covered behaviors", t["covered_behaviors"]),
            ("Unknown behaviors", t["unknown_behaviors"]),
            ("Behavioral coverage", f"{t['behavioral_coverage_percent']}%"),
            ("Business journeys", t["business_journeys"]),
            ("Journey PASS", t["journey_pass"]),
            ("Journey FAIL", t["journey_fail"]),
            ("Journey execution cov.", f"{t['journey_execution_coverage_percent']}%"),
            ("Executions", t["executions"]),
            ("Execution PASS", t["execution_pass"]),
            ("Execution FAIL", t["execution_fail"]),
            ("Evidence records", t["evidence_records"]),
            ("Observed outcomes", t["observed_outcomes"]),
            ("Inferred outcomes", t["inferred_outcomes"]),
            ("Unknown outcomes", t["unknown_outcomes"]),
            ("Backend verified", t["backend_verified"]),
            ("Business verified", t["business_verified"]),
        ]

        for k, v in rows:
            print(f"{k:28s}: {v}")

        print()
        print("-" * 70)
        print("🛡️ V8.7.5 TRUTH GATES")
        print("-" * 70)

        for k, v in t["truth_gates"].items():
            print(
                f"{k:30s}: "
                f"{'PASS' if v else 'FAIL'}"
            )

        print("-" * 70)

        if t["release_truth"] == "PASS":
            print("🟢 V8.7.5 RELEASE TRUTH GATE: PASS")
        else:
            print("🔴 V8.7.5 RELEASE TRUTH GATE: FAIL")
            print(
                "   FAIL-CLOSED: release claims are NOT authorized."
            )

        print(
            f"📄 Canonical truth: "
            f"{(self.report_dir / 'qa_v8_7_5_canonical_truth.json').resolve()}"
        )
        print(
            f"📄 Report directory: {self.report_dir.resolve()}"
        )

    async def run(self) -> int:
        self.log("=" * 70)
        self.log("🚀 V8.7.5 SEMANTIC QA TRUTH ENGINE")
        self.log("=" * 70)
        self.log(f"Version : {VERSION}")
        self.log(f"Run ID  : {self.run_id}")
        self.log(f"Target  : {self.target}")
        self.log("Legacy V8.x state: BLOCKED FROM READ")

        async with async_playwright() as p:
            self.log("🌐 STARTING CHROMIUM")

            try:
                browser = await p.chromium.launch(
                    headless=self.headless
                )
            except Exception as exc:
                self.error(
                    "browser",
                    f"{type(exc).__name__}: {exc}",
                )
                t = self.truth()
                self.save(t)
                self.report(t)
                return 3

            self.log("🟢 CHROMIUM READY")

            try:
                discovery = await self.discover(browser)

                if not discovery:
                    self.log(
                        "🔴 STOP: current discovery did not "
                        "establish a valid application model."
                    )
                    t = self.truth()
                    self.save(t)
                    self.report(t)
                    return 4

                self.build_behaviors()

                if not self.behaviors:
                    self.error(
                        "behavior_model",
                        "CURRENT_BEHAVIOR_MODEL_EMPTY",
                    )
                    t = self.truth()
                    self.save(t)
                    self.report(t)
                    return 5

                self.build_journeys()

                await self.execute_journeys(browser)
                await self.close_unknowns(browser)

                t = self.truth()
                self.save(t)
                self.report(t)

                return 0 if t["release_truth"] == "PASS" else 7

            finally:
                await browser.close()


def parse_args():
    p = argparse.ArgumentParser(
        description="V8.7.5 semantic, fail-closed QA agent"
    )

    p.add_argument(
        "url",
        nargs="?",
        default="https://demoqa.com",
    )

    g = p.add_mutually_exclusive_group()
    g.add_argument("--headless", action="store_true")
    g.add_argument("--headed", action="store_true")

    p.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
    )

    p.add_argument(
        "--nav-timeout",
        type=int,
        default=DEFAULT_NAV_TIMEOUT,
    )

    p.add_argument(
        "--dom-timeout",
        type=int,
        default=DEFAULT_DOM_TIMEOUT,
    )

    p.add_argument(
        "--action-timeout",
        type=int,
        default=DEFAULT_ACTION_TIMEOUT,
    )

    # Compatibility with previous commands.
    p.add_argument(
        "--timeout",
        type=int,
        default=None,
    )

    p.add_argument(
        "--journey-timeout",
        type=int,
        default=DEFAULT_JOURNEY_TIMEOUT,
    )

    p.add_argument(
        "--unknown-budget",
        type=int,
        default=DEFAULT_UNKNOWN_BUDGET,
    )

    args = p.parse_args()

    if args.timeout is not None:
        args.nav_timeout = args.timeout
        args.dom_timeout = min(args.timeout, DEFAULT_DOM_TIMEOUT)

    return args


def main():
    args = parse_args()
    agent = QAAgent(args)
    raise SystemExit(asyncio.run(agent.run()))


if __name__ == "__main__":
    main()
