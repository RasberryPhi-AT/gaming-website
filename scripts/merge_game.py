import json
import os
import random
import shutil
import subprocess
import time
import urllib.request
import uuid
import zipfile
from ddgs import DDGS
from groq import Groq

# ── Card color palettes ───────────────────────────────────────────────────────

PALETTES = [
    {"color": "#0a0a12", "accent": "#7c3aed", "highlight": "#c4b5fd"},
    {"color": "#0d0000", "accent": "#cc2200", "highlight": "#ff6644"},
    {"color": "#001a0d", "accent": "#22c55e", "highlight": "#bbf7d0"},
    {"color": "#00060f", "accent": "#3b82f6", "highlight": "#93c5fd"},
    {"color": "#0d0010", "accent": "#ec4899", "highlight": "#fbcfe8"},
    {"color": "#0a0800", "accent": "#f59e0b", "highlight": "#fde68a"},
    {"color": "#00080d", "accent": "#06b6d4", "highlight": "#a5f3fc"},
    {"color": "#0d0a00", "accent": "#f97316", "highlight": "#fed7aa"},
    {"color": "#050a00", "accent": "#84cc16", "highlight": "#d9f99d"},
    {"color": "#0a0005", "accent": "#a855f7", "highlight": "#e9d5ff"},
]

REQUEST_LOG  = "games/request-log.json"
REGISTRY     = "games/registry.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def append_log(game_name, status, reason=""):
    log = []
    if os.path.exists(REQUEST_LOG):
        with open(REQUEST_LOG, "r", encoding="utf-8") as f:
            try:
                log = json.load(f)
            except json.JSONDecodeError:
                pass
    log.append({"gameName": game_name, "status": status, "reason": reason})
    with open(REQUEST_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def groq_call(client, messages, temperature=0):
    """Groq completion with up to 3 retries on rate-limit."""
    for attempt in range(3):
        try:
            return (
                client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages,
                    temperature=temperature,
                )
                .choices[0].message.content.strip()
            )
        except Exception as exc:
            if attempt < 2 and ("429" in str(exc) or "rate_limit" in str(exc).lower()):
                wait = 5 * (2 ** attempt)
                print(f"[merge_game] Groq rate limit — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise


def try_zip(url, archive_path, extract_dir):
    """Download a zip and extract it. Returns True on success."""
    try:
        urllib.request.urlretrieve(url, archive_path)
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)
        os.remove(archive_path)
        remove_git_metadata(extract_dir)
        print(f"[merge_game] zip extracted → {extract_dir}/")
        return True
    except Exception as exc:
        print(f"[merge_game] zip failed: {exc}")
        if os.path.exists(archive_path):
            os.remove(archive_path)
        return False


def try_clone(url, extract_dir):
    """Git clone --depth 1 a repo. Returns True on success."""
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, extract_dir],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        remove_git_metadata(extract_dir)
        print(f"[merge_game] cloned → {extract_dir}/")
        return True
    print(f"[merge_game] git clone failed:\n{result.stderr.strip()}")
    return False


def remove_git_metadata(directory):
    """Remove all .git dirs and .gitmodules files so nothing is treated as a submodule."""
    for dirpath, dirnames, files in os.walk(directory, topdown=False):
        for dirname in list(dirnames):
            if dirname == ".git":
                shutil.rmtree(os.path.join(dirpath, dirname), ignore_errors=True)
        if ".gitmodules" in files:
            os.remove(os.path.join(dirpath, ".gitmodules"))


def acquire(url, archive_path, extract_dir):
    """Resolve a URL to game files — zip download or git clone."""
    url = url.strip()
    if not url.startswith("http"):
        return False
    if ".zip" in url:
        return try_zip(url, archive_path, extract_dir)
    if "github.com" in url:
        return try_clone(url, extract_dir)
    # Unknown type: try zip first, fall back to clone
    return try_zip(url, archive_path, extract_dir) or try_clone(url, extract_dir)


def find_file(root, filename):
    """Walk root and return the first matching filepath, or None."""
    for dirpath, _dirs, files in os.walk(root):
        if filename in files:
            return os.path.join(dirpath, filename).replace("\\", "/")
    return None


# ── 1. Parse event payload ────────────────────────────────────────────────────
#
# DISPATCH_EVENT_PAYLOAD is used instead of GITHUB_EVENT_PAYLOAD because
# GitHub Actions reserves and silently drops all GITHUB_* env var names.

payload   = json.loads(os.environ["DISPATCH_EVENT_PAYLOAD"])
game_name = payload["client_payload"]["gameName"]
slug      = game_name.lower().replace(" ", "-")
extract_dir  = f"games/{slug}"
archive_path = f"/tmp/{slug}.zip"

print(f"[merge_game] game={game_name!r}  slug={slug}")

client = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── 2. Blocklist check ───────────────────────────────────────────────────────

BLOCKLIST_PATH = "games/blocklist.json"
if os.path.exists(BLOCKLIST_PATH):
    with open(BLOCKLIST_PATH, "r", encoding="utf-8") as f:
        blocklist = json.load(f)
    for entry in blocklist:
        if entry["slug"] == slug:
            print(f"[merge_game] '{game_name}' is blocklisted: {entry['reason']}")
            append_log(game_name, "failed", f"Blocklisted: {entry['reason']}")
            raise SystemExit(0)

# ── 3. Duplicate detection ───────────────────────────────────────────────────

if os.path.exists(REGISTRY):
    with open(REGISTRY, "r", encoding="utf-8") as f:
        existing = json.load(f)
    known_names = {g["name"].lower() for g in existing.get("games", [])}
    if game_name.lower() in known_names or os.path.exists(extract_dir):
        print(f"[merge_game] '{game_name}' already exists — skipping.")
        raise SystemExit(0)

# ── 3. Web search — narrow first, broad fallback ─────────────────────────────

narrow  = f'"{game_name}" html5 game open source zip download github'
broad   = f'{game_name} html5 browser game open source github'

results = list(DDGS().text(narrow, max_results=5))
print(f"[merge_game] narrow search: {len(results)} result(s)")

if len(results) < 2:
    print("[merge_game] too few results — running broader search")
    extra   = list(DDGS().text(broad, max_results=5))
    seen    = {r["href"] for r in results}
    results += [r for r in extra if r["href"] not in seen]
    print(f"[merge_game] combined: {len(results)} result(s)")

if not results:
    print("[merge_game] no search results — exiting.")
    append_log(game_name, "failed", "Web search returned no results.")
    raise SystemExit(0)

search_block = "\n\n".join(
    f"Title: {r.get('title', '')}\nURL: {r.get('href', '')}\nSnippet: {r.get('body', '')}"
    for r in results
)

# ── 4. Ask Groq for up to 3 ranked candidates ────────────────────────────────

candidates_raw = groq_call(client, messages=[{
    "role": "user",
    "content": (
        f'You are a research assistant finding open-source HTML5 game files.\n\n'
        f'Search results for "{game_name}":\n{search_block}\n\n'
        f'List up to 3 candidate URLs ranked by confidence, one per line. Each must be either:\n'
        f'  - A direct .zip download URL (GitHub release asset, /archive/ link, etc.)\n'
        f'  - A GitHub repository URL (https://github.com/owner/repo)\n'
        f'If no useful source exists, respond with exactly: FAILED\n'
        f'Output raw URLs only — no numbering, no explanation, no markdown.'
    ),
}])

print(f"[merge_game] candidates:\n{candidates_raw}")

if candidates_raw == "FAILED":
    print("[merge_game] Groq found no candidates — exiting.")
    append_log(game_name, "failed", "No downloadable source found online.")
    raise SystemExit(0)

candidates = [u.strip() for u in candidates_raw.splitlines() if u.strip().startswith("http")]

if not candidates:
    print("[merge_game] no valid URLs parsed — exiting.")
    append_log(game_name, "failed", "Groq response contained no valid URLs.")
    raise SystemExit(0)

# ── 5. Try each candidate until one works ────────────────────────────────────

acquired = False
for i, url in enumerate(candidates, 1):
    print(f"[merge_game] trying {i}/{len(candidates)}: {url}")
    if acquire(url, archive_path, extract_dir):
        acquired = True
        break

if not acquired:
    print("[merge_game] all candidates failed — exiting.")
    append_log(game_name, "failed", f"All {len(candidates)} candidate(s) failed to download.")
    raise SystemExit(1)

# ── 6. Optional build step (package.json) ────────────────────────────────────

pkg_json = find_file(extract_dir, "package.json")
if pkg_json:
    pkg_dir = os.path.dirname(pkg_json)
    print(f"[merge_game] package.json found at {pkg_json}")
    try:
        with open(pkg_json, "r", encoding="utf-8") as f:
            pkg_data = json.load(f)
        scripts = pkg_data.get("scripts", {})

        r = subprocess.run(
            ["npm", "install"], cwd=pkg_dir,
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"[merge_game] npm install failed:\n{r.stderr.strip()}")
        elif "build" in scripts:
            r = subprocess.run(
                ["npm", "run", "build"], cwd=pkg_dir,
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0:
                print("[merge_game] npm build succeeded")
            else:
                print(f"[merge_game] npm run build failed:\n{r.stderr.strip()}")
    except Exception as exc:
        print(f"[merge_game] build step error (non-fatal): {exc}")

# ── 7. Locate index.html (after any build output) ────────────────────────────

index_rel = find_file(extract_dir, "index.html")
if index_rel is None:
    print("[merge_game] WARNING: index.html not found — using fallback path")
    index_rel = f"{extract_dir}/index.html"

game_path = index_rel[: index_rel.rfind("/") + 1]
print(f"[merge_game] entry={index_rel}  path={game_path}")

# ── 8. Load or initialise registry.json ──────────────────────────────────────

if os.path.exists(REGISTRY):
    with open(REGISTRY, "r", encoding="utf-8") as f:
        registry_text = f.read()
else:
    print("[merge_game] registry.json missing — creating default")
    registry_text = json.dumps({"games": []}, indent=2)

# ── 9. Ask Groq to append registry entry and return clean JSON ───────────────

game_id = str(uuid.uuid4()).replace("-", "")[:8]
palette = random.choice(PALETTES)

updated_json = groq_call(client, messages=[{
    "role": "user",
    "content": (
        f'You are a JSON editor maintaining an arcade game registry.\n\n'
        f'Current registry JSON:\n{registry_text}\n\n'
        f'Append one new entry to the "games" array. Use these pre-assigned values exactly:\n'
        f'  "id": "{game_id}"\n'
        f'  "category": "web"\n'
        f'  "name": "{game_name}"\n'
        f'  "path": "{game_path}"\n'
        f'  "color": "{palette["color"]}"\n'
        f'  "accent": "{palette["accent"]}"\n'
        f'  "highlight": "{palette["highlight"]}"\n\n'
        f'Generate these fields from your knowledge of the game:\n'
        f'  "description": punchy 1-2 sentence abstract explaining the game and why it is fun\n'
        f'  "tags": array of 2-4 short genre or style label strings\n\n'
        f'Return ONLY the complete updated JSON object — no markdown fences, no explanation.'
    ),
}], temperature=0.3)

if "```" in updated_json:
    lines = updated_json.splitlines()
    updated_json = "\n".join(l for l in lines if not l.startswith("```")).strip()

try:
    json.loads(updated_json)
except json.JSONDecodeError as exc:
    print(f"[merge_game] invalid JSON from Groq: {exc}\n{updated_json}")
    raise SystemExit(1)

with open(REGISTRY, "w", encoding="utf-8") as f:
    f.write(updated_json)

append_log(game_name, "success", "web")
print(f"[merge_game] done — {game_name!r} added to registry (id={game_id})")
