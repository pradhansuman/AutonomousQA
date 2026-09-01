#!/usr/bin/env bash
set -euo pipefail

FILE="qa_agent_v8_8_FINAL.py"
BACKUP="${FILE}.before_number_input_execution_$(date +%Y%m%d_%H%M%S)"

echo "============================================================"
echo "FIXING NUMBER_INPUT EXECUTION"
echo "============================================================"

cp "$FILE" "$BACKUP"
echo "Backup: $BACKUP"

python3 - <<'PY'
from pathlib import Path

p = Path("qa_agent_v8_8_FINAL.py")
text = p.read_text()

old = '''if s in ("TEXT_INPUT", "TEXTAREA"):'''

if old not in text:
    raise SystemExit(
        "ERROR: Could not find TEXT_INPUT execution block. No changes made."
    )

new = '''if s in ("TEXT_INPUT", "NUMBER_INPUT", "TEXTAREA"):'''

text = text.replace(old, new, 1)

p.write_text(text)

print("SUCCESS: NUMBER_INPUT added to semantic execution block.")
PY

echo
echo "============================================================"
echo "SYNTAX VALIDATION"
echo "============================================================"

python3 -m py_compile "$FILE"

echo
echo "SUCCESS: NUMBER_INPUT patch applied and syntax is valid."
