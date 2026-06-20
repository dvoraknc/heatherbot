"""One-time backfill: embed all existing profiles' episodic memories into
the semantic-memory vector store so #6 recall works immediately for everyone
(instead of lazily embedding on each user's next message)."""

import glob
import json
import os
import time

from heather import memory_vectors as mv


def main():
    files = sorted(glob.glob("user_profiles/*.json"))
    total_profiles = 0
    total_embeds = 0
    start = time.time()
    for path in files:
        base = os.path.splitext(os.path.basename(path))[0]
        try:
            chat_id = int(base)
        except ValueError:
            continue  # skip non-numeric (group) ids
        try:
            with open(path, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            continue
        n = (
            len(profile.get("session_memories", []) or [])
            + len(profile.get("memorable", []) or [])
            + len(profile.get("personal_notes", []) or [])
        )
        if n == 0:
            continue
        total_profiles += 1
        embeds = mv.index_profile_memories(chat_id, profile)
        total_embeds += embeds
        if total_profiles % 25 == 0:
            print(f"  {total_profiles} profiles, {total_embeds} embeds, "
                  f"{time.time() - start:.0f}s elapsed", flush=True)
    print(f"DONE: {total_profiles} profiles indexed, {total_embeds} new embeds, "
          f"{time.time() - start:.0f}s total", flush=True)


if __name__ == "__main__":
    main()
