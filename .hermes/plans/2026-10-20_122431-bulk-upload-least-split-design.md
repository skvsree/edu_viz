# Bulk upload: least splitting per PDF — design (v3, token-first)

> **For Hermes:** design document only. Do not implement until the open questions are answered.

**Goal:** cut the tokens burned per bulk-upload chapter — and the wall clock with them — by
generating from as few PDF text chunks as possible, reusing the provider's prompt cache, and never
paying twice for the same prompt.

**Branch:** `feature/bulk-upload-optimization` (off `main` @ `5f32ed6`, after the streaming memory
fix `c3fe79f`).

**Tech stack:** FastAPI + SQLAlchemy worker (`app/services/job_worker.py`), provider client
`app/services/ai_generation.py`, settings in `app/core/config.py`.

---

## 1. The current path

| Knob | Value | Where |
|---|---|---|
| chunk size | **6,000 chars** (+800 overlap → 5,200-char stride) | `job_worker.py:564` |
| prompt text cap | **12,000 chars** — `sample = text[:12000]`: any chunk bigger than this is silently truncated | `ai_generation.py:294` |
| extraction modes | 3 — `core`, `mechanisms`, `traps` | `job_worker.py:1339` |
| rounds per chunk | 3 | `ai_pass_rounds` |
| items per pass | 6 flashcards + 6 MCQs | `ai_pass_items` |
| passes in flight | 1 (serial) | `ai_pass_concurrency` |
| per-pass read timeout | 60 s | `opencode_request_timeout` |
| completion budget | 4,096 tokens | `opencode_max_tokens` |
| timeout retries | 2 identical sends | `MAX_TIMEOUT_RETRIES` (`job_worker.py:77`) |
| transient retries | 4 identical sends | `MAX_TRANSIENT_RETRIES` (`:74`) |
| 529 / format retries | 5 identical sends | `:70`, `:73` |
| session id | `eduviz-job-{job.id}` — stable per job, which is what enables provider caching | `job_worker.py:1140` |

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

Five multipliers, in order of size:

1. **Three modes, one chunk.** The same 1,500 tokens are paid three times per round.
2. **Prompt layout defeats the prefix cache.** DeepSeek bills a cache-hit input token at
   **$0.003/1M against $0.15/1M on a miss — 50× cheaper** — and caches by shared *prefix*. Our
   prompt is `instructions → mode note → avoid lists → source text`, and the avoid lists change on
   every pass, so the prefix diverges *before* the document and **nothing is reused**. Reordering to
   `instructions → source text → avoid lists` would let passes 2–9 of a chunk reuse the preamble and
   the whole chunk.
3. **Retries re-send the identical prompt.** `_generate_pack_with_retry` (`job_worker.py:497`)
   loops on the same `prompt` string: timeouts get 2 sends, 503s up to 4, 529s/format errors up to
   5. A timed-out pass has **already been billed for its input** — the retry buys nothing but a
   second bill.
4. **Rounds.** Every round re-sends the chunk to get 6 more items per mode. Two rounds of 6+6 cost
   twice the input of one round of 12+12 for the same 24 items.
5. **Chunk boundaries.** Every chunk re-sends the instructions and repeats 800 characters of the
   previous chunk.

And a blind spot: **usage is never recorded.** `ai_generation.py` parses `choices` and drops the
`usage` block, so no run has a token figure — which is why "we take a lot of tokens" cannot be
attributed today.

## 3. Proposed change — measure, then stop paying twice

**T1. Token accounting first (no behaviour change).**
Read `usage` from every provider response — including `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens` when present — accumulate per pass, and persist per file
(`input_tokens`, `output_tokens`, `cache_hit_tokens`; migration) plus a roll-up on the bulk. Log one
line per pass and one summary per file (`tokens_in=… cached=… tokens_out=… retries=… cards=…`).
Show it in the bulk status response. **Nothing else counts as an improvement until this baseline
exists.**

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
stays.

**T4. Fewer chunks — and raise the 12,000-char cap that silently truncates them.**
`ai_chunk_chars: int = 24000`, `ai_chunk_overlap_chars: int = 200`, and a new
`ai_prompt_text_chars: int = 26000` replacing the hard-coded `text[:12000]`. Without that third
change, bigger chunks would be truncated to their first 12k characters — paid for and never read.
Lands with T2 (a bigger chunk makes a timeout dearer).

**T5. A per-file token budget (circuit breaker).**
`ai_token_budget_per_file: int = 150_000` (tune to the T1 baseline). When a file crosses it, stop
generating for that file, mark it failed with the real reason ("token budget exhausted after N
passes, M cards"), and move on. This is the guard against today's worst case: 11 minutes and tens of
thousands of tokens producing **zero** cards.

**T7. Cache-first prompt order (potentially the single biggest lever).**
Rebuild `build_iterative_study_pack_prompt` as `static instructions → source text → variable avoid
lists → output contract`, and move the per-mode sentence after the text. Then all 9 passes of a
chunk share one prefix and should bill mostly at the cache-hit rate. Verify with
`prompt_cache_hit_tokens` from T1; if the endpoint reports no cache fields, the reorder is harmless.

**T8. Schedule bulk generation off-peak.**
DeepSeek peak is **01:00–04:00 and 06:00–10:00 UTC, Mon–Fri** (06:30–09:30 and 11:30–15:30 IST);
everything else, weekends included, bills at half rate. Bulk import is background work: default the
importer and cron windows outside those hours.

**T6. Optional, biggest structural win — merge the modes.**
Send the chunk **once** and ask for all three lenses in a single response, instead of three calls.
Up to 3× fewer input tokens at the cost of a much longer completion — the exact shape the provider
503s on. Only with a small combined ask (e.g. 4+4 per lens) and only after T1 gives a baseline.

**Out of scope:** the importer's per-chapter split of the book PDF (required — it is how a chapter
becomes a deck).

## 4. Expected effect

| | now | after T1–T5, T7–T8 |
|---|---|---|
| chunks per 36k chapter | ~7 | 2 |
| passes | up to 63 | ~12–20 |
| input tokens per chapter (est.) | ~130k + retry waste | ~35–50k nominal, less after caching |
| cache-hit share of input | ~0% (prefix broken by avoid lists) | most passes |
| timeout retries | 2 identical sends each | 1 shrunk send |
| zero-card runs | possible after tens of thousands of tokens | cut off by the budget with an honest reason |
| visibility | none | per-pass and per-file token + cache counts |

Honest caveat: T7's value depends on whether the OpenCode endpoint passes DeepSeek's cache through
and reports it. That is a 4-call experiment, and **it is currently blocked because the QA key is
expired** (see §8).

## 5. Risks

- **12 + 12 may 503** — 36 items is the documented failure, 24 is untested. Behind `ai_pass_items`.
- **A shrunk retry may still time out** — bounded to one attempt, then the pass counts as failed.
- **Bigger chunks raise the cost of a timeout** — mitigated by T2 landing first.
- **Reordering the prompt may change output quality** — mode guidance moves later; A/B the card
  count, not just the tokens.
- **A token budget that is too tight** could cut a productive chapter; start at median × 3.

## 6. Verification (QA first, prod by image pull only)

Baseline with T1 on one fixed chapter (e.g. `gegp108` on QA), then repeat after T3/T4/T7:

- input/output tokens, cache-hit tokens, retries, passes, chunks, wall clock, cards, cards per 10k
  input tokens
- acceptance: **tokens per card down ≥ 40%**, cards within 90% of baseline, wall clock ≤ 60%,
  no pass retried with an unchanged prompt, no unbounded loops
- then Portainer → edu-viz → **Pull and recreate** (no SSH to prod)

## 7. Third-party tools — what exists, and what is worth adopting

Surveyed October 2026. None of these fix our biggest multipliers (§2.1–2.2) — those are prompt
design in our own code — but four categories are genuinely useful.

| Category | Tools | Verdict for us |
|---|---|---|
| LLM gateway / proxy (budgets, retries, spend logs, fallbacks) | LiteLLM Proxy (free, self-hosted; virtual-key budgets, spend logs, Langfuse/OTEL hooks), Bifrost (Go, OSS), Portkey, OpenRouter, LLM Gateway | **Skip for now.** Would hand us T1 + T5 free, but adds a service and a network hop. ~50 lines of `usage` capture is cheaper for one app. Revisit if keys are shared across projects. |
| Structured output (kills format-retry loops) | Provider `response_format`/JSON-schema (cheapest), Instructor (Pydantic + repair), BAML, Outlines (constrained decoding — local models) | **Adopt one.** Our `MAX_AI_FORMAT_RETRIES = 5` loop is exactly what these remove. Native JSON-schema first if the endpoint supports it, else Instructor-style validate-and-repair. |
| PDF parsing / structural extraction | Docling (structure-aware), Marker (best for book-structured documents — good NCERT fit), MinerU, MarkItDown, LlamaParse/Reducto (hosted, paid) | **Consider Marker.** Plain PyMuPDF text carries headers, footers and page numbers as tokens and gives no headings — so chunks cannot follow sections. Structural extraction serves "least split" directly; expect 10–30% fewer junk tokens plus section-aligned chunks. |
| Chunking | Chonkie, semantic-text-splitter, Unstructured `chunk_by_title`, LangChain splitters | **Adopt the idea, not the dependency** — split on detected headings/paragraphs under a char ceiling; that is ~40 lines. |
| Observability | Langfuse, Helicone, OpenLLMetry, LiteLLM spend logs | **Optional.** T1 logging first; adopt Langfuse only for a cross-project dashboard. |
| Provider economics | DeepSeek automatic prefix caching (50× cheaper hits), off-peak half price | **Adopt** — T7 and T8. Free, no dependency. |

## 8. Blocking finding: the QA provider key has expired

A 4-call probe against the configured endpoint returned
`401 authentication_error: api_key_expired — "This API key has expired. Create a new API key in
OpenCode Console"` for `/opt/edu_viz/.env`'s `AI_API_KEY` (67 chars, provider `opencode`, model
`deepseek-v4.1-flash`). So **QA cannot generate at all right now**, and every measurement in §6 is
blocked until a new key is dropped into `.env`.

Notes:
- The probe needs `x-opencode-session` + `User-Agent`; without them the edge answers 403 (Cloudflare
  1010) before auth, which is why the first probe attempt looked like a permissions bug.
- 401 is **not** in `_is_retryable_transient_error`, so an expired key fails fast instead of burning
  retries — prod's 11-minute 0-card failures are therefore a *different* cause and still need the
  `/settings/ai` "Test this configuration" check.

## 9. Open questions (need answers before code)

1. **Mode merge (T6):** collapse the three lenses into one call (up to 3× fewer input tokens, but a
   longer completion that may 503), or keep three calls and take the wins from T3/T4/T7 only?
2. **Volume:** is the ≥280-MCQ floor hard? It decides whether the token budget may cut a chapter
   short or must keep spending until the floor is met.
3. **Retry policy:** `shrink` (halve the chunk once) or `skip` (fail the pass) as the default?
4. **Budget:** is ~150k input tokens per chapter acceptable, or tighter, e.g. 100k?
5. **Parser:** stay on PyMuPDF plain text, or pilot Marker for structural extraction and
   section-aligned chunks?
6. **Key:** issue a new OpenCode key for QA (and confirm prod's key/model in `/settings/ai`), or
   switch QA to the DeepSeek provider with its own key?
