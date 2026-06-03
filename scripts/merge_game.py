import json
import os
import random
import urllib.request
import uuid
import zipfile
from groq import Groq
from duckduckgo_search import DDGS

# ── Card color palettes — one picked at random per game ──────────────────────

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

# ── 1. Parse incoming event payload ──────────────────────────────────────────
#
# The workflow passes the dispatch event as DISPATCH_EVENT_PAYLOAD because
# GitHub Actions reserves all GITHUB_* env var names and silently drops any
# custom variable whose name starts with that prefix.

payload   = json.loads(os.environ["DISPATCH_EVENT_PAYLOAD"])
game_name = payload["client_payload"]["gameName"]
slug      = game_name.lower().replace(" ", "-")

print(f"[merge_game] game={game_name!r}  slug={slug}")

client = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── 2. DuckDuckGo search ──────────────────────────────────────────────────────

query   = f'"{game_name}" html5 game open source zip download github'
results = list(DDGS().text(query, max_results=5))

search_block = "\n\n".join(
    f"Title: {r.get('title', '')}\nURL: {r.get('href', '')}\nSnippet: {r.get('body', '')}"
    for r in results
)

print(f"[merge_game] search returned {len(results)} result(s)")

# ── 3. Ask Groq for a direct zip download URL ─────────────────────────────────

url_resp = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{
        "role": "user",
        "content": (
            f'You are a research assistant locating open-source HTML5 browser game files.\n\n'
            f'Web search results for "{game_name}":\n{search_block}\n\n'
            f'Find a single direct downloadable .zip URL that contains the HTML5 game assets '
            f'(e.g. a GitHub release asset or /archive/ link). The game must be browser-compatible.\n'
            f'If no verifiable link is present or inferable from the results, respond with exactly '
            f'the word: FAILED\n'
            f'Otherwise output only the raw URL — no punctuation, no markdown, no explanation.'
        ),
    }],
    temperature=0,
)

zip_url = url_resp.choices[0].message.content.strip()
print(f"[merge_game] zip_url={zip_url!r}")

# ── 4. Exit safely if nothing was found ──────────────────────────────────────

if zip_url == "FAILED":
    print("[merge_game] No downloadable zip found. Exiting.")
    raise SystemExit(0)

# ── 5. Download archive and extract into games/<slug>/ ───────────────────────

archive_path = f"/tmp/{slug}.zip"
extract_dir  = f"games/{slug}"

print(f"[merge_game] downloading → {archive_path}")
urllib.request.urlretrieve(zip_url, archive_path)

os.makedirs(extract_dir, exist_ok=True)

print(f"[merge_game] extracting → {extract_dir}/")
with zipfile.ZipFile(archive_path, "r") as zf:
    zf.extractall(extract_dir)

os.remove(archive_path)

# ── 6. Recursively locate the index.html entry point ─────────────────────────

index_rel = None
for dirpath, _dirs, files in os.walk(extract_dir):
    if "index.html" in files:
        index_rel = os.path.join(dirpath, "index.html").replace("\\", "/")
        break

if index_rel is None:
    print("[merge_game] WARNING: index.html not found — using fallback path")
    index_rel = f"{extract_dir}/index.html"

game_path = index_rel[: index_rel.rfind("/") + 1]
print(f"[merge_game] entry={index_rel}  path={game_path}")

# ── 7. Load or initialise games/registry.json ────────────────────────────────

registry_path = "games/registry.json"

if os.path.exists(registry_path):
    with open(registry_path, "r", encoding="utf-8") as f:
        registry_text = f.read()
else:
    print("[merge_game] registry.json missing — creating default")
    registry_text = json.dumps({"games": []}, indent=2)

# ── 8. Ask Groq to append the new game record and return clean JSON ───────────

game_id = str(uuid.uuid4()).replace("-", "")[:8]
palette = random.choice(PALETTES)

registry_resp = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{
        "role": "user",
        "content": (
            f'You are a JSON editor maintaining an arcade game registry.\n\n'
            f'Current registry JSON:\n{registry_text}\n\n'
            f'Append one new entry to the "games" array using the pre-assigned values below exactly '
            f'as provided — do not alter them:\n'
            f'  "id": "{game_id}"\n'
            f'  "category": "web"\n'
            f'  "name": "{game_name}"\n'
            f'  "path": "{game_path}"\n'
            f'  "color": "{palette["color"]}"\n'
            f'  "accent": "{palette["accent"]}"\n'
            f'  "highlight": "{palette["highlight"]}"\n\n'
            f'Generate the remaining fields from your knowledge of the game:\n'
            f'  "description": a punchy 1-2 sentence abstract explaining the game and why it is fun\n'
            f'  "tags": an array of 2-4 short genre or style label strings\n\n'
            f'Return ONLY the complete updated JSON object. '
            f'No markdown fences, no commentary, no extra text whatsoever.'
        ),
    }],
    temperature=0.3,
)

updated_json = registry_resp.choices[0].message.content.strip()

# Strip markdown fences the model may have added despite instructions
if "```" in updated_json:
    lines = updated_json.splitlines()
    updated_json = "\n".join(l for l in lines if not l.startswith("```")).strip()

# Validate — abort rather than corrupt the registry
try:
    json.loads(updated_json)
except json.JSONDecodeError as exc:
    print(f"[merge_game] invalid JSON from Groq: {exc}")
    print(updated_json)
    raise SystemExit(1)

with open(registry_path, "w", encoding="utf-8") as f:
    f.write(updated_json)

print(f"[merge_game] done — {game_name!r} written to registry (id={game_id})")
