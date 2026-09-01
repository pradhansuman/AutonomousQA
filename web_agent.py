import asyncio
import re
import sys
from urllib.parse import quote, urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


MAX_SEARCH_RESULTS = 8
MAX_PAGES = 8
MAX_TEXT = 12000


# ============================================================
# DISPLAY
# ============================================================

def banner(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ============================================================
# URL CLEANING
# ============================================================

def clean_url(url):

    if not url:
        return None

    # DuckDuckGo redirect:
    # https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com

    if "duckduckgo.com/l/" in url:

        parsed = urlparse(url)

        params = parse_qs(
            parsed.query
        )

        if "uddg" in params:

            return unquote(
                params["uddg"][0]
            )

    return url


def valid_url(url):

    try:

        parsed = urlparse(url)

        return (
            parsed.scheme in
            ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:

        return False


# ============================================================
# BLOCKED CONTENT DETECTION
# ============================================================

def is_blocked(url, text=""):

    content = (
        url + " " + text
    ).lower()

    patterns = [

        "captcha",
        "recaptcha",
        "unusual traffic",
        "verify you are human",
        "i'm not a robot",
        "im not a robot",
        "access denied",
        "robot check",
        "enable javascript",
    ]

    return any(
        pattern in content
        for pattern in patterns
    )


# ============================================================
# SEARCH
# ============================================================

async def search(page, query):

    banner(
        f"🔎 SEARCH: {query}"
    )

    url = (
        "https://html.duckduckgo.com/html/?q="
        + quote(query)
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.wait_for_timeout(
            1000
        )

        body = await page.locator(
            "body"
        ).inner_text()

        if is_blocked(
            url,
            body
        ):

            print(
                "❌ Search blocked"
            )

            return []

        raw_results = await page.locator(
            "a.result__a"
        ).evaluate_all(
            """
            links => links.map(a => ({
                title: a.innerText.trim(),
                url: a.href
            }))
            """
        )

        results = []
        seen = set()

        for item in raw_results:

            original_url = item["url"]

            url = clean_url(
                original_url
            )

            if not valid_url(url):
                continue

            if is_blocked(url):
                continue

            if url in seen:
                continue

            seen.add(url)

            results.append({
                "title":
                    item["title"],
                "url":
                    url
            })

            if len(results) >= MAX_SEARCH_RESULTS:
                break

        print(
            f"✓ Clean results: "
            f"{len(results)}"
        )

        return results

    except Exception as e:

        print(
            f"❌ Search failed: {e}"
        )

        return []


# ============================================================
# MAIN CONTENT EXTRACTION
# ============================================================

async def browse(page, result):

    url = result["url"]

    print("\n🌐 BROWSE")
    print(url)

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=25000,
        )

        await page.wait_for_timeout(
            1000
        )

        title = await page.title()

        body_text = await page.locator(
            "body"
        ).inner_text()

        # Never accept CAPTCHA pages.

        if is_blocked(
            url,
            body_text
        ):

            print(
                "❌ CAPTCHA / BLOCKED"
            )

            return None

        html = await page.content()

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # Remove obvious non-content.

        for tag in soup([
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
            "iframe",
        ]):

            tag.decompose()

        # Prefer article/main content.

        main = (
            soup.find("article")
            or soup.find("main")
            or soup.find(
                attrs={
                    "role": "main"
                }
            )
        )

        if main:

            content = main

        else:

            content = soup.body

        if not content:

            print(
                "❌ No main content"
            )

            return None

        text = content.get_text(
            " ",
            strip=True
        )

        # Normalize whitespace.

        text = re.sub(
            r"\s+",
            " ",
            text
        )

        if len(text) < 500:

            print(
                "❌ Too little content"
            )

            return None

        text = text[:MAX_TEXT]

        print(
            f"✓ MAIN CONTENT: "
            f"{len(text)} chars"
        )

        return {
            "title": title,
            "url": url,
            "text": text
        }

    except Exception as e:

        print(
            f"⚠ Browse failed: {e}"
        )

        return None


# ============================================================
# RELEVANCE
# ============================================================

def relevance(page_data, goal):

    text = page_data[
        "text"
    ].lower()

    keywords = [
        word.lower()
        for word in re.findall(
            r"[a-zA-Z0-9]+",
            goal
        )
        if len(word) >= 4
    ]

    if not keywords:

        return 0

    matches = sum(
        keyword in text
        for keyword in keywords
    )

    return matches / len(
        keywords
    )


# ============================================================
# EXTRACT EVIDENCE
# ============================================================

def extract_evidence(
    pages,
    goal
):

    banner(
        "🧩 EXTRACT INFORMATION"
    )

    evidence = []

    for page in pages:

        score = relevance(
            page,
            goal
        )

        print(
            f"\nSource: {page['title']}"
        )

        print(
            f"Relevance: "
            f"{score:.2f}"
        )

        # Reject obviously irrelevant pages.

        if score < 0.20:

            print(
                "❌ LOW RELEVANCE"
            )

            continue

        sentences = re.split(
            r"(?<=[.!?])\s+",
            page["text"]
        )

        claims = []

        goal_words = [
            word.lower()
            for word in re.findall(
                r"[a-zA-Z0-9]+",
                goal
            )
            if len(word) >= 4
        ]

        for sentence in sentences:

            sentence_lower = (
                sentence.lower()
            )

            matches = sum(
                word in sentence_lower
                for word in goal_words
            )

            if matches >= 1:

                if 40 <= len(
                    sentence
                ) <= 1000:

                    claims.append(
                        sentence.strip()
                    )

            if len(claims) >= 8:
                break

        if claims:

            evidence.append({
                "title":
                    page["title"],
                "url":
                    page["url"],
                "claims":
                    claims,
                "relevance":
                    score
            })

            print(
                f"✓ Claims extracted: "
                f"{len(claims)}"
            )

        else:

            print(
                "❌ No useful claims"
            )

    return evidence


# ============================================================
# CROSS-SOURCE VERIFICATION
# ============================================================

def verify(evidence):

    banner(
        "🔍 CROSS-SOURCE VERIFY"
    )

    if not evidence:

        return []

    # Count repeated concepts/words across sources.

    all_text = ""

    for item in evidence:

        all_text += " ".join(
            item["claims"]
        ).lower() + " "

    words = re.findall(
        r"\b[a-zA-Z]{5,}\b",
        all_text
    )

    frequency = {}

    for word in words:

        frequency[word] = (
            frequency.get(word, 0)
            + 1
        )

    verified = []

    for item in evidence:

        claims = []

        for claim in item[
            "claims"
        ]:

            claim_words = re.findall(
                r"\b[a-zA-Z]{5,}\b",
                claim.lower()
            )

            overlap = sum(
                frequency.get(
                    word,
                    0
                ) > 1
                for word in claim_words
            )

            if overlap >= 2:

                claims.append(
                    claim
                )

        if claims:

            verified.append({
                "title":
                    item["title"],
                "url":
                    item["url"],
                "claims":
                    claims
            })

            print(
                f"✓ {item['title']}"
            )

        else:

            print(
                f"⚠ Weak evidence: "
                f"{item['title']}"
            )

    return verified


# ============================================================
# ANSWER
# ============================================================

def answer(
    goal,
    evidence
):

    banner(
        "✅ ANSWER WITH EVIDENCE"
    )

    print(
        f"\n🎯 GOAL\n{goal}\n"
    )

    if not evidence:

        print(
            "❌ No trustworthy "
            "evidence was found."
        )

        return

    print(
        f"Verified sources: "
        f"{len(evidence)}"
    )

    for index, item in enumerate(
        evidence,
        1
    ):

        print(
            f"\n[{index}] "
            f"{item['title']}"
        )

        print(
            f"Source: {item['url']}"
        )

        for claim in item[
            "claims"
        ][:5]:

            print(
                f"  • {claim}"
            )


# ============================================================
# AGENT
# ============================================================

async def agent(goal):

    banner(
        "🤖 AUTONOMOUS WEB RESEARCH AGENT"
    )

    print(
        f"\n🎯 GOAL\n{goal}"
    )

    # --------------------------------------------------------
    # PLAN
    # --------------------------------------------------------

    banner(
        "🧠 PLAN"
    )

    queries = [
        goal,
        f"{goal} official",
        f"{goal} research",
        f"{goal} 2026",
    ]

    for i, query in enumerate(
        queries,
        1
    ):

        print(
            f"{i}. {query}"
        )

    # --------------------------------------------------------
    # BROWSER
    # --------------------------------------------------------

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=False
        )

        page = await browser.new_page()

        all_results = []

        # ----------------------------------------------------
        # SEARCH
        # ----------------------------------------------------

        for query in queries:

            results = await search(
                page,
                query
            )

            all_results.extend(
                results
            )

        # Deduplicate.

        unique = {}

        for result in all_results:

            unique[
                result["url"]
            ] = result

        results = list(
            unique.values()
        )

        print(
            f"\n📊 Unique sources: "
            f"{len(results)}"
        )

        # ----------------------------------------------------
        # BROWSE
        # ----------------------------------------------------

        banner(
            "🌐 BROWSE + 📖 READ"
        )

        pages = []

        for result in results[
            :MAX_PAGES
        ]:

            page_data = await browse(
                page,
                result
            )

            if page_data:

                pages.append(
                    page_data
                )

        await browser.close()

    # --------------------------------------------------------
    # EXTRACT
    # --------------------------------------------------------

    evidence = extract_evidence(
        pages,
        goal
    )

    # --------------------------------------------------------
    # VERIFY
    # --------------------------------------------------------

    verified = verify(
        evidence
    )

    # --------------------------------------------------------
    # FEEDBACK LOOP
    # --------------------------------------------------------

    if len(verified) < 2:

        banner(
            "🔄 INSUFFICIENT EVIDENCE"
        )

        print(
            "The agent would normally "
            "perform another targeted "
            "search here."
        )

        retry_query = (
            f"{goal} "
            "independent sources"
        )

        async with async_playwright() as p:

            browser = (
                await p.chromium.launch(
                    headless=False
                )
            )

            page = (
                await browser.new_page()
            )

            retry_results = await search(
                page,
                retry_query
            )

            for result in retry_results[
                :4
            ]:

                page_data = (
                    await browse(
                        page,
                        result
                    )
                )

                if page_data:

                    pages.append(
                        page_data
                    )

            await browser.close()

        evidence = extract_evidence(
            pages,
            goal
        )

        verified = verify(
            evidence
        )

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

    answer(
        goal,
        verified
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    if len(sys.argv) < 2:

        print(
            'Usage:\n'
            'python3 web_agent.py '
            '"Research autonomous QA agents"'
        )

        sys.exit(1)

    goal = " ".join(
        sys.argv[1:]
    )

    asyncio.run(
        agent(goal)
    )
