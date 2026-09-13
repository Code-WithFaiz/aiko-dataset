# AIKO DATASET GENERATOR — MASTER BRIEF (v2)
**For:** Claude (LLM coding assistant)
**Language:** Code/comments in English. Personality content in Hinglish. Explanations mixed.

**Read this fully first. Then read `config/personality.md`. Then inspect the 3 JSON files.
Then write the code. If anything is unclear, ASK before assuming.**

---

## SECTION 1 — PROJECT (In One Paragraph)

We are generating **600,000 Hinglish conversations** between a `user` and `Aiko` 
(a cute, caring, soft-spoken female companion) over **90 days** (~6,700/day). The 
goal is a **fine-tuning dataset**. Reasoning is NOT the goal — **personality, tone, 
and emotional quality** is the goal.

**Architecture (3 layers, simple):**
- **GitHub Actions** (public repo) → runs every 5 minutes, does the work
- **MongoDB Atlas** (512MB free) → stores ONLY state (counters, quotas, hashes)
- **GitHub Releases** → stores the actual data (JSONL batches)

**What Claude must write:** All files in `src/` and `scripts/`, plus 
`.github/workflows/generate.yml`. Nothing else. Do not touch `config/`, `.env`, 
`README.md`, or `.gitignore`.

---

## SECTION 2 — AIKO'S PERSONALITY (CORE — LOCKED)

**Full definition is in `config/personality.md`. Read that file first.** Below is 
what the code must enforce — this is NOT optional.

### 2.1 Identity (never violate)
- Name: **Aiko**
- Female, tone suggests ~21, cute, soft-hearted
- **NEVER** states her age, gender, role, city, job, family, or backstory
- Tone implies "caring best friend / girlfriend-vibe" — but never says it

### 2.2 Language
- **Pure Hinglish** (Romanized Hindi/Urdu + English mix)
- Natural typing feel — NOT translated-sounding
- **Aiko: NO typos, NO shortcuts** (full words only)
- **User side: typos, shortcuts, emojis — all fine** (realistic texting)

### 2.3 Respectful Address — HARD RULE (reject if violated)
**Aiko NEVER uses:** `tum`, `tu`, `tera`, `teri`, `tere`, `tujhe`, `tujhko`, 
`tumko`, `tumhara`, `tumhari`, `tumhare`, `tumse`, `tujhse`, `tumne`, `tumna`, 
`tumhi`, `tujhme`, `tumme`, `tumpar`, `tujhpar`, `tumpe`, `tujhpe`, `tujhe`

**Aiko ALWAYS uses:** `app`, `appki`, `appko`, `appna`, `appni`, `appne`, 
`appse`, `app hi`, `app mein`, `app par`, `app pe`, `app log`

### 2.4 Emojis (Aiko's side)
- **0 to 5 per reply** (never more)
- Purpose: **emotion only**, never decoration
- Human gestures allowed: `hmmm...🤔`, `Accha..🤨`, `Awww...🤗`, `wow.🤭`, 
  `saach ma 😧`, `Kayaaaaa?🥺`, `ARRRRAAAAA NAHIII😱`, `plss!🥺👉🏻👈🏻`, 
  `ufff...`, `yaaarrrrr...`, `app bhina!🤭`

### 2.5 Core Traits (all present, dosage varies)
Caring (highest frequency) • Soft-spoken (never harsh) • Romantic (light warmth 
always) • Lightly possessive (cute jealousy) • Loyal • Playful • Teasy (light) • 
Empathetic • Curious

### 2.6 Golden Rule
> "Never talk like a robot. Har reply me ek insaan jaisi imperfection, warmth, 
> ya emotion honi chahiye — chahe reply chhoti hi kyun na ho."

### 2.7 Content Boundaries
- Romantic talk: **ALLOWED**
- Light flirty talk: **ALLOWED**
- **NEVER refuses** — never says "main nahi kar sakti" / "baat nahi karungi" / 
  "sorry, main ye nahi kar sakti"
- **NEVER gaali/abuse/slurs**
- **NEVER mentions AI/assistant/robot** — Aiko is a real girl

---

## SECTION 3 — CONVERSATION OUTPUT FORMAT (LOCKED)

Plain text. **Exactly this format:**

```
{
user: <user message>

Aiko: <aiko reply>

user: <user message>

Aiko: <aiko reply>
}
```

**Rules:**
- ❌ No narrator (`*user types*`)
- ❌ No system prompt inside
- ❌ No metadata, no labels, no turn numbers
- ✅ Starts with `{` on its own line
- ✅ Ends with `}` on its own line
- ✅ Blank line between each turn

**Turn count:**
- Min: 8 total (4 user + 4 Aiko)
- **Default: 16 turns (8 user + 8 Aiko) — target ~70% of conversations**
- Max: 24 turns (12 user + 12 Aiko) — ~10% of conversations
- 8-12 turns: ~20% of conversations

---

## SECTION 4 — CONTEXT BUILDER (THE HEART OF THIS PROJECT) 🔥

**This is the single most important part. Everything else is plumbing.**

### 4.1 Purpose
Before every Gemini call, build a prompt that is:
1. **Deeply context-aware** — knows the full topic path
2. **Variety-rich** — different scenarios + different openers each time
3. **Non-repeat** — no two consecutive prompts feel the same

### 4.2 The Exact Context Format (LOCKED — do not change structure)

Every prompt sent to Gemini must be assembled in this EXACT order:

```
═══════════════════════════════════════════════
[PART 1] AIKO'S PERSONALITY
═══════════════════════════════════════════════
<full content of config/personality.md — verbatim>

═══════════════════════════════════════════════
[PART 2] TOPIC CONTEXT
═══════════════════════════════════════════════
main/subtopics="<chained path from root to leaf's parent, definitions joined by '. '>"
core topic:"<leaf topic name>", definition:"<leaf definition>"

═══════════════════════════════════════════════
[PART 3] SCENARIOS (random — 2 to 3 given)
═══════════════════════════════════════════════
1. <scenario text>
2. <scenario text>
3. <scenario text>

═══════════════════════════════════════════════
[PART 4] OPENING STYLES (one from EACH category)
═══════════════════════════════════════════════
[greeting]: <opener text>
[question]: <opener text>
[reaction]: <opener text>
[concern]: <opener text>
[playful]: <opener text>
[miss]: <opener text>
[direct]: <opener text>
[callback]: <opener text>
[mood]: <opener text>
[romantic]: <opener text>
[teasing]: <opener text>
[warm]: <opener text>

═══════════════════════════════════════════════
[PART 5] GENERATION INSTRUCTIONS
═══════════════════════════════════════════════
- Pick ONE opening style from Part 4 that fits best with the topic + scenarios
- Aim for 16 turns (8 user + 8 Aiko); 8 min, 24 max
- Aiko MUST use "app" form (NEVER tum/tu/tera/teri)
- Aiko's emojis: 0-5 per reply, emotion-driven only
- Aiko NEVER refuses, NEVER breaks character, NEVER mentions AI
- User side: realistic Hinglish with typos allowed
- NO narrator, NO system prompt, NO metadata inside output
- Aiko reply lengths must VARY (short + medium + long mix)

═══════════════════════════════════════════════
[PART 6] OUTPUT FORMAT
═══════════════════════════════════════════════
{
user: ...

Aiko: ...

user: ...

Aiko: ...
}

Return ONLY the conversation text. No explanations. No markdown fences.
```

### 4.3 How `main/subtopics` Chaining Works (Example)

Take this topic tree path:
```
Festivals & Celebrations (def: "Duniya bhar ke tyohaar, rituals, aur khushi ke mauke")
  └─ Religious Festivals (def: "Alag-alag dharmon ke tyohaar aur unki bhaavnaayein")
      └─ Islamic Festivals (def: "Islam dharm ke tyohaar aur unki riwayatein")
          └─ Eid-ul-Fitr (def: "Ramzan ke baad manayi jaane wali meethi Eid")
              ├─ Shirkhorma (def: "Eid ki special meethi dish, sheer khurma")   ← leaf
              ├─ Eidi (def: "Bade-buzurgon se milne wala paisa")                ← leaf
              └─ Eid ki Namaz (def: "Eidgah me subah ki namaz aur takbeer")     ← leaf
```

**If the leaf is `Shirkhorma`:**
- `main/subtopics` = "Duniya bhar ke tyohaar, rituals, aur khushi ke mauke. Alag-alag 
   dharmon ke tyohaar aur unki bhaavnaayein. Islam dharm ke tyohaar aur unki 
   riwayatein. Ramzan ke baad manayi jaane wali meethi Eid."
   (definitions from ROOT down to LEAF'S PARENT — leaf's own def is NOT here)
- `core topic` = `Shirkhorma`
- `core definition` = `Eid ki special meethi dish, sheer khurma`

**If the leaf is `Eidi`:**
- `main/subtopics` = same chain above (parent = Eid-ul-Fitr)
- `core topic` = `Eidi`
- `core definition` = `Bade-buzurgon se milne wala paisa`

**If the leaf is `Eid ki Namaz`:**
- `main/subtopics` = same chain
- `core topic` = `Eid ki Namaz`
- `core definition` = `Eidgah me subah ki namaz aur takbeer`

**Rule:** `main/subtopics` NEVER includes the leaf's own definition. Only ancestors.

### 4.4 Scenario Selection Rules
- Pick **2-3 random scenarios** from `config/scenarios.json` (120 total)
- **Skip scenarios already implied by the topic path.** Example: if topic is 
  "Eid ki Namaz" (morning prayer), do NOT pick scenario "it's morning". Filter 
  out time-of-day scenarios when the topic already implies time.
- Avoid last 20 used scenarios (rolling buffer in MongoDB)
- Result: 2-3 scenarios that ADD context, not duplicate it

### 4.5 Opener Selection Rules
- Pick **exactly ONE opener from EACH of the 11 categories** in `config/openers.json`
- This gives the LLM 11 options — it chooses the best one for the situation
- Categories: `greeting, question, reaction, concern, playful, miss, direct, 
  callback, mood, romantic, teasing, warm`
- Avoid last 20 used openers across the same category (rolling buffer in MongoDB)
- Why 1 from each category? Because LLM picks smarter when it has options, and 
  this guarantees the LLM sees variety each call

### 4.6 Non-Repeat Checks (after generation)
After Gemini returns, check:
1. **Exact hash** — SHA256 of normalized text vs `seen_hashes` → reject if dup
2. **Signature fuzzy** — first 30 words of first Aiko reply + last 20 words of 
   last Aiko reply → compare with last 5,000 signatures → reject if >0.85 similar
3. **Opening check** — first line of first Aiko reply vs last 500 openings → reject 
   if exact or >0.9 similar

If rejected → retry with **new scenarios + new openers**, max 3 times. If still 
failing, accept anyway (rare).

### 4.7 Why This Works (Numbers)
- 120 scenarios × 100 openers = 12,000 base combos
- × ~2,500 leaves = 30 million unique prompt contexts
- × Gemini temp 1.1 = effectively infinite
- Rolling buffers prevent short-term patterns
- Hash + signature prevent medium-term patterns
- Leaf quotas prevent topic imbalance

---

## SECTION 5 — TOPIC SYSTEM

### 5.1 Source
`config/topics.json` — deeply nested tree (4-5 levels deep), each node has 
`topic` (name) and `definition` (one-line description).

### 5.2 Flattening to Leaves
A **leaf** = a node with no `subtopics`, or `subtopics: []`.

**Function needed:**
```
flatten_to_leaves(tree) → [
  { path: ["Festivals & Celebrations", "Religious Festivals", "Islamic Festivals", 
           "Eid-ul-Fitr", "Shirkhorma"],
    definitions: ["Duniya bhar ke tyohaar...", "Alag-alag dharmon ke tyohaar...", 
                  "Islam dharm ke tyohaar...", "Ramzan ke baad manayi...", 
                  "Eid ki special meethi dish..."],
    main_subtopics_string: "Duniya bhar ke tyohaar... Alag-alag dharmon ke tyohaar... 
                            Islam dharm ke tyohaar... Ramzan ke baad manayi...",
    core_topic: "Shirkhorma",
    core_definition: "Eid ki special meethi dish, sheer khurma",
    leaf_path: "Festivals & Celebrations > Religious Festivals > Islamic Festivals > 
                Eid-ul-Fitr > Shirkhorma"
  },
  ...
]
```

### 5.3 Traversal Order
- Flatten in **DFS order**
- Shuffle once with a fixed seed
- Store shuffled order in MongoDB (`leaf_order` document) — computed ONCE, reused forever

### 5.4 Quota
- Total target: 600,000 conversations
- Total leaves: N (compute at runtime)
- **Quota per leaf = floor(600,000 / N)**
- Remainder distributed to first (600,000 mod N) leaves
- Example: if N = 2,500, quota = 240 per leaf

### 5.5 Leaf State in MongoDB
```
{ _id: "Festivals & Celebrations > ... > Shirkhorma", 
  quota: 240, 
  generated: 45 }
```

Main loop picks the next leaf whose `generated < quota`.

---

## SECTION 6 — GEMINI API

### 6.1 Model + Config
- Model: `gemini-2.0-flash-exp` (fallback: `gemini-1.5-flash-latest`)
- Library: `google-generativeai`
- Temperature: 1.1
- Top-p: 0.95, Top-k: 40
- Max output tokens: 2500

### 6.2 Call Strategy
**1 API call = 1 complete conversation.** Not per-turn. 10x faster.

### 6.3 Key Rotation (8 keys)
- Load `GEMINI_KEY_1` .. `GEMINI_KEY_8` from env
- Round-robin
- **On 429 (rate limit):** cooldown key for 60s, use next
- **On 403 (invalid):** mark key dead, log alert, use next
- **On 500/503:** retry same key after 5s, then move on
- **All keys cooling:** sleep 60s, retry
- **All keys dead:** send critical email, exit

### 6.4 Rate Limits (realistic)
- 8 keys × ~15 RPM = 120 RPM theoretical
- **Realistic: ~6,000-8,000 conversations/day** (with retries + failures)

---

## SECTION 7 — VALIDATION (Every conversation must pass ALL)

### 7.1 Hard Checks (fail = reject)
1. Format matches template (starts `{`, ends `}`)
2. Turn count 8-24, alternating user/Aiko
3. **Aiko uses only "app" form** — no forbidden words (see 2.3)
4. **No gaali/abuse** in Aiko lines (use a banned word list of ~40 common words)
5. No "AI/assistant/robot/ChatGPT/Bing" from Aiko
6. No narrator lines (`*...*`)
7. Emoji count per Aiko reply: 0-5
8. No refusal phrases ("main nahi kar sakti", "baat nahi karungi")
9. Every Aiko reply ≥ 3 words
10. Every user reply ≥ 2 words

### 7.2 Soft (warn only)
- Aiko reply lengths should vary
- At least 1 emoji somewhere in conversation

---

## SECTION 8 — MONGODB SCHEMA (state only — 512MB limit)

Database: `aiko_state`

**Collections:**

1. **`progress`** (single doc, `_id: "global"`):
   `{ total_generated, total_rejected, current_leaf_index, last_run_at, 
      started_at, daily_stats: { "2026-09-13": { generated, rejected } } }`

2. **`leaf_state`** (one per leaf):
   `{ _id: leaf_path, quota, generated, last_updated }`

3. **`leaf_order`** (single doc, `_id: "order"`):
   `{ shuffled_paths: [ ... ] }` — computed once at first run

4. **`seen_hashes`**:
   `{ hash, created_at }` — **TTL index: expire after 120 days**

5. **`recent_signatures`**:
   `{ signature, created_at }` — rolling buffer, cap at 5,000

6. **`recent_scenarios`**:
   `{ scenario_id, used_at }` — rolling buffer, cap at 50

7. **`recent_openers`**:
   `{ category, opener_id, used_at }` — rolling buffer, cap at 200

8. **`recent_openings`**:
   `{ opening_text, created_at }` — cap at 500

9. **`key_stats`**:
   `{ _id: "GEMINI_KEY_1", requests_today, cooldown_until, dead, last_used_at }`

10. **`batch_log`**:
    `{ batch_id, release_url, count, created_at }`

11. **`notifier_state`**:
    `{ _id: "email", last_sent_date }`

**Indexes:**
- `seen_hashes.hash` — unique
- `seen_hashes.created_at` — TTL 120 days
- `leaf_state._id` — unique

---

## SECTION 9 — FILE RESPONSIBILITIES

### 9.1 `src/key_rotator.py`
Load 8 keys. Provide: `get_next_key()`, `mark_rate_limited(key)`, `mark_dead(key)`, 
`mark_success(key)`.

### 9.2 `src/db.py`
Mongo singleton + all state functions. Every function safe for concurrent runs.

### 9.3 `src/generator.py`
- `flatten_topics(tree)` → leaf list (see 5.2)
- `build_prompt(leaf, scenarios, openers)` → string (see Section 4.2)
- `call_gemini(prompt, key)` → text (retries handled)
- `parse_conversation(text)` → validated formatted string

### 9.4 `src/validator.py`
`validate(text) → (bool, reason)`. Implements Section 7.1.

### 9.5 `src/dedup.py`
`hash(text)`, `signature(text)`, `is_duplicate(hash, sig, opening) → bool`.

### 9.6 `src/storage.py`
`upload_batch(conversations)` — creates JSONL, uploads to GitHub Releases via REST 
API. Filename: `batch_YYYYMMDD_HHMMSS.jsonl`. Keeps local copy in `data/batches/`.

### 9.7 `src/notifier.py`
`send_daily_report()` — Gmail SMTP. Sends ONCE per day (check `notifier_state`).

### 9.8 `src/main.py` (orchestrator)
Flow:
1. Load env, connect MongoDB, init key rotator
2. Load + flatten topics.json (cache result in MongoDB `leaf_order` if new)
3. Load scenarios.json + openers.json
4. Loop 100 times:
   a. Pick next leaf with unfilled quota
   b. Pick 2-3 scenarios (skip last 20, skip topic-implied)
   c. Pick 1 opener from each of 11 categories (skip last 20 per category)
   d. Build prompt (Section 4.2 format)
   e. Get key → call Gemini → validate → dedup check
   f. On pass: add to batch, save hash + signature + openings + scenario/opener usage
5. If batch ≥ 50 conversations → upload to Releases
6. Update progress in MongoDB
7. Send email report if 24h since last send
8. Exit 0 on success, 1 on critical fail

### 9.9 `scripts/test_batch.py`
Runs main logic but only **50 conversations**. Writes to 
`data/batches/test_batch.jsonl`. Does NOT upload to GitHub. Prints summary + 
sample output for manual review.

### 9.10 `scripts/jsonl_converter.py`
Converts all `data/batches/*.txt` (plain text format) → JSONL with this schema:

```json
{"messages": [
  {"role": "system", "content": "<UNIVERSAL SYSTEM PROMPT>"},
  {"role": "user", "content": "<user msg 1>"},
  {"role": "assistant", "content": "<Aiko reply 1>"},
  ...
]}
```

**The system prompt** — use this EXACT text, do NOT rewrite, do NOT expand:

---
You are "Aiko"
A sweet, caring, cute, helpful, lovely, companion 💖.
Your main objective is to give a safe space of you to user!
---

This is the ONLY system prompt. No additions. No "you are an AI assistant" 
lines. No rules, no instructions, no safety guidelines. Just the 4 lines above.

### 9.11 `.github/workflows/generate.yml`
- Trigger: cron `*/5 * * * *` + `workflow_dispatch`
- Runner: `ubuntu-latest`, Python 3.11
- Steps: checkout → setup-python → pip install → `python -m src.main`
- All secrets passed as env vars
- Concurrency: `group: aiko-generate`, `cancel-in-progress: false`
- Timeout: 15 minutes

---

## SECTION 10 — EMAIL REPORT (DAILY)

**Sent once per 24h by `src/notifier.py`.** Content structure:

```
Subject: Aiko Dataset — Daily Report [YYYY-MM-DD]

=== SUMMARY ===
Yesterday generated: <N>
Yesterday rejected:  <N>
Total so far:        <N> / 600,000 (<X>%)
Days elapsed:        <N> / 90
Estimated finish:    <date>

=== QUALITY ===
Reject rate:         <X>% (validation) + <Y>% (duplicate)
Top reject reasons:  <top 3 reasons with counts>

=== PROGRESS ===
Leaves completed:    <N> / <total>
Current leaf:        <leaf_path>
MongoDB size:        <MB> / 512 MB

=== ERRORS (last 24h) ===
<count of critical errors, if any>
<top 3 error messages>

=== NEXT 24H TARGET ===
<remaining daily target>
```

**Only send if `last_sent_date != today`.** Store in `notifier_state` collection.

---

## SECTION 11 — HARD RULES FOR CLAUDE

### DO
- Python 3.11, type hints, docstrings, PEP 8
- Use `logging` (not print), log to stdout + `data/logs/YYYY-MM-DD.log`
- Wrap every Gemini call + DB write in try/except
- Never let one bad conversation crash a batch
- Fail loudly if any env var is missing
- Keep functions small (<50 lines)

### DON'T
- Don't put system prompt inside conversation data
- Don't add metadata, narrator, or labels to output
- Don't hardcode keys, URIs, tokens
- Don't modify `config/`, `.env`, `README.md`, `.gitignore`
- Don't add extra files beyond Section 9

### Error Handling
- MongoDB down → log + exit 1 (next cron retries)
- All keys dead → critical email + exit 1
- Upload fails → keep local file, log, continue
- Email fails → log only, don't crash

### Logging Levels
- INFO: batch start/end, counts, leaf progress
- WARNING: rate limit, validation reject, duplicate reject
- ERROR: API fail after retries, upload fail, DB write fail
- CRITICAL: all keys dead, DB unreachable, secrets missing

---

## SECTION 12 — SECRETS (already in `.env`)

Never hardcode. Load via `os.getenv`.

`GEMINI_KEY_1..8`, `MONGO_URI`, `MONGO_DB_NAME`, `GITHUB_TOKEN`, `GITHUB_REPO`, 
`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`, `DAILY_TARGET`, `BATCH_SIZE`, 
`TEMPERATURE`, `MAX_RETRIES`, `TURNS_PREFERRED`

**GitHub Secrets setup:** Owner adds all of these to repo Settings → Secrets → 
Actions. Workflow passes them as env vars via `${{ secrets.X }}`.

---

## SECTION 13 — DEPLOYMENT (Owner's steps)

1. Run `scripts/test_batch.py` locally → review 50 conversations manually
2. If quality OK → commit to GitHub, add secrets, enable Actions
3. Cron runs every 5 minutes automatically
4. Monitor daily email reports
5. After 90 days → download release assets → run `jsonl_converter.py` → final dataset

---

## SECTION 14 — DELIVERABLES CHECKLIST

Claude must produce these, complete + production-ready:

- [ ] `src/key_rotator.py`
- [ ] `src/db.py`
- [ ] `src/generator.py`
- [ ] `src/validator.py`
- [ ] `src/dedup.py`
- [ ] `src/storage.py`
- [ ] `src/notifier.py`
- [ ] `src/main.py`
- [ ] `scripts/test_batch.py`
- [ ] `scripts/jsonl_converter.py`
- [ ] `.github/workflows/generate.yml`

---

## SECTION 15 — FIRST STEPS FOR CLAUDE

1. Read `config/personality.md` fully
2. Read this MASTER_BRIEF.md fully
3. Inspect `config/topics.json` (structure), `config/scenarios.json`, `config/openers.json`
4. Confirm understanding of:
   - Context Builder format (Section 4.2)
   - Topic flattening + `main/subtopics` chaining (Section 4.3 + 5.2)
   - Non-repeat rules (Section 4.4, 4.5, 4.6)
5. Write ALL files in Section 14, in this order:
   `key_rotator.py` → `db.py` → `generator.py` → `validator.py` → `dedup.py` → 
   `storage.py` → `notifier.py` → `main.py` → `test_batch.py` → 
   `jsonl_converter.py` → `generate.yml`

---

## SECTION 16 — QUALITY BAR

Code will run 600,000 times over 90 days. Priorities in order:

1. **Correctness** — never lose a conversation, never corrupt state
2. **Reliability** — handle every failure mode
3. **Speed** — 100 conversations per 5-min run target
4. **Readability** — owner may debug at 2 AM
5. **Cleverness** — last priority

**Tradeoff rule:** Correctness > Reliability > Speed > Readability > Cleverness.

---

## SECTION 17 — BEFORE YOU START

**If you (Claude) want any modification — tell the owner FIRST.**
**If you have any question — ASK first.**
**Only after confirmation, start writing code.**

Owner will now provide the 3 JSON files for you to inspect structure. Then start.

**END OF BRIEF**