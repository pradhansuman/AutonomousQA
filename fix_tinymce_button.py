from pathlib import Path
import shutil
import py_compile
import sys

FILE = Path("qa_agent_v8_8_FINAL.py")
BACKUP = Path("qa_agent_v8_8_FINAL.py.before_tinymce_button_fix")

if not FILE.exists():
    print("ERROR: qa_agent_v8_8_FINAL.py not found")
    sys.exit(1)

shutil.copy2(FILE, BACKUP)
text = FILE.read_text()

print("=" * 70)
print("V8.8 TINYMCE BUTTON LOCATOR PATCH")
print("=" * 70)

# Find the _resolve_locator method.
start = text.find("async def _resolve_locator(")

if start == -1:
    print("ERROR: _resolve_locator method not found")
    sys.exit(1)

# Find a safe insertion point immediately after method parameters/body start.
# We patch by adding an early BUTTON label-resolution strategy.
marker = "        #"

# Search within the resolver only for a known locator-related point.
resolver_end = text.find("\n    async def ", start + 1)
if resolver_end == -1:
    resolver_end = len(text)

resolver = text[start:resolver_end]

# Do not apply twice.
if 'BUTTON LABEL FALLBACK' in resolver:
    print("INFO: Button fallback patch already exists.")
else:
    # Insert directly before the first existing 'candidates' initialization
    target = "        candidates = []"

    if target not in resolver:
        print("ERROR: Could not find candidates = [] inside _resolve_locator")
        print("No changes applied.")
        sys.exit(1)

    patch = '''
        # BUTTON LABEL FALLBACK
        # A generic selector such as "button" may not uniquely identify
        # toolbar controls (for example TinyMCE Undo). Resolve by accessible
        # label before falling back to the generic selector.
        if semantic == "BUTTON" and label:
            button_candidates = [
                page.get_by_role("button", name=label, exact=True),
                page.get_by_title(label, exact=True),
                page.locator(f'[aria-label="{label}"]'),
                page.locator(f'[title="{label}"]'),
            ]

            for candidate in button_candidates:
                try:
                    if await candidate.count():
                        visible = candidate.filter(has=page.locator(":visible"))
                        if await visible.count():
                            return visible.first
                        return candidate.first
                except Exception:
                    continue

'''

    resolver = resolver.replace(target, patch + target, 1)
    text = text[:start] + resolver + text[resolver_end:]

    FILE.write_text(text)
    print("PASS: BUTTON label fallback added")

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
print("SUCCESS: PATCH APPLIED")
print(f"Backup: {BACKUP}")
print("Python syntax: VALID")
print("=" * 70)
