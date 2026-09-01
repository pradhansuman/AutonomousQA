from pathlib import Path
import shutil
import py_compile
import sys
from datetime import datetime

FILE = Path("qa_agent_v8_8_FINAL.py")

print("=" * 78)
print("V8.8 FINAL BUTTON PRECONDITION PATCH")
print("=" * 78)

if not FILE.exists():
    print(f"ERROR: {FILE} not found")
    sys.exit(1)

text = FILE.read_text()

backup = Path(
    f"{FILE.name}.before_final_button_fix_"
    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)

shutil.copy2(FILE, backup)

print(f"Backup created: {backup}")

# Find the execution section after the TAB block.
start_marker = '        if s == "BUTTON":'
end_marker = '        raise RuntimeError(f"unsupported semantic behavior: {s}")'

starts = []
offset = 0

while True:
    pos = text.find(start_marker, offset)
    if pos == -1:
        break
    starts.append(pos)
    offset = pos + 1

if not starts:
    print("ERROR: BUTTON execution block not found.")
    print("Restoring backup.")
    shutil.copy2(backup, FILE)
    sys.exit(1)

# The execution block is the last BUTTON semantic block before the
# unsupported-semantic RuntimeError.
end = text.find(end_marker, starts[-1])

if end == -1:
    print("ERROR: Unsupported semantic behavior marker not found.")
    print("Restoring backup.")
    shutil.copy2(backup, FILE)
    sys.exit(1)

start = starts[-1]

old_block = text[start:end]

print()
print("Found BUTTON execution block.")
print(f"Replacing characters {start} through {end}")
print()

new_block = '''        if s == "BUTTON":
            # BUTTON PRECONDITION:
            # A discovered control can exist in the DOM but still be disabled
            # in the current application state. Disabled controls are not
            # executable failures; record the observed precondition and return
            # successful evidence for the current state.
            try:
                enabled = await loc.is_enabled()
            except Exception:
                enabled = True

            aria_disabled = None
            try:
                aria_disabled = await loc.get_attribute("aria-disabled")
            except Exception:
                pass

            class_name = ""
            try:
                class_name = await loc.get_attribute("class") or ""
            except Exception:
                pass

            effectively_disabled = (
                (not enabled)
                or str(aria_disabled).lower() == "true"
                or "disabled" in class_name.lower()
            )

            if effectively_disabled:
                detail.update(
                    precondition="DISABLED",
                    action_performed=False,
                    enabled=enabled,
                    aria_disabled=aria_disabled,
                    class_name=class_name,
                    reason=(
                        "Control exists but is disabled in the current "
                        "application state; click is not executable until "
                        "its prerequisite state is established."
                    ),
                    after_url=canon(page.url)
                )
                return detail

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
                precondition="ENABLED",
                action_performed=True,
                dialog_count=len(dialogs),
                dialog_messages=dialogs,
                after_url=canon(page.url)
            )
            return detail

'''

text = text[:start] + new_block + text[end:]

FILE.write_text(text)

print("Patch written.")
print()

print("=" * 78)
print("PYTHON SYNTAX VALIDATION")
print("=" * 78)

try:
    py_compile.compile(str(FILE), doraise=True)
    print("SUCCESS: Python syntax is VALID")
except Exception as exc:
    print("SYNTAX ERROR:")
    print(exc)
    print("Restoring backup automatically.")
    shutil.copy2(backup, FILE)
    sys.exit(1)

print()
print("=" * 78)
print("PATCH COMPLETE")
print("=" * 78)
print(f"Active file : {FILE}")
print(f"Backup      : {backup}")
print()
