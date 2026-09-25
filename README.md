# Aiko Dataset Generator

Generate 600,000 unique Hinglish conversations between a user and Aiko
(a fixed-personality female companion) for LLM fine-tuning.
Target: 90 days. Quality over speed.

---

## Architecture — 3 layers

1. **GitHub Actions** — cron-driven runs (public repo, unlimited minutes)
2. **MongoDB Atlas** — state only (topic progress, quotas, dedup buffers,
   key stats, run lock). 512MB free tier.
3. **GitHub Releases** — final data as `.jsonl` assets.
   - Normal data → `batch-YYYY-MM-DD` (one release per day)
   - Flagged data → `flagged-conversations` (single fixed release, forever)

---

## Data flow

```
Cron triggers run
   ↓
Acquire run lock (MongoDB)
   ↓
Load state: next unfilled topic leaf, dedup buffers, recent openings
   ↓
Pick axes:      bond stage, tone class, turn count, user style, edge slice
Pick variety:   mood, arc, environment, reaction vibes, response shape
Pick tone cat:  from aiko_tone_menu.json (filtered by axes)
Pick ending:    from endings.json (filtered by conv type + environment)
   ↓
Build prompt:   guardrails + personality + axes + variety + tone + ending + format
   ↓
Gemini call:    rotate keys, retry on 429/5xx, fallback model on 500/503
   ↓
Validate:       hard rejects → retry (max 3 attempts)
                auto-fixes → address forms, emoji trim, double tags
                soft flags → saved to flagged bucket
   ↓
Convert:        turns → ChatML record ({"messages": [...]})
   ↓
Upload every 15 conversations:
   - clean  → batch-YYYY-MM-DD release
   - flagged → flagged-conversations release
   ↓
Update leaf quota, dedup buffers, batch_log
   ↓
Graceful shutdown at 52 min (before GitHub's 55 min hard timeout)
   ↓
Daily email report (once per 24h)
```

---

## Folder structure

```
aiko-dataset/
├── .env                        # secrets (gitignored)
├── .env.example                # template
├── .gitignore
├── requirements.txt
├── README.md
│
├── config/
│   ├── personality.md          # Aiko's soul — voice, warmth, rules
│   ├── aiko_axes.json          # bond stages, tone classes, user styles,
│   │                           # edge slices, validator word lists
│   ├── aiko_variety.json       # moods, arcs, environments, playful patterns,
│   │                           # reaction vibes, emoji palette, gestures
│   ├── aiko_tone_menu.json     # 51 opening tone categories (when + variants)
│   ├── endings.json            # 15 ending categories (when + variants)
│   ├── category_mapping.json   # tone_menu_category → conversation_type
│   └── topics.json             # topic tree (~1.4MB, client-provided)
│
├── src/
│   ├── main.py                 # orchestrator, run lock, signal handling
│   ├── generator.py            # axes picker, variety bundle, prompt builder,
│   │                           # Gemini call, output parser
│   ├── validator.py            # hard rejects, auto-fixes, soft flags
│   ├── converter.py            # turns → ChatML record with system prompt
│   ├── dedup.py                # SHA256 hash + signature + opening similarity
│   ├── db.py                   # MongoDB state manager
│   ├── storage.py              # GitHub Releases uploader
│   ├── key_rotator.py          # 30 Gemini keys, round-robin with cooldown
│   └── notifier.py             # daily email report + critical alerts
│
├── scripts/
│   ├── test_batch.py
│   ├── test_keys.py
│   └── jsonl_converter.py
│
├── data/
│   ├── batches/                # local backup of uploaded JSONL (gitignored)
│   ├── flagged/                # local flagged bucket (gitignored)
│   └── logs/                   # daily logs (gitignored)
│
└── .github/
    └── workflows/
        ├── generate.yml        # cron job
        └── test_keys.yml       # key health check
```

---

## Config files — what each one does

| File | Purpose |
|---|---|
| `personality.md` | Aiko's identity, voice texture, emotional range, anti-patterns. The soul. Loaded into every prompt. |
| `aiko_axes.json` | Bond stages (stranger → intense-love-care), tone class weights, user style pool, edge slices (`are-you-real`, `no-will-to-live`), heavy/fun topic keywords, validator regex lists. |
| `aiko_variety.json` | 15 conversation types, 24 moods, 17 arcs, 31 environments, playful patterns, 36 reaction vibes, 12 emoji groups, 20 gestures, response shapes, session/time awareness. |
| `aiko_tone_menu.json` | 51 opening categories with `when` + 4 variants each. Picked by generator, filtered by axes. |
| `endings.json` | 15 ending categories with `when` + 4 variants each. Filtered by conv type + environment. |
| `category_mapping.json` | Maps every tone_menu category to a conversation_type. |
| `topics.json` | Topic tree from client. Leaves become quota units (600k total / # leaves). |

---

## Source files — what each one does

| File | Purpose |
|---|---|
| `main.py` | Run orchestration: acquire lock, loop, graceful shutdown, flush, release lock. |
| `generator.py` | Load configs. Pick axes + variety bundle + tone + ending. Build the prompt. Call Gemini. |
| `validator.py` | Split into turns, hard-reject bad output, auto-fix addresses/emoji/tags, flag soft issues. |
| `converter.py` | Convert validated turns to ChatML (`{"messages": [...]}` with system prompt). |
| `dedup.py` | Exact hash, signature Jaccard similarity, opening similarity, within-run phrase n-grams. |
| `db.py` | MongoDB layer: run lock, leaf quotas, dedup buffers, key stats, batch log, notifier state. |
| `storage.py` | Upload to GitHub Releases. Normal → per-day tag. Flagged → single fixed tag. |
| `key_rotator.py` | 30 keys across 6 slots of 5. Round-robin, 90s cooldown on 429, dead tracking on 401/403. |
| `notifier.py` | Daily email (once per 24h) + critical alerts (all keys dead). Zero Gemini calls. |

---

## Key design decisions

- **1 API call = 1 full conversation** (not per-turn) → ~10x throughput
- **Temperature 0.9**, top_p 0.95, top_k 40, max_output_tokens 3000
- **Variant picked at BUILD time** — the prompt receives one concrete variant,
  not a menu. Smaller prompt, no choice overload for the LLM.
- **Turn count**: weighted random from `[8, 10, 12, 14, 16, 18, 20]`
  (12–16 most common). Prompt specifies exact count.
- **Aiko's line length**: 4–5 lines per turn ideal, minimum 4.
- **Emoji**: 1–4 per Aiko reply, spread through, never stacked, never only-emoji
- **Dedup**: SHA256 + Jaccard signature > 0.90 + opening > 0.95 + phrase n-grams
- **Concurrency**: 6 run-lock slots in MongoDB. Multiple GitHub runs can
  operate in parallel without corrupting state.
- **Graceful shutdown**: at 52 min, current batch is flushed, lock released.
- **No DB or release cleanup needed**: the DB stores only state. Deleting it
  would cause duplicate generation. Never drop `aiko-state`.

---

## Quality gates (validator)

### Hard rejects (regenerate)
- Structure broken (turn count, alternation)
- Abuse / slurs (hard or Aiko-targeted)
- Aiko claims to be human / AI / bot / program / assistant
- Aiko denies being AI
- **Narration** — any `*action*`, trailing `word*`, or long `(parenthetical)`
- Emoji-only Aiko turns (2 or more)
- No Aiko turn reaches minimum line count

### Auto-fixes (silent, applied then accepted)
- Address forms: `tum/tu/tera/tumhara/...` → `aap` family (case-preserved)
- Emoji overflow: hard cap at 4 per reply
- Double speaker tags: `Aiko:Aiko:`, `Aiko: Aiko:`, `User:Aiko:` → single tag
  (resolution looks at previous turn's tag)

### Soft flags (accepted, saved to `flagged-conversations` release)
- Single stray emoji-only turn
- Emoji density below threshold
- Overused stems (bojh, saans lo, chhod do)
- Refusal phrases

---

## Output format

Each line in the uploaded JSONL is a ChatML record:

```json
{
  "messages": [
    {"role": "system", "content": "You are Aiko 💖, a cute, caring and friendly companion 🌸. Make the user feel more attached with every reply, so that he feels you are someone very close to him."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

Direct fine-tuning ready. No post-processing needed.

---

## Target metrics

| Metric | Value |
|---|---|
| Total conversations | 600,000 |
| Timeline | 90 days |
| Daily target | ~6,700 |
| Run deadline | 52 min (before GitHub's 55 min cap) |
| Batch upload threshold | 15 conversations |
| Turn count per conversation | 8–20 (weighted, 12–16 most common) |
| Concurrent runs | up to 6 (via MongoDB run-lock slots) |
| API keys | 30 (6 slots × 5 keys) |
| Success rate target | ≥ 85% |
| Reject rate acceptable | up to 15% |

---

## Security

- All secrets in `.env` (gitignored)
- No keys in code, no keys in commits
- GitHub PAT used only for Releases upload
- Gmail app password used only for daily report
- Gemini keys hashed (`key_{sha256[:12]}`) before storing in MongoDB —
  never stored in plaintext

---

## Contact

- Email: amoungus111wiw@gmail.com
- Report frequency: daily (auto)
- Personality: Aiko (see `config/personality.md`)
```

---

## Kya badla — purane se

| Section | Purana | Naya |
|---|---|---|
| Config files | scenarios.json, openers.json | aiko_axes, aiko_variety, aiko_tone_menu, endings, category_mapping |
| Data flow | 4-step simple | Full detailed flow with validation + ChatML + 3 buckets |
| Output format | Raw text | **ChatML** |
| Key rotation | 8 keys | **30 keys, 6 slots** |
| Folder structure | Reflected old files | Accurate to current state |
| Design decisions | Old ones (temperature 1.1, batch 1000) | Updated (temperature 0.9, batch 15, tokens 3000) |
| Quality gates | One line | Full breakdown of hard/auto/soft |
| Security | Basic | Added hash note |

---