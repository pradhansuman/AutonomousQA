#!/usr/bin/env bash
set -euo pipefail

echo "============================================================"
echo "AUTONOMOUSQA REPOSITORY CLEANUP"
echo "============================================================"

# Files to keep
KEEP_1="qa_agent_v8_8_FINAL.py"
KEEP_2="releases/v8.8/qa_agent_v8_8_FINAL_RELEASE_PASS.py"

echo
echo "The following old QA agent versions will be removed:"
echo

# Remove old root-level qa_agent version files,
# but preserve the canonical V8.8 source.
while IFS= read -r file; do
    if [[ "$file" != "$KEEP_1" ]]; then
        echo "REMOVE: $file"
        git rm "$file"
    fi
done < <(
    git ls-files |
    grep -E '^qa_agent_v.*\.py$' || true
)

echo
echo "============================================================"
echo "FILES REMAINING"
echo "============================================================"

git ls-files | grep -E 'qa_agent.*\.py$|releases/v8\.8/' || true

echo
echo "============================================================"
echo "GIT STATUS"
echo "============================================================"

git status --short

echo
echo "============================================================"
echo "COMMITTING CLEANUP"
echo "============================================================"

git add -A
git commit -m "Clean repository and retain canonical V8.8 release only"

echo
echo "============================================================"
echo "PUSHING TO GITHUB"
echo "============================================================"

git push origin main

echo
echo "============================================================"
echo "CLEANUP COMPLETE"
echo "============================================================"
