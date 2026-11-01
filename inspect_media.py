"""Ispiše Instagram polja za objavljene reelove iz state.json (samo čita, ništa ne mijenja)."""

import json
from pathlib import Path

import requests

import agent

FIELDS = ["id", "permalink", "timestamp", "media_type", "media_product_type",
          "is_shared_to_feed", "trial_params", "is_trial_reel", "boost_eligibility_info"]

state = json.loads(Path("state.json").read_text())
for p in state["posts"]:
    mid = p.get("media_id")
    if not mid:
        continue
    print(f"\n=== {p['file_name']} ({mid})")
    for f in FIELDS:
        r = requests.get(f"{agent.GRAPH}/{mid}", params={"fields": f, "access_token": agent.ig_token()}, timeout=60)
        print(f"  {f}: {r.json()}")
    r = requests.get(f"{agent.GRAPH}/{mid}/insights", params={"metric": "views,reach", "access_token": agent.ig_token()}, timeout=60)
    print("  insights:", r.json())
