import json
import os
import urllib.request
import zipfile
from groq import Groq
from duckduckgo_search import DDGS

# ── 1. Parse event payload ────────────────────────────────────────────────────

payload   = json.loads(os.environ["DISPATCH_EVENT_PAYLOAD"])
game_name = payload["client_payload"]["gameName"]
slug      = game_name.lower().replace(" ", "-")

print(f"[merge_game] Requested game: {game_name!r}  (slug: {slug})")

# ── 2. Web search for a downloadable zip ─────────────────────────────────────

query   = f'"{game_name}" html5 game open source zip download github'
results = list(DDGS().text(query, max_results=5))

search_block = "\n\n".join(
    f"Title: {r.get('title', '')}\nURL: {r.get('href', '')}\nSnippet: {r.get('body', '')}"
    for r in results
)

print(f"[merge_game] Search returned {len(results)} result(s)")

# ── 3. Ask Groq to extract a direct zip download URL ─────────────────────────

client = Groq(api_key=os.environ["GROQ_API_KEY"])

url_prompt = f"""You are a research assistant helping locate open-source HTML5 game files.

Below are web search results for the game "{game_name}":

{search_block}

Task: Identify a single direct URL that downloads a zip archive containing the game's files.
The URL should point to a .zip file (e.g. a GitHub release asset or archive download).
If no such link is clearly present or inferable, respond with exactly: FAILED
Otherwise respond with only the raw URL — no explanation, no markdown, no punctuation around it."""

response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": url_prompt}],
    temperature=0,
)

zip_url = response.choices[0].message.content.strip()
print(f"[merge_game] Groq returned: {zip_url!r}")

if zip_url == "FAILED":
    print("[merge_game] No downloadable zip found. Exiting.")
    raise SystemExit(0)

# ── 4. Download and extract the archive ──────────────────────────────────────

archive_path = f"/tmp/{slug}.zip"
extract_dir  = f"games/{slug}"

print(f"[merge_game] Downloading {zip_url} -> {archive_path}")
urllib.request.urlretrieve(zip_url, archive_path)

os.makedirs(extract_dir, exist_ok=True)

print(f"[merge_game] Extracting into {extract_dir}/")
with zipfile.ZipFile(archive_path, "r") as zf:
    zf.extractall(extract_dir)

os.remove(archive_path)

# ── 5. Locate index.html inside the extracted tree ───────────────────────────

index_rel = None
for dirpath, _dirs, files in os.walk(extract_dir):
    if "index.html" in files:
        full = os.path.join(dirpath, "index.html")
        index_rel = full.replace("\\", "/")
        break

if index_rel is None:
    print("[merge_game] WARNING: index.html not found in extracted archive.")
    index_rel = f"{extract_dir}/index.html"

# Derive the folder path (strip trailing filename)
game_path = index_rel[: index_rel.rfind("/") + 1]

print(f"[merge_game] Entry point: {index_rel}")
print(f"[merge_game] Game path:   {game_path}")

# ── 6. Load (or create) registry.json ────────────────────────────────────────

registry_path = "games/registry.json"

if os.path.exists(registry_path):
    with open(registry_path, "r", encoding="utf-8") as f:
        registry_text = f.read()
else:
    registry_text = json.dumps({"games": []}, indent=2)

# ── 7. Ask Groq to append a new game record and return updated JSON ───────────

registry_prompt = f"""You are a JSON editor maintaining a game registry file.

Current registry JSON:
{registry_text}

Task: Append a new game entry for "{game_name}" with the following rules:
- "id": a unique lowercase 8-character alphanumeric string not already in the registry
- "category": "web"
- "name": "{game_name}"
- "path": "{game_path}"
- "index": "{index_rel}"
- "description": a concise 1-2 sentence summary of what the game is and why it's fun
- "tags": an array of 2-4 short genre/style tags (e.g. "Puzzle", "Action", "Retro")
- "color": a dark background hex color that fits the game's aesthetic
- "accent": a vivid accent hex color that fits the game's aesthetic
- "highlight": a light or contrasting hex color for text/highlights

Return ONLY the complete updated JSON object — no markdown fences, no explanation, no extra text."""

reg_response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": registry_prompt}],
    temperature=0.4,
)

updated_json_text = reg_response.choices[0].message.content.strip()

# Strip accidental markdown fences if the model included them anyway
if updated_json_text.startswith("```"):
    lines = updated_json_text.splitlines()
    updated_json_text = "\n".join(
        line for line in lines if not line.startswith("```")
    ).strip()

# Validate before writing
try:
    json.loads(updated_json_text)
except json.JSONDecodeError as exc:
    print(f"[merge_game] Groq returned invalid JSON: {exc}")
    print(updated_json_text)
    raise SystemExit(1)

with open(registry_path, "w", encoding="utf-8") as f:
    f.write(updated_json_text)

print(f"[merge_game] registry.json updated with entry for {game_name!r}")
