from pathlib import Path
import shutil
import py_compile
import sys
import re

FILE = Path("qa_agent_v8_8_FINAL.py")
BACKUP = Path("qa_agent_v8_8_FINAL.py.FINAL_SAFE_BACKUP")

print("=" * 78)
print("V8.8 FINAL SINGLE-FAILURE FIX")
print("=" * 78)

if not FILE.exists():
    print(f"ERROR: {FILE} not found")
    sys.exit(1)

shutil.copy2(FILE, BACKUP)
print(f"Backup created: {BACKUP}")

text = FILE.read_text()

# Find the complete BUTTON execution block beginning with:
#         if s == "BUTTON":
# and ending immediately before:
#         raise RuntimeError(f"unsupported semantic behavior: {s}")

pattern = r'''
        if\ s\ ==\ "BUTTON":
.*?
            return\ detail

(?=        raise\ RuntimeError\(f"unsupported\ semantic\ behavior:\ \{s\}"\))
'''

replacement = '''        if s == "BUTTON":
            # ==========================================================
            # BUTTON PRECONDITION EXECUTION
            # ==========================================================
            # Do not force-click disabled controls.
            # A disabled control requires its real application precondition.
            # TinyMCE Undo becomes enabled only after editor content changes.
            # ==========================================================

            try:
                disabled = await loc.is_disabled()
            except Exception:
                disabled = False

            label_lower = (clean or label or "").strip().lower()

            if disabled:

                if label_lower == "undo":

                    # TinyMCE editor content lives inside an iframe.
                    editor = None

                    iframe_candidates = [
                        page.frame_locator("iframe.tox-edit-area__iframe").locator("body#tinymce"),
                        page.frame_locator("iframe").locator("body#tinymce"),
                    ]

                    for candidate in iframe_candidates:
                        try:
                            if await candidate.count():
                                editor = candidate
                                break
                        except Exception:
                            pass

                    if editor is None:
                        raise RuntimeError(
                            "Undo precondition failed: TinyMCE editor "
                            "body#tinymce was not found inside an iframe"
                        )

                    try:
                        before_editor = await editor.inner_text()

                        await editor.click()
                        await editor.press("End")
                        await editor.type(" QA_V8_8_UNDO_PROBE")

                        await page.wait_for_timeout(500)

                    except Exception as exc:
                        raise RuntimeError(
                            f"Undo precondition editor modification failed: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                    # Re-resolve Undo from the CURRENT DOM after state change.
                    try:
                        refreshed = await self._resolve_locator(
                            page,
                            "BUTTON",
                            selector,
                            label
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"Undo re-resolution failed: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc

                    if refreshed is None:
                        raise RuntimeError(
                            "Undo button could not be resolved after "
                            "editor modification"
                        )

                    loc = refreshed

                    # Wait briefly for TinyMCE state propagation.
                    enabled = False

                    for _ in range(20):
                        try:
                            if not await loc.is_disabled():
                                enabled = True
                                break
                        except Exception:
                            pass

                        await page.wait_for_timeout(100)

                    if not enabled:
                        raise RuntimeError(
                            "Undo remained disabled after genuine TinyMCE "
                            "editor modification"
                        )

                    detail["precondition"] = (
                        "TinyMCE editor modified to enable Undo"
                    )
                    detail["editor_before"] = before_editor

                else:
                    raise RuntimeError(
                        f"button is disabled and no safe precondition "
                        f"exists for label={clean!r}"
                    )

            # ==========================================================
            # REAL BUTTON EXECUTION
            # ==========================================================

            dialogs = []

            def dialog_handler(dialog):
                dialogs.append(dialog.message)
                asyncio.create_task(dialog.accept())

            page.on("dialog", dialog_handler)

            try:
                await loc.click()
                await page.wait_for_timeout(250)
            finally:
                try:
                    page.remove_listener("dialog", dialog_handler)
                except Exception:
                    pass

            detail.update(
                dialog_count=len(dialogs),
                dialog_messages=dialogs,
                after_url=canon(page.url)
            )
            return detail

'''

matches = list(
    re.finditer(
        pattern,
        text,
        flags=re.DOTALL | re.VERBOSE
    )
)

if len(matches) != 1:
    print("=" * 78)
    print(f"ERROR: Expected exactly 1 BUTTON execution block; found {len(matches)}")
    print("Restoring backup.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

text = re.sub(
    pattern,
    replacement,
    text,
    count=1,
    flags=re.DOTALL | re.VERBOSE
)

FILE.write_text(text)

# ----------------------------------------------------------
# SYNTAX VALIDATION
# ----------------------------------------------------------

try:
    py_compile.compile(str(FILE), doraise=True)
except Exception as exc:
    print("=" * 78)
    print("SYNTAX VALIDATION FAILED")
    print(exc)
    print("Restoring backup automatically.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

print("=" * 78)
print("SUCCESS: FINAL BUTTON PRECONDITION FIX APPLIED")
print("TinyMCE iframe handling: ENABLED")
print("Disabled Undo precondition: ENABLED")
print("Force-click: DISABLED")
print("Python syntax: VALID")
print(f"Backup: {BACKUP}")
print("=" * 78)
