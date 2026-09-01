from pathlib import Path
import shutil
import py_compile
import sys

FILE = Path("qa_agent_v8_8_FINAL.py")
BACKUP = Path("qa_agent_v8_8_FINAL.py.before_semantic_patch")

if not FILE.exists():
    print(f"ERROR: {FILE} not found")
    sys.exit(1)

shutil.copy2(FILE, BACKUP)
text = FILE.read_text()

print("=" * 70)
print("V8.8 TARGETED SEMANTIC PATCH")
print("=" * 70)

# ------------------------------------------------------------
# PATCH 1: Fix discovery classification
# ------------------------------------------------------------

old = '''elif tag=="input" or role=="textbox":
                    semantic="TEXT_INPUT"; action="fill"'''

new = '''elif tag=="input" and typ in ("submit", "button", "reset", "image"):
                    semantic="BUTTON"; action="click"

                elif tag=="input" and typ in (
                    "number", "date", "datetime-local",
                    "month", "time", "week"
                ):
                    semantic="NUMBER_INPUT"; action="fill"

                elif tag=="input" or role=="textbox":
                    semantic="TEXT_INPUT"; action="fill"'''

if old not in text:
    print("ERROR: PATCH 1 discovery block not found.")
    print("Restoring backup. No changes kept.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

text = text.replace(old, new, 1)
print("PASS: PATCH 1 - input classification")


# ------------------------------------------------------------
# PATCH 2: Add NUMBER_INPUT action mapping
# ------------------------------------------------------------

old = '''"TEXT_INPUT": "fill",'''

new = '''"TEXT_INPUT": "fill",
            "NUMBER_INPUT": "fill",'''

if old not in text:
    print("ERROR: PATCH 2 action mapping not found.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

text = text.replace(old, new, 1)
print("PASS: PATCH 2 - NUMBER_INPUT action mapping")


# ------------------------------------------------------------
# PATCH 3: Add NUMBER_INPUT risk mapping
# ------------------------------------------------------------

old = '''"TEXT_INPUT": 60,'''

new = '''"TEXT_INPUT": 60,
            "NUMBER_INPUT": 60,'''

if old not in text:
    print("ERROR: PATCH 3 risk mapping not found.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

text = text.replace(old, new, 1)
print("PASS: PATCH 3 - NUMBER_INPUT risk mapping")


# ------------------------------------------------------------
# PATCH 4: Execute NUMBER_INPUT through fill branch
# ------------------------------------------------------------

old = '''if s in ("TEXT_INPUT", "TEXTAREA"):'''

new = '''if s in ("TEXT_INPUT", "NUMBER_INPUT", "TEXTAREA"):'''

if old not in text:
    print("ERROR: PATCH 4 execution branch not found.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

text = text.replace(old, new, 1)
print("PASS: PATCH 4 - NUMBER_INPUT execution")


# ------------------------------------------------------------
# PATCH 5: Use numeric probe for number inputs
# ------------------------------------------------------------

old = '''probe = "QA_V8_7_20_PROBE"
            element_id = str(await loc.get_attribute("id") or "")'''

new = '''probe = "QA_V8_7_20_PROBE"

            if s == "NUMBER_INPUT" or input_type == "number":
                probe = "42"

            element_id = str(await loc.get_attribute("id") or "")'''

if old not in text:
    print("ERROR: PATCH 5 numeric probe block not found.")
    shutil.copy2(BACKUP, FILE)
    sys.exit(1)

text = text.replace(old, new, 1)
print("PASS: PATCH 5 - numeric probe")


# ------------------------------------------------------------
# WRITE AND VALIDATE
# ------------------------------------------------------------

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
print("SUCCESS: ALL PATCHES APPLIED")
print(f"Backup: {BACKUP}")
print("Python syntax: VALID")
print("=" * 70)
