#!/usr/bin/env python3
"""
V8.7.8 - DISCOVERY ROBUSTNESS + SEMANTIC MODEL FIX

Key fixes over V8.7.5:
- Navigation anchors are valid semantic surfaces even when visually hidden.
- Discovery never treats "0 interactive controls" as "0 application content"
  when valid same-host navigation links are present.
- React/SPA content gets a short stability wait before collection.
- Link extraction is independent from interactive-element visibility.
- /None, javascript:, fragments and foreign hosts are rejected.
- Query variants are collapsed by application surface.
- Current-run-only evidence.
- No legacy V8.x artifacts are read.
- Release remains fail-closed.
"""

from __future__ import annotations
import argparse, asyncio, hashlib, json, re, time, uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse, parse_qsl, urlencode

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

VERSION = "8.7.8"
REPORT_DIR = Path("qa_v8_7_8_report")

def now():
    return datetime.now(timezone.utc).isoformat()

def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:20]

def canon(raw, base=None, drop_query=False):
    if raw is None:
        return None
    v = str(raw).strip()
    if not v or v.lower() in {"none","null","undefined","javascript:void(0)","#"}:
        return None
    if base:
        v = urljoin(base, v)
    v, _ = urldefrag(v)
    try:
        p = urlparse(v)
        scheme = p.scheme.lower()
        host = (p.hostname or "").lower()
        if scheme not in {"http","https"} or not host:
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
        if p.port and not ((scheme=="http" and p.port==80) or (scheme=="https" and p.port==443)):
            netloc = f"{host}:{p.port}"
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return None

def surface(u):
    return canon(u, drop_query=True) or u

def same_host(a,b):
    return (urlparse(a).hostname or "").lower() == (urlparse(b).hostname or "").lower()

@dataclass
class Element:
    id: str
    tag: str
    role: str
    label: str
    text: str
    selector: str
    href: str|None
    visible: bool
    disabled: bool
    semantic: str
    key: str

@dataclass
class PageModel:
    url: str
    surface: str
    title: str
    elements: list[Element]
    links: list[str]
    status: int|None

class Agent:
    def __init__(self,a):
        self.target = canon(a.url)
        if not self.target:
            raise ValueError("Invalid target URL")
        self.headless = not a.headed
        self.max_pages = max(1,a.max_pages)
        self.nav_timeout = max(1000,a.nav_timeout)
        self.dom_timeout = max(1000,a.dom_timeout)
        self.action_timeout = max(1000,a.action_timeout)
        self.unknown_budget = max(1,a.unknown_budget)
        self.run_id = uuid.uuid4().hex
        self.t0 = time.monotonic()
        self.pages=[]
        self.behaviors=[]
        self.evidence=[]
        self.errors=[]
        self.queue=[self.target]
        self.queued={surface(self.target)}
        self.seen=set()

    def log(self,s):
        print(f"[{time.monotonic()-self.t0:7.1f}s] {s}",flush=True)

    def ev(self,kind,url=None,details=None):
        self.evidence.append({
            "evidence_id":sha(f"{self.run_id}|{len(self.evidence)}|{kind}|{url}"),
            "run_id":self.run_id,"version":VERSION,"current_run":True,
            "type":kind,"url":url,"observed":True,"timestamp":now(),
            "details":details or {}
        })

    def err(self,phase,msg,url=None):
        self.errors.append({
            "run_id":self.run_id,"version":VERSION,"current_run":True,
            "timestamp":now(),"phase":phase,"message":msg,"url":url
        })

    async def navigate(self,page,url):
        """Navigate without making full load completion a prerequisite.

        DemoQA can keep network activity open long enough to trip a normal
        goto timeout even though the application document is usable. We use
        commit/DOM readiness and then explicitly inspect the rendered content.
        """
        response = None
        try:
            response = await page.goto(
                url,
                wait_until="commit",
                timeout=self.nav_timeout
            )
        except PlaywrightTimeoutError:
            self.log(f"⚠️ COMMIT TIMEOUT | {url}")
            # A timeout is only fatal if Playwright has no usable document.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self.dom_timeout)
        except Exception:
            pass

        await page.locator("html").wait_for(state="attached", timeout=self.dom_timeout)
        await page.locator("body").wait_for(state="attached", timeout=self.dom_timeout)

        # Give React/SPA hydration a bounded opportunity to render.
        for _ in range(6):
            count = await page.locator("a[href]").count()
            body_text = ""
            try:
                body_text = (await page.locator("body").inner_text(timeout=1000)).strip()
            except Exception:
                pass
            if count > 0 or len(body_text) >= 20:
                break
            await page.wait_for_timeout(500)

        return response

    async def collect(self,page,url):
        self.log(f"🔎 DISCOVER [{len(self.pages)+1}/{self.max_pages}] {url}")
        try:
            response=await self.navigate(page,url)
            final=canon(page.url,url)
            if not final or not same_host(final,self.target):
                self.err("discovery","FOREIGN_OR_INVALID_FINAL_URL",url)
                return None

            title=await page.title()

            # IMPORTANT: links are collected independently of visibility.
            raw_links=await page.locator("a[href]").evaluate_all(
                """els => els.map(e => ({
                    href:e.getAttribute('href'),
                    text:(e.innerText||e.textContent||'').trim().slice(0,160)
                }))"""
            )

            links=set()
            for x in raw_links or []:
                u=canon(x.get("href"),final)
                if u and same_host(u,self.target):
                    links.add(u)

            # Interactive semantics: visibility is used for executable controls,
            # but navigation anchors remain modelable even when not visible.
            raw=await page.locator(
                "button,input,textarea,select,a,"
                "[role='button'],[role='checkbox'],[role='radio'],"
                "[role='tab'],[role='slider'],[role='combobox'],"
                "[contenteditable='true']"
            ).evaluate_all(
                """els => els.map((e,i)=>{
                    const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                    return {
                      i, tag:(e.tagName||'').toLowerCase(),
                      role:e.getAttribute('role')||'',
                      text:(e.innerText||e.textContent||'').trim().slice(0,160),
                      aria:e.getAttribute('aria-label')||'',
                      name:e.getAttribute('name')||'',
                      placeholder:e.getAttribute('placeholder')||'',
                      type:e.getAttribute('type')||'',
                      id:e.id||'', href:e.getAttribute('href'),
                      disabled:!!e.disabled,
                      visible:!!(r.width&&r.height)&&
                        s.visibility!=='hidden'&&s.display!=='none'
                    }
                })"""
            )

            elems=[]
            for x in raw or []:
                tag=str(x.get("tag") or "").lower()
                role=str(x.get("role") or "").lower()
                text=str(x.get("text") or "").strip()
                aria=str(x.get("aria") or "").strip()
                name=str(x.get("name") or "").strip()
                placeholder=str(x.get("placeholder") or "").strip()
                typ=str(x.get("type") or "").lower()
                ident=str(x.get("id") or "").strip()
                href=canon(x.get("href"),final)
                label=(aria or placeholder or name or text or href or tag)[:200]

                if tag=="a":
                    semantic="NAVIGATION"; action="navigate"
                elif role=="checkbox" or typ=="checkbox":
                    semantic="CHECKBOX"; action="check"
                elif role=="radio" or typ=="radio":
                    semantic="RADIO"; action="check"
                elif role=="slider" or typ=="range":
                    semantic="SLIDER"; action="adjust"
                elif role=="combobox" or tag=="select":
                    semantic="SELECT"; action="select"
                elif tag=="textarea":
                    semantic="TEXTAREA"; action="fill"
                elif tag=="input" and typ=="file":
                    semantic="FILE_UPLOAD"; action="upload"
                elif tag=="input" or role=="textbox":
                    semantic="TEXT_INPUT"; action="fill"
                elif role=="tab":
                    semantic="TAB"; action="click"
                elif tag=="button" or role=="button":
                    semantic="BUTTON"; action="click"
                else:
                    semantic="INTERACTIVE"; action="click"

                # Navigation links are retained even if CSS says hidden.
                if semantic!="NAVIGATION" and (not x.get("visible") or x.get("disabled")):
                    continue

                if ident:
                    selector=f"#{ident}"
                elif name:
                    selector=f'[name="{name.replace(chr(34), chr(92)+chr(34))}"]'
                elif role:
                    selector=f'[role="{role}"]'
                elif tag=="a" and href:
                    selector=f'a[href="{x.get("href","").replace(chr(34), chr(92)+chr(34))}"]'
                else:
                    selector=tag or "*"

                key="|".join([
                    surface(final),semantic,action,label.lower(),
                    name.lower(),placeholder.lower(),typ,href or ""
                ])
                elems.append(Element(
                    id=sha(f"{self.run_id}|{key}"),tag=tag,role=role,label=label,
                    text=text,selector=selector,href=href,
                    visible=bool(x.get("visible")),disabled=bool(x.get("disabled")),
                    semantic=semantic,key=key
                ))

            uniq={}
            for e in elems:
                uniq.setdefault(e.key,e)

            status=response.status if response else None
            model=PageModel(
                url=final,surface=surface(final),title=title,
                elements=list(uniq.values()),links=sorted(links),
                status=status
            )
            self.pages.append(model)
            self.ev("DISCOVERY_PAGE",final,{
                "status":status,"elements":len(model.elements),
                "links":len(model.links),"visible_executable":
                    sum(1 for e in model.elements if e.semantic!="NAVIGATION")
            })

            self.log(
                f"✅ DISCOVERED | {final} | "
                f"links={len(model.links)} | semantic_elements={len(model.elements)}"
            )
            return model
        except Exception as e:
            self.err("discovery",f"{type(e).__name__}: {e}",url)
            self.log(f"❌ DISCOVERY ERROR | {url} | {type(e).__name__}: {e}")
            return None

    async def discover(self,browser):
        self.log("="*70)
        self.log("🗺️ V8.7.8 CURRENT-RUN DISCOVERY")
        self.log("Legacy artifacts: NOT READ")
        page=await browser.new_page()
        page.set_default_timeout(self.dom_timeout)
        try:
            while self.queue and len(self.pages)<self.max_pages:
                raw=self.queue.pop(0)
                key=surface(raw)
                self.queued.discard(key)
                if key in self.seen:
                    continue
                self.seen.add(key)

                model=await self.collect(page,raw)
                if not model:
                    continue

                for link in model.links:
                    k=surface(link)
                    if k not in self.seen and k not in self.queued:
                        self.queue.append(link)
                        self.queued.add(k)

            # A valid application model requires either executable semantics
            # OR same-host navigation surfaces. This fixes the V8.7.5 zero-model bug.
            if not self.pages:
                self.err("discovery","CURRENT_DISCOVERY_EMPTY")
                return False

            if not any(p.elements or p.links for p in self.pages):
                self.err("discovery","CURRENT_APPLICATION_MODEL_EMPTY")
                return False

            root = next((p for p in self.pages if surface(self.target) == p.surface), None)
            if root is None:
                self.err("discovery","TARGET_ROOT_NOT_DISCOVERED")
                return False

            if len(root.links) == 0 and len(root.elements) == 0:
                self.err("discovery","ROOT_SURFACE_HAS_NO_DISCOVERABLE_CONTENT")
                return False

            self.log(
                f"📚 DISCOVERY COMPLETE | pages={len(self.pages)} | "
                f"root_links={len(root.links)} | root_elements={len(root.elements)}"
            )
            return True
        finally:
            await page.close()

    def build_behaviors(self):
        self.log("="*70)
        self.log("🧠 V8.7.8 SEMANTIC BEHAVIOR MODEL")
        uniq={}
        for p in self.pages:
            for e in p.elements:
                risk={
                    "FILE_UPLOAD":70,"TEXT_INPUT":60,"TEXTAREA":60,
                    "SELECT":60,"SLIDER":55,"CHECKBOX":55,"RADIO":55,
                    "TAB":50,"BUTTON":50,"NAVIGATION":35
                }.get(e.semantic,40)
                blob=f"{e.label} {p.url}".lower()
                if any(w in blob for w in
                       ("submit","save","delete","login","register","upload")):
                    risk=min(100,risk+15)
                uniq.setdefault(e.key,{
                    "behavior_id":sha(f"{self.run_id}|{e.key}"),
                    "run_id":self.run_id,"version":VERSION,"current_run":True,
                    "page":p.url,"surface":p.surface,"semantic":e.semantic,
                    "action":{"TEXT_INPUT":"fill","TEXTAREA":"fill",
                              "SELECT":"select","SLIDER":"adjust",
                              "CHECKBOX":"check","RADIO":"check",
                              "FILE_UPLOAD":"upload","TAB":"click",
                              "BUTTON":"click","NAVIGATION":"navigate"}.get(e.semantic,"click"),
                    "selector":e.selector,"label":e.label,"risk":risk,
                    "status":"UNKNOWN","evidence_ids":[]
                })
        self.behaviors=list(uniq.values())

        # Canonical schema invariant: every semantic behavior must have a
        # non-empty semantic/action and must originate from the current run.
        malformed = [
            b for b in self.behaviors
            if not b.get("semantic")
            or not b.get("action")
            or b.get("run_id") != self.run_id
            or b.get("current_run") is not True
        ]
        if malformed:
            self.err(
                "behavior_model",
                f"CANONICAL_BEHAVIOR_SCHEMA_INVALID count={len(malformed)}"
            )
            raise RuntimeError(
                f"Canonical behavior schema invalid: {len(malformed)} behaviors"
            )

        self.log(f"🧠 BEHAVIOR MODEL COMPLETE | behaviors={len(self.behaviors)}")

    async def execute_safe_navigation(self,browser):
        """Execute a bounded, deduplicated set of current-run navigation behaviors.

        V8.7.7 had a type/schema bug here: PageModel.elements contains Element
        dataclass objects, but the executor treated them as dictionaries.
        V8.7.8 uses the Element attributes directly and deduplicates by
        canonical destination surface.
        """
        self.log("="*70)
        self.log("🧪 V8.7.8 SAFE NAVIGATION EXECUTION")

        candidates = []
        seen_destinations = set()

        for p in self.pages:
            for e in p.elements:
                if e.semantic != "NAVIGATION":
                    continue
                dest = e.href
                if not dest or not same_host(dest, self.target):
                    continue
                key = surface(dest)
                if key in seen_destinations:
                    continue
                seen_destinations.add(key)
                candidates.append((p.url, e, dest))

        # Bound execution so navigation discovery does not turn into an
        # uncontrolled 1000+ action run.
        limit = min(len(candidates), max(0, self.max_pages - 1))
        self.log(
            f"Navigation candidates={len(candidates)} | "
            f"deduplicated destinations={len(candidates)} | execute={limit}"
        )

        results = []
        for index, (source_url, element, dest) in enumerate(candidates[:limit], 1):
            pg = None
            try:
                self.log(
                    f"🧪 NAV [{index}/{limit}] "
                    f"{source_url} -> {dest}"
                )
                pg = await browser.new_page()
                pg.set_default_timeout(self.action_timeout)

                response = await pg.goto(
                    dest,
                    wait_until="commit",
                    timeout=self.nav_timeout
                )
                try:
                    await pg.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self.dom_timeout
                    )
                except Exception:
                    pass

                await pg.locator("html").wait_for(
                    state="attached",
                    timeout=self.dom_timeout
                )
                final = canon(pg.url, dest)
                ok = bool(final and same_host(final, self.target))

                eid = self.ev(
                    "NAVIGATION_EXECUTION",
                    final or dest,
                    {
                        "from": source_url,
                        "destination": dest,
                        "final_url": final,
                        "http_status": response.status if response else None,
                        "ok": ok,
                        "current_run": True
                    }
                )

                # Update the matching semantic behavior only.
                for b in self.behaviors:
                    if (
                        b["page"] == source_url
                        and b["semantic"] == "NAVIGATION"
                        and b["selector"] == element.selector
                        and b["label"] == element.label
                    ):
                        b["status"] = "PASS" if ok else "FAIL"
                        b["evidence_ids"].append(eid)
                        break

                results.append(ok)
                self.log(
                    f"   {'🟢 PASS' if ok else '🔴 FAIL'} | "
                    f"final={final}"
                )

            except Exception as exc:
                self.err(
                    "navigation_execution",
                    f"{type(exc).__name__}: {exc}",
                    dest
                )
                self.log(
                    f"   🔴 FAIL | {type(exc).__name__}: {exc}"
                )
                results.append(False)
            finally:
                if pg is not None:
                    try:
                        await pg.close()
                    except Exception:
                        pass

        passed = sum(1 for x in results if x)
        failed = len(results) - passed
        self.log(
            f"🧪 NAVIGATION EXECUTION COMPLETE | "
            f"executed={len(results)} | pass={passed} | fail={failed}"
        )

    def truth(self,discovery_ok):
        covered=sum(1 for b in self.behaviors if b["status"]=="PASS")
        total=len(self.behaviors)
        unknown=sum(1 for b in self.behaviors if b["status"]=="UNKNOWN")
        execution_fail=sum(1 for b in self.behaviors if b["status"]=="FAIL")
        coverage=(covered/total*100) if total else None

        # Gate values represent the truth of the condition itself:
        # "bad condition is present" => FAIL. A false bad-condition is PASS.
        gates={
            "current_discovery": bool(discovery_ok),
            "canonical_model": bool(discovery_ok and total > 0),
            "legacy_state_reused": False,
            "synthetic_evidence": False,
            "foreign_evidence": False,
            "execution_failures": execution_fail == 0,
            "unknown_behaviors": unknown == 0,
            "evidence_exists": bool(self.evidence),
            "fabricated_backend_claims": False,
        }
        release=all(gates.values())

        truth={
            "version":VERSION,"run_id":self.run_id,"target":self.target,
            "discovery_pages":len(self.pages),
            "behavioral_surfaces":total,"covered_behaviors":covered,
            "unknown_behaviors":unknown,"behavioral_coverage":coverage,
            "executions":covered+execution_fail,
            "execution_pass":covered,"execution_fail":execution_fail,
            "evidence_records":len(self.evidence),
            "legacy_state_reused":False,
            "gates":gates,"release_truth_gate":release,
            "generated_at":now()
        }
        self.log("="*70)
        self.log("🧭 V8.7.8 SINGLE CANONICAL TRUTH")
        self.log("="*70)
        for k,v in truth.items():
            if k=="gates": continue
            self.log(f"{k.replace('_',' ').title():30}: {v}")
        self.log("-"*70)
        self.log("🛡️ V8.7.8 TRUTH GATES")
        self.log("-"*70)
        for k,v in gates.items():
            self.log(f"{k:30} : {'PASS' if v else 'FAIL'}")
        self.log("-"*70)
        self.log(
            "🟢 V8.7.8 RELEASE TRUTH GATE: PASS"
            if release else
            "🔴 V8.7.8 RELEASE TRUTH GATE: FAIL\n"
            "   FAIL-CLOSED: release claims are NOT authorized."
        )
        return truth

    def write(self,truth):
        REPORT_DIR.mkdir(exist_ok=True)
        (REPORT_DIR/"qa_v8_7_8_canonical_truth.json").write_text(
            json.dumps(truth,indent=2),encoding="utf-8")
        (REPORT_DIR/"application_map.json").write_text(
            json.dumps([asdict(p) for p in self.pages],indent=2),encoding="utf-8")
        (REPORT_DIR/"semantic_behaviors.json").write_text(
            json.dumps(self.behaviors,indent=2),encoding="utf-8")
        (REPORT_DIR/"evidence.json").write_text(
            json.dumps(self.evidence,indent=2),encoding="utf-8")
        (REPORT_DIR/"errors.json").write_text(
            json.dumps(self.errors,indent=2),encoding="utf-8")

    async def run(self):
        self.log("="*70)
        self.log("🚀 V8.7.8 DISCOVERY ROBUSTNESS + SEMANTIC QA")
        self.log("="*70)
        self.log(f"Version : {VERSION}")
        self.log(f"Run ID  : {self.run_id}")
        self.log(f"Target  : {self.target}")
        self.log("Legacy V8.x state: BLOCKED FROM READ")

        async with async_playwright() as p:
            self.log("🌐 STARTING CHROMIUM")
            browser=await p.chromium.launch(headless=self.headless)
            self.log("🟢 CHROMIUM READY")
            ok=await self.discover(browser)
            if ok:
                self.build_behaviors()
                # Do not turn navigation discovery into a huge slow test run.
                await self.execute_safe_navigation(browser)
            truth=self.truth(ok)
            self.write(truth)
            await browser.close()
            self.log(f"📄 Canonical truth: {REPORT_DIR/'qa_v8_7_8_canonical_truth.json'}")

def args():
    ap=argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--headless",action="store_true")
    ap.add_argument("--headed",action="store_true")
    ap.add_argument("--max-pages",type=int,default=50)
    ap.add_argument("--nav-timeout",type=int,default=12000)
    ap.add_argument("--dom-timeout",type=int,default=6000)
    ap.add_argument("--action-timeout",type=int,default=3500)
    ap.add_argument("--journey-timeout",type=int,default=12000)
    ap.add_argument("--unknown-budget",type=int,default=60)
    return ap.parse_args()

if __name__=="__main__":
    a=args()
    asyncio.run(Agent(a).run())
