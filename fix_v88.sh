#!/bin/bash
set -e

FILE="qa_agent_v8_8_FINAL.py"

echo "============================================================"
echo "V8.8 AUTOMATIC SEMANTIC PATCH"
echo "============================================================"

if [ ! -f "$FILE" ]; then
    echo "❌ ERROR: $FILE not found."
    exit 1
fi

# Backup
BACKUP="${FILE}.backup_$(date +%Y%m%d_%H%M%S)"
cp "$FILE" "$BACKUP"

echo "✅ Backup created:"
echo "   $BACKUP"

# Run Python patcher
python3 - "$FILE" <<'PY'
import sys
from pathlib import Path

file_path = Path(sys.argv[1])
text = file_path.read_text()

original = text


# ============================================================
# PATCH 1: DISCOVERY SEMANTIC CLASSIFICATION
# ============================================================

old = '''elif tag=="input" and typ=="file":
                    semantic="FILE_UPLOAD"; action="upload"

                elif (
                    tag=="input"
                    and ident=="sliderValue"
                    and any(
                        str(other.get("type") or "").lower()=="range"
                        or str(other.get("role") or "").lower()=="slider"
                        for other in (raw or [])
                    )
                ):
                    # DemoQA #sliderValue is the companion value field,
                    # not the executable slider control. The actual
                    # executable control is #slider (type=range).
                    continue

                elif tag=="input" or role=="textbox":
                    semantic="TEXT_INPUT"; action="fill"
'''

new = '''elif (
                    tag=="input"
                    and ident=="sliderValue"
                    and any(
                        str(other.get("type") or "").lower()=="range"
                        or str(other.get("role") or "").lower()=="slider"
                        for other in (raw or [])
                    )
                ):
                    # DemoQA #sliderValue is the companion value field,
                    # not the executable slider control.
                    continue

                elif tag == "input":

                    if typ == "file":
                        semantic = "FILE_UPLOAD"
                        action = "upload"

                    elif typ in (
                        "submit",
                        "button",
                        "reset",
                        "image"
                    ):
                        semantic = "BUTTON"
                        action = "click"

                    elif typ == "number":
                        semantic = "NUMBER_INPUT"
                        action = "fill"

                    elif typ == "range":
                        semantic = "SLIDER"
                        action = "adjust"

                    else:
                        semantic = "TEXT_INPUT"
                        action = "fill"

                elif role == "textbox":
                    semantic = "TEXT_INPUT"
                    action = "fill"
'''

if old in text:
    text = text.replace(old, new, 1)
    print("✅ PATCH 1: Input semantic classification fixed")
else:
    print("⚠️ PATCH 1: Exact block not found; skipping")


# ============================================================
# PATCH 2: ADD NUMBER_INPUT TO ACTION MAP
# ============================================================

if '"NUMBER_INPUT"' not in text:
    old = '"TEXT_INPUT": "fill",'

    if old in text:
        text = text.replace(
            old,
            '''"TEXT_INPUT": "fill",
            "NUMBER_INPUT": "fill",''',
            1
        )
        print("✅ PATCH 2: NUMBER_INPUT action added")
    else:
        print("⚠️ PATCH 2: Action map location not found")
else:
    print("ℹ️ PATCH 2: NUMBER_INPUT already exists")


# ============================================================
# PATCH 3: ADD NUMBER_INPUT TO RISK MAP
# ============================================================

if '"NUMBER_INPUT": 60' not in text:

    old = '"TEXT_INPUT": 60,'

    if old in text:
        text = text.replace(
            old,
            '''"TEXT_INPUT": 60,
            "NUMBER_INPUT": 60,''',
            1
        )
        print("✅ PATCH 3: NUMBER_INPUT risk added")
    else:
        print("⚠️ PATCH 3: Risk map location not found")

else:
    print("ℹ️ PATCH 3: NUMBER_INPUT risk already exists")


# ============================================================
# PATCH 4: EXECUTION SUPPORT FOR NUMBER_INPUT
# ============================================================

old = '''if s in ("TEXT_INPUT", "TEXTAREA"):'''

if old in text:

    text = text.replace(
        old,
        '''if s in ("TEXT_INPUT", "TEXTAREA", "NUMBER_INPUT"):''',
        1
    )

    print("✅ PATCH 4A: NUMBER_INPUT execution enabled")

else:
    print("⚠️ PATCH 4A: TEXT_INPUT execution condition not found")


# Change text probe safely for number inputs.
old = '''probe = "QA_V8_7_20_PROBE"'''

new = '''if s == "NUMBER_INPUT" or input_type == "number":
                probe = "12345"
            else:
                probe = "QA_V8_8_PROBE"'''

if old in text:
    text = text.replace(old, new, 1)
    print("✅ PATCH 4B: Numeric probe handling added")
else:
    print("⚠️ PATCH 4B: Probe assignment not found")


# ============================================================
# PATCH 5: RUNTIME CORRECTION
# ============================================================

old = '''if actual_type == "range" or actual_role == "slider":
                                semantic = "SLIDER"
                                b["semantic"] = "SLIDER"
                                b["action"] = "adjust"
                                self.log("   🔁 SEMANTIC PROMOTION | TEXT_INPUT -> SLIDER")'''

new = '''if actual_type == "range" or actual_role == "slider":
                                semantic = "SLIDER"
                                b["semantic"] = "SLIDER"
                                b["action"] = "adjust"
                                self.log("   🔁 SEMANTIC PROMOTION | TEXT_INPUT -> SLIDER")

                            elif actual_type == "number":
                                semantic = "NUMBER_INPUT"
                                b["semantic"] = "NUMBER_INPUT"
                                b["action"] = "fill"
                                self.log(
                                    "   🔁 SEMANTIC PROMOTION | "
                                    "TEXT_INPUT -> NUMBER_INPUT"
                                )

                            elif actual_type in (
                                "submit",
                                "button",
                                "reset",
                                "image"
                            ):
                                semantic = "BUTTON"
                                b["semantic"] = "BUTTON"
                                b["action"] = "click"
                                self.log(
                                    "   🔁 SEMANTIC PROMOTION | "
                                    "TEXT_INPUT -> BUTTON"
                                )

                            elif actual_type == "file":
                                semantic = "FILE_UPLOAD"
                                b["semantic"] = "FILE_UPLOAD"
                                b["action"] = "upload"
                                self.log(
                                    "   🔁 SEMANTIC PROMOTION | "
                                    "TEXT_INPUT -> FILE_UPLOAD"
                                )'''

if old in text:
    text = text.replace(old, new, 1)
    print("✅ PATCH 5: Runtime semantic correction expanded")
else:
    print("⚠️ PATCH 5: Runtime correction block not found")


# ============================================================
# WRITE RESULT
# ============================================================

if text == original:
    print("❌ NO CHANGES WERE MADE")
    sys.exit(1)

file_path.write_text(text)

print("============================================================")
print("✅ PATCHING COMPLETE")
print("============================================================")
PY


echo
echo "============================================================"
echo "STEP 2: PYTHON SYNTAX VALIDATION"
echo "============================================================"

python3 -m py_compile "$FILE"

echo "✅ Python syntax is valid"


echo
echo "============================================================"
echo "STEP 3: VERIFY OLD BAD RULE"
echo "============================================================"

if grep -n 'elif tag=="input" or role=="textbox"' "$FILE"; then
    echo "❌ ERROR: Old generic input rule still exists!"
    echo "   Restoring backup..."
    cp "$BACKUP" "$FILE"
    exit 1
else
    echo "✅ Old generic input rule removed"
fi


echo
echo "============================================================"
echo "STEP 4: VERIFY NEW SEMANTICS"
echo "============================================================"

grep -n 'NUMBER_INPUT' "$FILE" || true

echo
echo "============================================================"
echo "🎉 V8.8 PATCH COMPLETED SUCCESSFULLY"
echo "============================================================"
echo
echo "Backup:"
echo "  $BACKUP"
echo
echo "Next step:"
echo "  Run the agent ONCE and inspect the new failures."
echo
