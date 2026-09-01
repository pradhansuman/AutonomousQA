from pathlib import Path
import shutil
import py_compile
import sys

FILE = Path("qa_agent_v8_8_FINAL.py")
BACKUP = Path("qa_agent_v8_8_FINAL.py.before_disabled_button_fix")

if not FILE.exists():
    print("ERROR: qa_agent_v8_8_FINAL.py not found")
    sys.exit(1)

text = FILE.read_text()

print("=" * 70)
print("V8.8 DISABLED BUTTON PRECONDITION PATCH")
print("=" * 70)

# Safety: do not apply twice.
if "TINYMCE UNDO PRECONDITION" in text:
    print("INFO: Patch already exists. No changes made.")
    sys.exit(0)

# Find the semantic execution method.
method = "async def _execute_one_semantic("
start = text.find(method)

if start == -1:
    print("ERROR: _execute_one_semantic method not found.")
    sys.exit(1)

# Find the next method after it.
end = text.find("\n    async def ", start + len(method))

if end == -1:
    end = len(text)

section = text[start:end]

# Find the BUTTON execution block.
needle = 'if s == "BUTTON":'

pos = section.find(needle)

if pos == -1:
    print("ERROR: BUTTON execution block not found.")
    print("No changes made.")
    sys.exit(1)

# Preserve indentation based on actual code.
line_start = section.rfind("\n", 0, pos) + 1
indent = section[line_start:pos]

patch = f'''{indent}# TINYMCE UNDO PRECONDITION
{indent}# Undo is initially disabled until editor state changes.
{indent}# Create a real editor change before executing Undo.
{indent}if s == "BUTTON" and str(label or "").strip().lower() == "undo":
{indent}    try:
{indent}        editor_frame = page.frame_locator("iframe").locator("body#tinymce")
{indent}        if await editor_frame.count():
{indent}            await editor_frame.click()
{indent}            await editor_frame.press("End")
{indent}            await editor_frame.type(" QA_V8_8_PROBE")
{indent}            await page.wait_for_timeout(300)
{indent}    except Exception as exc:
{indent}        self.log(
{indent}            f"   ⚠️ UNDO PRECONDITION FAILED | "
{indent}            f"{{type(exc).__name__}}: {{exc}}"
{indent}        )

{indent}'''

section = section[:line_start] + patch + section[line_start:]
text = text[:start] + section + text[end:]

# Create backup only immediately before writing.
shutil.copy2(FILE, BACKUP)
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

print("PASS: TinyMCE Undo precondition added")
print(f"Backup: {BACKUP}")
print("Python syntax: VALID")
print("=" * 70)
