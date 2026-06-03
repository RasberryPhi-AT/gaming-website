import json
import os
import re
import urllib.request
import zipfile
from groq import Groq
from duckduckgo_search import DDGS

# ── Helpers ───────────────────────────────────────────────────────────────────

EXTERNAL_RE = re.compile(
    r'(?:'
    r'fetch\s*\(\s*[\'"`]https?://'
    r'|<script[^>]+src=[\'"`]\s*https?://'
    r'|<link[^>]+href=[\'"`]\s*https?://'
    r'|new\s+WebSocket\s*\(\s*[\'"`]wss?://'
    r'|import\s+[\'"`]https?://'
    r'|import\s*\(\s*[\'"`]https?://'
    r')',
    re.IGNORECASE,
)

REQUEST_LOG = "games/request-log.json"


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


def source_files(directory, exts=(".html", ".js", ".css")):
    result = []
    for dp, _, fnames in os.walk(directory):
        for fn in fnames:
            if fn.endswith(exts):
                result.append(os.path.join(dp, fn).replace("\\", "/"))
    return result


def read_safe(path, max_chars=7000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(max_chars)
    except OSError:
        return ""


def strip_fences(text):
    if text.startswith("```"):
        lines = text.splitlines()
        return "\n".join(l for l in lines if not l.startswith("```")).strip()
    return text


def groq_complete(client, prompt, temperature=0):
    return strip_fences(
        client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        ).choices[0].message.content.strip()
    )


def external_refs(directory):
    """Return {filepath: content} for every source file that contains external URLs."""
    hits = {}
    for fp in source_files(directory):
        content = read_safe(fp)
        if EXTERNAL_RE.search(content):
            hits[fp] = content
    return hits


# ── 1. Parse event payload ────────────────────────────────────────────────────

payload   = json.loads(os.environ["DISPATCH_EVENT_PAYLOAD"])
game_name = payload["client_payload"]["gameName"]
slug      = game_name.lower().replace(" ", "-")
print(f"[merge_game] Requested: {game_name!r}  slug={slug}")

client = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── 2. Web search ─────────────────────────────────────────────────────────────

query   = f'"{game_name}" html5 game open source zip download github'
results = list(DDGS().text(query, max_results=5))
search_block = "\n\n".join(
    f"Title: {r.get('title', '')}\nURL: {r.get('href', '')}\nSnippet: {r.get('body', '')}"
    for r in results
)
print(f"[merge_game] Search: {len(results)} result(s)")

# ── 3. Ask Groq for a direct zip URL ─────────────────────────────────────────

zip_url = groq_complete(client, f"""You are a research assistant locating open-source HTML5 game files.

Search results for "{game_name}":
{search_block}

Find ONE direct .zip download URL (e.g. a GitHub release asset or /archive/ link).
If none exists, respond exactly: FAILED
Otherwise respond with only the raw URL — no punctuation, no explanation.""")

print(f"[merge_game] Zip URL: {zip_url!r}")

if zip_url == "FAILED":
    print("[merge_game] No downloadable zip found — logging and exiting.")
    append_log(game_name, "failed", "No downloadable open-source zip found online.")
    raise SystemExit(0)

# ── 4. Download and extract ───────────────────────────────────────────────────

archive_path = f"/tmp/{slug}.zip"
extract_dir  = f"games/{slug}"

print(f"[merge_game] Downloading → {archive_path}")
urllib.request.urlretrieve(zip_url, archive_path)

os.makedirs(extract_dir, exist_ok=True)
with zipfile.ZipFile(archive_path, "r") as zf:
    zf.extractall(extract_dir)
os.remove(archive_path)
print(f"[merge_game] Extracted → {extract_dir}/")

# ── 5. Locate index.html ──────────────────────────────────────────────────────

index_rel = None
for dirpath, _dirs, files in os.walk(extract_dir):
    if "index.html" in files:
        index_rel = os.path.join(dirpath, "index.html").replace("\\", "/")
        break

if index_rel is None:
    print("[merge_game] WARNING: index.html not found in archive.")
    index_rel = f"{extract_dir}/index.html"

game_path = index_rel[: index_rel.rfind("/") + 1]
print(f"[merge_game] Entry: {index_rel}  Path: {game_path}")

# ── 6. Locality check ─────────────────────────────────────────────────────────

print("[merge_game] Checking for external network dependencies...")
ext = external_refs(extract_dir)
is_local = not ext

if not is_local:
    print(f"[merge_game] External refs in {len(ext)} file(s) — asking Groq to localise...")

    for fpath, content in ext.items():
        fixed = groq_complete(client, f"""You are a web developer making an HTML5 game fully self-hosted (no CDN or external API calls).

File path: {fpath}
File content:
{content}

Rewrite the file so it makes NO external network requests:
- Replace any CDN <script src="https://..."> / <link href="https://..."> with local paths, e.g. src="libs/libraryname.min.js"
- Remove or safely stub any fetch()/XMLHttpRequest calls to external APIs that are not essential to gameplay
- For every external library you reference locally, add a comment on its own line: <!-- NEEDS_DOWNLOAD: <original_cdn_url> -->
Return ONLY the complete corrected file — no explanation, no markdown fences.""")

        with open(fpath, "w", encoding="utf-8") as f:
            f.write(fixed)
        print(f"[merge_game] Rewrote {fpath}")

        # Download any libraries flagged by Groq
        for lib_url in re.findall(r'NEEDS_DOWNLOAD:\s*(https?://\S+)', fixed):
            lib_name = lib_url.split("/")[-1].split("?")[0] or "lib.js"
            lib_dir  = os.path.join(game_path, "libs")
            os.makedirs(lib_dir, exist_ok=True)
            lib_dest = os.path.join(lib_dir, lib_name)
            try:
                urllib.request.urlretrieve(lib_url, lib_dest)
                print(f"[merge_game] Downloaded lib → {lib_dest}")
            except Exception as exc:
                print(f"[merge_game] Could not fetch {lib_url}: {exc}")

    # Re-check after fixes
    ext      = external_refs(extract_dir)
    is_local = not ext
    print(f"[merge_game] After fixes: {'fully local' if is_local else f'still external in {len(ext)} file(s)'}")

category = "new-local" if is_local else "new-online"
print(f"[merge_game] Category → {category}")

# ── 7. Thorough bug review ────────────────────────────────────────────────────

print("[merge_game] Running bug review on all source files...")
for fpath in source_files(extract_dir):
    content = read_safe(fpath, max_chars=6000)
    if not content.strip():
        continue

    result = groq_complete(client, f"""You are a senior web developer auditing an HTML5 game file before it ships.

File: {fpath}
Content:
{content}

Thoroughly check for:
- JavaScript runtime errors (undefined variables, missing null/undefined guards, broken callbacks)
- Incorrect or missing asset/script path references
- Syntax errors or typos in JS/HTML/CSS
- Missing event listener cleanup or common memory leaks
- Any console.error or TODO markers indicating incomplete code

If you find bugs, return the COMPLETE corrected file with all fixes applied.
If the file is correct, respond with exactly the word: NO_BUGS""")

    if result == "NO_BUGS":
        continue

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(result)
    print(f"[merge_game] Bug fixes applied → {fpath}")

# ── 8. Load (or create) registry.json ────────────────────────────────────────

registry_path = "games/registry.json"
if os.path.exists(registry_path):
    with open(registry_path, "r", encoding="utf-8") as f:
        registry_text = f.read()
else:
    registry_text = json.dumps({"games": []}, indent=2)

# ── 9. Ask Groq to append a new registry entry ───────────────────────────────

updated = groq_complete(client, f"""You are a JSON editor maintaining a game registry file.

Current registry:
{registry_text}

Append a new entry for "{game_name}" using these exact values:
- "id": unique lowercase 8-character alphanumeric string not already in the registry
- "category": "{category}"
- "name": "{game_name}"
- "path": "{game_path}"
- "index": "{index_rel}"
- "description": concise 1-2 sentence summary of the game and why it is fun
- "tags": array of 2-4 short genre/style strings (e.g. "Puzzle", "Action", "Retro")
- "color": dark background hex color matching the game's aesthetic
- "accent": vivid accent hex color matching the game's aesthetic
- "highlight": light or contrasting hex color for text/highlights

Return ONLY the complete updated JSON object — no markdown fences, no explanation.""", temperature=0.4)

try:
    json.loads(updated)
except json.JSONDecodeError as exc:
    print(f"[merge_game] Groq returned invalid JSON: {exc}\n{updated}")
    raise SystemExit(1)

with open(registry_path, "w", encoding="utf-8") as f:
    f.write(updated)

append_log(game_name, "success", category)
print(f"[merge_game] Done — {game_name!r} added as {category}.")
