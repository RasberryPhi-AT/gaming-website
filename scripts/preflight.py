"""
Pre-commit preflight check that runs inside the GitHub Actions workflow
before 'git add games/'. Detects and auto-fixes common issues that would
break Cloudflare deployment, then uses Groq to diagnose anything it
cannot fix deterministically.
"""
import json
import os
import shutil
import subprocess
import sys
from groq import Groq

REGISTRY = "games/registry.json"
issues   = []
fixed    = []


def log(msg):
    print(f"[preflight] {msg}")


# ── 1. Remove any .git dirs / .gitmodules inside games/ ──────────────────────
# These cause git to commit gitlinks (mode 160000) instead of real files,
# which breaks Cloudflare's build-time submodule resolution step.

for dirpath, dirnames, files in os.walk("games", topdown=False):
    for d in list(dirnames):
        if d == ".git":
            full = os.path.join(dirpath, d)
            shutil.rmtree(full, ignore_errors=True)
            msg = f"removed nested .git at {full}"
            log(msg); fixed.append(msg)
    if ".gitmodules" in files:
        path = os.path.join(dirpath, ".gitmodules")
        os.remove(path)
        msg = f"removed .gitmodules at {path}"
        log(msg); fixed.append(msg)

# ── 2. Un-stage any gitlinks already in the index ────────────────────────────

result = subprocess.run(
    ["git", "ls-files", "--stage", "games/"],
    capture_output=True, text=True,
)
for line in result.stdout.splitlines():
    if line.startswith("160000"):
        path = line.split("\t", 1)[1].strip()
        subprocess.run(["git", "rm", "--cached", path], capture_output=True)
        msg = f"removed gitlink from index: {path}"
        log(msg); fixed.append(msg)

# ── 3. Validate registry.json ─────────────────────────────────────────────────

if os.path.exists(REGISTRY):
    try:
        with open(REGISTRY, "r", encoding="utf-8") as f:
            reg = json.load(f)
        if "games" not in reg or not isinstance(reg["games"], list):
            raise ValueError("missing or invalid 'games' array")
        log(f"registry.json OK — {len(reg['games'])} entries")
    except Exception as exc:
        msg = f"registry.json invalid: {exc}"
        log(msg); issues.append(msg)
else:
    msg = "registry.json missing"
    log(msg); issues.append(msg)

# ── 4. Check registry paths resolve to real directories ──────────────────────

if os.path.exists(REGISTRY):
    try:
        with open(REGISTRY, "r", encoding="utf-8") as f:
            reg = json.load(f)
        for g in reg.get("games", []):
            path = g.get("path", "")
            if path and not os.path.exists(path):
                msg = f"registry entry '{g['name']}' points to missing path: {path}"
                log(msg); issues.append(msg)
    except Exception:
        pass

# ── 5. Ask Groq to diagnose unfixed issues ────────────────────────────────────

if issues:
    log(f"{len(issues)} issue(s) could not be auto-fixed — consulting Groq...")
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])

        # Collect relevant file context
        context_parts = [f"Issues detected:\n" + "\n".join(f"- {i}" for i in issues)]

        if os.path.exists(REGISTRY):
            with open(REGISTRY, "r", encoding="utf-8") as f:
                context_parts.append(f"games/registry.json content:\n{f.read()[:3000]}")

        diagnosis = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{
                "role": "user",
                "content": (
                    "You are a DevOps engineer debugging a Cloudflare Worker deployment.\n\n"
                    + "\n\n".join(context_parts)
                    + "\n\nFor each issue, provide:\n"
                    "1. A plain-English explanation of what caused it\n"
                    "2. The exact shell command or file change needed to fix it\n"
                    "3. Whether the fix has already been applied or still needs manual action\n\n"
                    "Be concise and specific."
                ),
            }],
            temperature=0,
        ).choices[0].message.content.strip()

        log("Groq diagnosis:\n" + diagnosis)

        # Write diagnosis to a file so it appears in the workflow logs
        with open("games/preflight-diagnosis.txt", "w") as f:
            f.write(diagnosis)

    except Exception as exc:
        log(f"Groq call failed: {exc}")

# ── 6. Summary ────────────────────────────────────────────────────────────────

log(f"auto-fixed: {len(fixed)}  unresolved: {len(issues)}")

if fixed:
    log("fixed:\n" + "\n".join(f"  • {f}" for f in fixed))

if issues:
    log("unresolved:\n" + "\n".join(f"  • {i}" for i in issues))
    # Exit non-zero so the workflow surfaces the problem clearly
    sys.exit(1)
