# AIKO Dataset Generator

## Project Goal
Generate 600,000 unique Hinglish conversations with Aiko (a fixed 
personality female companion) for LLM fine-tuning. Target: 90 days.

## Architecture (3-Layer)

1. **GitHub Actions** (public repo, unlimited minutes) 
   → cron job every 5 min
2. **MongoDB Atlas** (512MB free) 
   → only state tracking (topic progress, counters, key usage)
3. **GitHub Releases** 
   → final data as .jsonl batches (2GB per file limit)

## Data Flow
GitHub Actions cron (every 5 min)
↓
Load state from MongoDB (which topic/subtopic is next?)
↓
Pick random scenario seed + opening style
↓
Build prompt with personality.md + topic + scenario
↓
Call Gemini API (8 keys, rotate on rate limit)
↓
Validate response (checklist in personality.md)
↓
If PASS → append to batch file (JSONL)
If FAIL → retry with different seed (max 3)
↓
Every 1000 conversations → upload batch as GitHub Release asset
↓
Update MongoDB state (progress counters)
↓
Every 24 hours → send email report

## Folder Structure
aiko-dataset/
├── .env ← secrets (gitignored)
├── .env.example ← template
├── .gitignore
├── requirements.txt
├── README.md
│
├── config/
│ ├── personality.md ← Aiko (locked)
│ ├── topics.json ← topic tree (from client)
│ ├── scenarios.json ← 200+ situation seeds
│ └── openers.json ← 300+ opening styles
│
├── src/
│ ├── main.py ← orchestrator
│ ├── generator.py ← Gemini API call + prompt builder
│ ├── key_rotator.py ← 8-key rotation with rate-limit handling
│ ├── validator.py ← QA filter (personality checklist)
│ ├── dedup.py ← duplicate detection (hash + semantic)
│ ├── db.py ← MongoDB state manager
│ ├── storage.py ← GitHub Releases
uploader
│ └── notifier.py ← daily email reporter
│
├── scripts/
│ ├── test_batch.py ← 500 test conversations
│ └── jsonl_converter.py ← final .jsonl builder (with system prompt)
│
├── data/
│ ├── batches/ ← JSONL batch files (gitignored)
│ └── logs/ ← daily logs (gitignored)
│
└── .github/
└── workflows/
└── generate.yml ← cron job definition

## Key Design Decisions

- **1 API call = 1 full conversation** (not per-turn) → 10x speed
- **Temperature: 1.1** (high variety)
- **Scenario seed**: random from scenarios.json per call
- **Opening style**: random from openers.json per call
- **Dedup**: SHA256 of conversation + semantic similarity (last 10k)
- **Key rotation**: round-robin, on 429 → next key
- **Auto-pause**: if all keys hit rate limit → sleep 60s
- **Checkpoint**: state in MongoDB, resume from last completed topic
- **Public repo**: unlimited GitHub Actions minutes

## Quality Priority

Client wants **quality > speed**. Validation must be strict. 
Any doubt → reject and regenerate. Never compromise personality.

## Target Metrics

| Metric | Value |
|---|---|
| Total conversations | 600,000 |
| Timeline | 90 days |
| Daily target | 6,700 |
| Batch size | 100 per run |
| Runs per day | ~67 |
| Turns per conversation | 8-24 (aim 16) |
| Success rate target | ≥85% |
| Reject rate acceptable | up to 15% |

## Output Format (per conversation)
{
user: ...

Aiko: ...

user: ...

Aiko: ...
}

Simple text. No metadata. No system prompt. No narrator.
JSONL conversion happens later (separate script).

## Security

- All secrets in `.env` (gitignored)
- No keys in code, no keys in commits
- GitHub token used only for Releases upload
- Email app password used only for daily report

## Client Info

- Contact: amoungus111wiw@gmail.com
- Report frequency: daily
- Personality: Aiko (see config/personality.md)