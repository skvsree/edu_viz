# Bulk upload: least splitting per PDF — design (v2, token-first)

> **For Hermes:** design document only. Do not implement until the open questions are answered.

**Goal:** cut the tokens burned per bulk-upload chapter — and the wall clock with them — by
generating from as few PDF text chunks as possible and never paying twice for the same prompt.

**Branch:** `feature/bulk-upload-optimization` (off `main` @ `5f32ed6`, after the streaming memory
fix `c3fe79f`).

**Tech stack:** FastAPI + SQLAlchemy worker (`app/services/job_worker.py`), provider client
`app/services/ai_generation.py`, settings in `app/core/config.py`.

---

## 1. The current path

| Knob | Value | Where |
|---|---|---|
| chunk size | **6,000 chars** (+800 overlap → 5,200-char stride) | `job_worker.py:564` |
| extraction modes | 3 — `core`, `mechanisms`, `traps` | `job_worker.py:1339` |
| rounds per chunk | 3 | `ai_pass_rounds` |
| items per pass | 6 flashcards + 6 MCQs | `ai_pass_items` |
| passes in flight | 1 (serial) | `ai_pass_concurrency` |
| per-pass read timeout | 60 s | `opencode_request_timeout` |
| completion budget | 4,096 tokens | `opencode_max_tokens` |
| timeout retries | 2 identical sends | `MAX_TIMEOUT_RETRIES` (`job_worker.py:77`) |
| transient retries | 4 identical sends | `MAX_TRANSIENT_RETRIES` (`:74`) |
| 529 / format retries | 5 identical sends | `:70`, `:73` |

Per chunk: 3 modes × up to 3 rounds = 9 passes, each sending **the same chunk text** with a
different lens and only the last 15 covered items for context. A ~36,000-character chapter is ~7
chunks → up to **63 passes**, which matches the observed 321–586 cards per Class VII chapter.

The config comments also record the provider's measured limits, which explain the current shape:
1 pass in flight never failed while 3 lost one in three and 6 lost one in two; 18 + 18 items in one
call gets empty-bodied 503s where 6 + 6 returns in under 10 s; a healthy pass takes 10–40 s, and the
60 s timeout replaced a 180 s one because waiting that long per hang dominated a run.

## 2. Where the tokens go

Estimates (English ≈ 4 chars/token) — **replace with measurement before optimising further**:

- chunk text 6,000 chars ≈ **1,500 tokens**, instructions ≈ 400, covered list ≈ 200 →
  ~**2,100 input tokens per pass**
- 3 modes re-send that same chunk → **3× per round**, up to 3 rounds → **~19k input per chunk**
- 7 chunks → **~130k input tokens for one chapter**
- output: 12 items ≈ 500–800 tokens → 63 passes ≈ **32–50k output tokens**

Four multipliers, in order of size:

1. **Three modes, one chunk.** The same 1,500 tokens are paid three times per round. This is the
   single biggest multiplier.
2. **Retries re-send the identical prompt.** `_generate_pack_with_retry` (`job_worker.py:497`)
   loops on the same `prompt` string: timeouts get 2 sends, 503s up to 4, 529s/format errors up to
   5. A timed-out pass has **already been billed for its input** — the retry buys nothing but a
   second bill. With 29 timeouts on one chapter, retries add **roughly half a chapter's input again**.
3. **Rounds.** Every round re-sends the chunk to get 6 more items per mode. Two rounds of 6+6 cost
   twice the input of one round of 12+12 for the same 24 items.
4. **Chunk boundaries.** Every chunk re-sends the instructions and repeats 800 characters of the
   previous chunk.

And a blind spot: **usage is never recorded.** `ai_generation.py` parses `choices` and drops the
`usage` block, so no run has a token figure — which is why "we take a lot of tokens" cannot be
attributed today.

## 3. Proposed change — measure, then stop paying twice

**T1. Token accounting first (no behaviour change).**
Read `usage.prompt_tokens` / `completion_tokens` / `total_tokens` from every provider response,
accumulate per pass, and persist per file (`input_tokens`, `output_tokens` on
`BulkAIUploadFile`; migration) plus a roll-up on the bulk. Log one line per pass and one summary
per file (`tokens_in=… tokens_out=… retries=… cards=…`). Show it in the bulk status response so the
UI can display cost. **Nothing else ships until this baseline exists** — otherwise we cannot prove
any of T2–T5 helped.

**T2. Never retry an identical prompt.**
Add `ai_timeout_retry_policy: str = "shrink"`:

- `shrink` (default): on a read timeout, retry **once** with the chunk halved at a paragraph
  boundary and `pass_items` reduced (6+6). Smaller prompt, real chance of success, no third send.
- `skip`: record the pass as failed and let the chunk's other modes carry the coverage.
- Timeouts stop counting as transient errors, so one timeout can never cost 2 + 4 sends.

Transient 503s keep their retries (the provider rejects those before generating, so they are cheap),
but the attempt count drops to 2 and the backoff stays.

**T3. Bigger asks instead of more rounds.**
`ai_pass_items` 6 → **12** (24 items per call; the documented failure is 36 items, so 24 is
untested but plausible). One round of 12+12 replaces two rounds of 6+6 at **half the input tokens
for the same output**. Keep `ai_pass_rounds` at 3 as a ceiling; the early-stop on an empty round
stays, so typical runs settle at 1–2 rounds.

**T4. Fewer chunks.**
`ai_chunk_chars: int = 24000`, `ai_chunk_overlap_chars: int = 200` (from 800). A 36k chapter goes
from ~7 chunks to 2; a 12–20k chapter becomes one. Saves the per-chunk instruction preamble and the
overlap re-send. Note the interaction: a bigger chunk makes a *timeout* more expensive, which is why
T2 must land with or before T4.

**T5. A per-file token budget (circuit breaker).**
`ai_token_budget_per_file: int = 150_000` (tune to the T1 baseline). When a file crosses it, stop
generating for that file, mark it failed with the real reason ("token budget exhausted after N
passes, M cards"), and move on. This is the guard against today's worst case: 11 minutes and tens of
thousands of tokens producing **zero** cards.

**T6. Optional, biggest win — merge the modes.**
Send the chunk **once** and ask for all three lenses in a single response (core + mechanisms +
traps) instead of three calls. That removes multiplier #1 (up to 3× fewer input tokens) at the cost
of a much longer completion — the exact shape the provider 503s on. Only worth attempting with a
small combined ask (e.g. 4+4 per lens) and only after T1 gives a baseline. Left as an open question
rather than a default.

**Out of scope:** the importer's per-chapter split of the book PDF (required — it is how a chapter
becomes a deck), and prompt caching (the provider exposes no cache controls).

## 4. Expected effect

| | now | after T1–T5 |
|---|---|---|
| chunks per 36k chapter | ~7 | 2 |
| passes | up to 63 | ~12–20 (12 items/pass, early stop) |
| input tokens per chapter (est.) | ~130k + retry waste | ~35–50k |
| timeout retries | 2 identical sends each | 1 shrunk send |
| zero-card runs | possible after tens of thousands of tokens | cut off by the budget with an honest reason |
| visibility | none | per-pass and per-file token counts |

Honest caveat: the input estimate hinges on T1's measurement. If a pass really is ~2.1k input
tokens, T3 alone halves the bill; if the instruction preamble dominates, T4 matters more.

## 5. Risks

- **12 + 12 may 503** — 36 items is the documented failure, 24 is untested. Behind `ai_pass_items`
  so it reverts from the environment.
- **A shrunk retry may still time out** — bounded to one attempt, then the pass counts as failed
  rather than looping.
- **Bigger chunks raise the cost of a timeout** — mitigated by T2 landing first.
- **A token budget that is too tight** could cut a productive chapter; start at the measured median
  × 3 and keep it a setting.

## 6. Verification (QA first, prod by image pull only)

Baseline with T1 on one fixed chapter (e.g. `gegp108` on QA), then repeat after T3–T4:

- input/output tokens, retries, passes, chunks, wall clock, cards created, cards per 10k input tokens
- acceptance: **tokens per card down ≥ 40%**, cards within 90% of baseline, wall clock ≤ 60% of
  baseline, no pass retried with an unchanged prompt, zero unbounded loops
- then Portainer → edu-viz → **Pull and recreate** (no SSH to prod)

## 7. Open questions (need answers before code)

1. **Mode merge (T6):** collapse the three lenses into one call (up to 3× fewer input tokens, but a
   longer completion that may 503), or keep three calls and take the 2× from T3/T4 only?
2. **Volume:** is the ≥280-MCQ floor hard? It decides whether the token budget may cut a chapter
   short or must keep spending until the floor is met.
3. **Retry policy:** `shrink` (halve the chunk once) or `skip` (fail the pass) as the default on
   timeout?
4. **Budget:** is ~150k input tokens per chapter an acceptable ceiling, or tighter, e.g. 100k?
5. **A/B control:** `gegp108` on QA, starting at 12,000-char chunks before 24,000?
