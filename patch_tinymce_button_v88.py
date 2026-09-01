from pathlib import Path
import shutil
import py_compile
import sys

FILE = Path("qa_agent_v8_8_FINAL.py")
BACKUP = Path("qa_agent_v8_8_FINAL.py.before_disabled_button_patch")

if not FILE.exists():
    print(f"ERROR: {FILE} not found")
    sys.exit(1)

shutil.copy2(FILE, BACKUP)

text = FILE.read_text()

OLD = '''        if s == "BUTTON":
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

NEW = '''        if s == "BUTTON":
            # ----------------------------------------------------------
            # DISABLED BUTTON PRECONDITION HANDLING
            # ----------------------------------------------------------
            # A disabled control cannot produce a valid behavioral result
            # until its application-level precondition has been satisfied.
            # TinyMCE Undo is disabled until editor content changes.
            # ----------------------------------------------------------

            try:
                disabled = await loc.is_disabled()
            except Exception:
                disabled = False

            if disabled:
                label_lower = (clean or "").strip().lower()

                # TinyMCE Undo precondition:
                # Make a real editor change, then resolve Undo again from
                # the CURRENT DOM.
                if label_lower == "undo":
                    editor = page.locator("body#tinymce")

                    try:
                        if await editor.count():
                            before_editor = await editor.inner_text()

                            await editor.click()
                            await editor.press("End")
                            await editor.type(" QA_V8_8_UNDO_PROBE")

                            await page.wait_for_timeout(300)

                            refreshed = await self._resolve_locator(
                                page,
                                "BUTTON",
                                selector,
                                label
                            )

                            if refreshed is None:
                                raise RuntimeError(
                                    "Undo button could not be re-resolved "
                                    "after editor precondition"
                                )

                            loc = refreshed

                            try:
                                disabled = await loc.is_disabled()
                            except Exception:
                                disabled = False

                            if disabled:
                                raise RuntimeError(
                                    "Undo remained disabled after "
                                    "editor content modification"
                                )

                            detail["precondition"] = (
                                "TinyMCE editor modified before Undo"
                            )
                            detail["editor_before"] = before_editor

                    except Exception as exc:
                        raise RuntimeError(
                            f"button precondition failed for Undo: {exc}"
                        ) from exc

                else:
                    raise RuntimeError(
                        f"button is disabled; no safe precondition is "
                        f"implemented for label={clean!r}"
                    )

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

if OLD not in text:
    print("=" * 70)
    print("ERROR: EXACT BUTTON EXECUTION BLOCK NOT FOUND")
    print("No changes were applied.")
    print("=" * 70)
    sys.exit(1)

text = text.replace(OLD, NEW, 1)

FILE.write_text(text)

try:
    py_compile.compile(str(FILE), doraise=True)
except Exception as exc:
    print("=" * 70)
    print("SYNTAX VALIDATION FAILED")
    print(exc)
    print("Restoring backup automatically.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

print("=" * 70)
print("SUCCESS: DISABLED BUTTON PRECONDITION PATCH APPLIED")
print(f"Backup: {BACKUP}")
print("Python syntax: VALID")
print("=" * 70)
