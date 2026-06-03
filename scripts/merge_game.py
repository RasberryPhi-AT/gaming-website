import json
import os
import random
import urllib.request
import uuid
import zipfile
from groq import Groq
from duckduckgo_search import DDGS

# Curated card color palettes — one is chosen at random per game
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

# ── 1. Parse event payload ────────────────────────────────────────────────────

# NOTE: GITHUB_* env var names are reserved by GitHub Actions and cannot be set
# in a step's env: block — the workflow passes this as DISPATCH_EVENT_PAYLOAD.
payload   = json.loads(os.environ["DISPATCH_EVENT_PAYLOAD"])
game_name = payload["client_payload"]["gameName"]
slug      = game_name.lower().replace(" ", "-")

print(f"[merge_game] Requested: {game_name!r}  slug={slug}")

client = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── 2. Web search for a downloadable zip ─────────────────────────────────────

query   = f'"{game_name}" html5 game open source zip download github'
results = list(DDGS().text(query, max_results=5))

search_block = "\n\n".join(
    f"Title: {r.get('title', '')}\nURL: {r.get('href', '')}\nSnippet: {r.get('body', '')}"
    for r in results
)

print(f"[merge_game] Search: {len(results)} result(s)")

# ── 3. Ask Groq to identify a direct zip download link ───────────────────────

url_response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{
        "role": "user",
        "content": (
            f'You are a research assistant locating open-source HTML5 game files.\n\n'
            f'Search results for "{game_name}":\n{search_block}\n\n'
            f'Look through the snippets and determine a single direct .zip download URL '
            f'(e.g. a GitHub release asset or /archive/ link) that contains the HTML5 game assets.\n'
            f'If no such link is clearly present or inferable, respond with exactly: FAILED\n'
            f'Otherwise respond with only the raw download URL — no punctuation, no explanation.'
        ),
    }],
    temperature=0,
)

zip_url = url_response.choices[0].message.content.strip()
print(f"[merge_game] Groq returned: {zip_url!r}")

# ── 4. Exit cleanly if no zip was found ──────────────────────────────────────

if zip_url == "FAILED":
    print("[merge_game] No downloadable zip found. Exiting safely.")
    raise SystemExit(0)

# ── 5. Download and unpack the archive ───────────────────────────────────────

archive_path = f"/tmp/{slug}.zip"
extract_dir  = f"games/{slug}"

print(f"[merge_game] Downloading {zip_url} → {archive_path}")
urllib.request.urlretrieve(zip_url, archive_path)

os.makedirs(extract_dir, exist_ok=True)

print(f"[merge_game] Extracting → {extract_dir}/")
with zipfile.ZipFile(archive_path, "r") as zf:
    zf.extractall(extract_dir)

os.remove(archive_path)

# ── 6. Locate index.html and resolve its relative path ───────────────────────

index_rel = None
for dirpath, _dirs, files in os.walk(extract_dir):
    if "index.html" in files:
        index_rel = os.path.join(dirpath, "index.html").replace("\\", "/")
        break

if index_rel is None:
    print("[merge_game] WARNING: index.html not found in extracted archive.")
    index_rel = f"{extract_dir}/index.html"

game_path = index_rel[: index_rel.rfind("/") + 1]
print(f"[merge_game] Entry point: {index_rel}")
print(f"[merge_game] Game path:   {game_path}")

# ── 7. Load (or create) games/registry.json ──────────────────────────────────

registry_path = "games/registry.json"

if os.path.exists(registry_path):
    with open(registry_path, "r", encoding="utf-8") as f:
        registry_text = f.read()
else:
    registry_text = json.dumps({"games": []}, indent=2)

# ── 8. Ask Groq to append a new record and return updated registry JSON ───────

game_id = str(uuid.uuid4()).replace("-", "")[:8]
palette = random.choice(PALETTES)

registry_response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{
        "role": "user",
        "content": (
            f'You are a JSON editor maintaining a game registry file.\n\n'
            f'Current registry:\n{registry_text}\n\n'
            f'Append a new game entry using these pre-assigned values exactly as given:\n'
            f'- "id": "{game_id}"\n'
            f'- "category": "web"\n'
            f'- "name": "{game_name}"\n'
            f'- "path": "{game_path}"\n'
            f'- "color": "{palette["color"]}"\n'
            f'- "accent": "{palette["accent"]}"\n'
            f'- "highlight": "{palette["highlight"]}"\n\n'
            f'You must generate the following fields based on your knowledge of the game:\n'
            f'- "description": a concise 1-2 sentence summary of what the game is and why it is fun\n'
            f'- "tags": an array of 2-4 short genre/style strings (e.g. "Puzzle", "Action", "Retro")\n\n'
            f'Return ONLY the complete updated JSON object — '
            f'no markdown fences, no explanation, no extra text.'
        ),
    }],
    temperature=0.3,
)

updated_json = registry_response.choices[0].message.content.strip()

# Strip accidental markdown fences if the model wrapped its output
if updated_json.startswith("```"):
    lines = updated_json.splitlines()
    updated_json = "\n".join(l for l in lines if not l.startswith("```")).strip()

# Validate before writing — bail rather than corrupt the registry
try:
    json.loads(updated_json)
except json.JSONDecodeError as exc:
    print(f"[merge_game] Groq returned invalid JSON: {exc}")
    print(updated_json)
    raise SystemExit(1)

with open(registry_path, "w", encoding="utf-8") as f:
    f.write(updated_json)

print(f"[merge_game] registry.json updated — {game_name!r} added (id={game_id})")
