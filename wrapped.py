import requests
import datetime
import time
import math
import os
from collections import defaultdict

# ================== CONFIG ==================
TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
if not TOKEN:
    TOKEN = input("Paste your GitHub token: ").strip()

if not TOKEN:
    raise SystemExit("❌ Missing GitHub token. Set GITHUB_TOKEN/GH_TOKEN or provide via prompt.")

HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "github-wrapped"
}
YEAR = datetime.datetime.now().year
BASE = "https://api.github.com"

# ================== HELPERS ==================
def api(url):
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        return None
    return r.json()

def bar(pct, width=24):
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)

def pause():
    time.sleep(0.4)

# ================== FETCH USER ==================
user = api(f"{BASE}/user")
login = user["login"]

# ================== FETCH REPOS ==================
repos = []
page = 1
while True:
    data = api(f"{BASE}/user/repos?per_page=100&page={page}&type=owner,member")
    if not data:
        break
    repos.extend(data)
    page += 1

# ================== ANALYSIS ==================
lang_bytes = defaultdict(int)
hour_map = defaultdict(int)
repo_weight = defaultdict(int)
additions = deletions = commits_total = 0

for repo in repos:
    full = repo["full_name"]

    langs = api(f"{BASE}/repos/{full}/languages")
    if langs:
        for l, b in langs.items():
            lang_bytes[l] += b

    commits = api(f"{BASE}/repos/{full}/commits?author={login}&since={YEAR}-01-01T00:00:00Z")
    if commits:
        for c in commits:
            commits_total += 1
            hour = int(c["commit"]["author"]["date"][11:13])
            hour_map[hour] += 1
            repo_weight[full] += 1

    freq = api(f"{BASE}/repos/{full}/stats/code_frequency")
    if freq:
        for w in freq[-20:]:
            additions += max(w[1], 0)
            deletions += abs(min(w[2], 0))

# ================== COMPUTE ==================
total_lang = sum(lang_bytes.values()) or 1
langs_sorted = sorted(lang_bytes.items(), key=lambda x: x[1], reverse=True)[:5]
repos_sorted = sorted(repo_weight.items(), key=lambda x: x[1], reverse=True)[:5]
peak_hour = max(hour_map, key=hour_map.get) if hour_map else "N/A"
refactor_ratio = deletions / max(additions, 1)

# ================== RENDER ==================
print("\n" * 2)
print("════════════════════════════════════════════")
print("🎁      G I T H U B   W R A P P E D")
print("════════════════════════════════════════════")
pause()

print(f"\n👤  {login}")
print(f"📦  Repositories touched : {len(repo_weight)}")
print(f"🧾  Commits this year    : {commits_total}")
print(f"🕒  Peak coding hour     : {peak_hour}:00")
pause()

print("\n🧠  Language Reality")
for l, b in langs_sorted:
    pct = b / total_lang
    print(f"  {l:<12} {bar(pct)} {pct*100:5.1f}%")
pause()

print("\n🔥  Project Gravity")
for r, w in repos_sorted:
    strength = min(w / commits_total if commits_total else 0, 1)
    print(f"  {r:<35} {bar(strength, 16)}")
pause()

print("\n🧹  Code Hygiene")
print(f"  Refactor Ratio : {refactor_ratio:.2f}")
if refactor_ratio > 0.6:
    print("  → You clean systems, not just add features.")
elif refactor_ratio > 0.3:
    print("  → Balanced builder.")
else:
    print("  → Feature-forward coding style.")
pause()

print("\n📈  Final Take")
if commits_total > 500:
    print("  You showed up consistently.")
elif commits_total > 200:
    print("  You worked with intent.")
else:
    print("  You coded selectively — impact over noise.")

print("\n════════════════════════════════════════════")
print("✨  Wrapped complete. See you next year.")
print("════════════════════════════════════════════")