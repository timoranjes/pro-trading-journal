# MEMORY.md — Source of Truth (loads into system prompt every session)

## User Profile
- Name: Orange (Discord: Timoranjes, in #inbox). Language: English primary, TC secondary. Style: direct, decisive
- Charlotte Li (wife): separate workspace (#orange-ai-bot, gateway: ai.hermes.gateway-wife)
- Values: zero-cost solutions, streaming over polling, reliable Discord delivery
- Data loss is INTOLERABLE — always save to iCloud + GitHub immediately after major work
- Communication: Discord ack-first (ack within seconds before tool work)
- Chinese wiki tags: MUST use established Traditional Chinese theological terms (聖經, 召會, 生命, etc.)
- Price alerts: wants attribution (why prices moved), timestamps, NO peer comparisons

## Environment Facts
- Model: qwen3.5-plus (Alibaba Coding Plan). Fallback: deepseek-v4-flash
- Delegation: glm-5.1 (VolcEngine Ark). Vision: kimi-k2.5 (Alibaba). Web: Exa. Browser: Camofox localhost:9377
- Memory: Hindsight (cloud mode, hybrid). Compression: 70%. Approvals: smart. Max turns: 60
- Platform: macOS (launchd). Timezone: HKT
- Discord home channel: #inbox

## Architecture Decisions
- Ministry Wiki: Wiki (`~/wiki-ministry-words/`) = navigation + index center ONLY. NotebookLM = full-text backend. Wiki site hard cap: 100MB. Data preservation: iCloud symlink + GitHub immediately after major work.
- Alert System: 16 layers (portfolio alerts, market flows, AI digest, news monitor, volatility, correlation). Extended hours, non-trading day alerts, earnings calendar, analyst ratings.

## Resolved Issues
- **Rate Limit Mitigation (2026-05-08)**: Alibaba 429 from cumulative input >1M tokens. Fixes: Cron jobs staggered (peak 35→19), session archive thresholds lowered (200K→150K, 2h→1h), client-side RPS limiter (2 req/sec), exponential backoff (1s→5s→25s→1min), per-session budget (warn 100K/block 150K), fallback models reduced (28→3 vetted), memory flush/nudge intervals increased (3→5, 5→8), redact_secrets enabled.
- **Subagent Spawning (2026-05-08)**: Sequential inline work for parallelizable tasks. Fix: Decision framework in AGENTS.md — 3+ independent tasks = parallel spawn, same file = inline.
- **Config Cleanup (2026-05-08)**: Merged redundant AGENTS.md sections (Error Tracking + Error Recovery → single section), standardized reasoning_effort to xhigh, reduced fallback chain from 28 unreliable models to 3 vetted (glm-5.1, kimi-k2.5, qwen3.6-plus), AGENTS.md compressed 137→70 lines, SOUL.md compressed 161→113 lines (removed outdated model debugging notes, merged duplicate ambiguity sections, trimmed redundant failed solutions).

## Failed Solutions (Don't Suggest Again)
- NVIDIA embeddings, HuggingFace BGE-M3 — not available
- opencode ACP via delegate_task — process exits early
- Regular `sk-xxxxx` on Alibaba Coding Plan — must use `sk-sp-xxxxx`

## Known Bugs & Patterns
- **qwen3.6-plus 404** on Alibaba CN endpoint — model name wrong for CN. Fix: use qwen3.5-plus
- **Commodity alerts fail silently** on futures — futures data uses `hf_` prefix, not detected by spot logic
- **Market context routing** — `fetch_market_context()` must route by stock market: A-share → 上证指数/创业板指, HK → 恒生指数/恒生科技, US → S&P 500/Nasdaq 100
- **Catalyst priority for LLM attribution** — product/tech breakthroughs > earnings > analyst upgrades > policy > market β > flow data. Don't default to "融资买入"/"北向资金" as catalyst when specific product news exists

## Price Alert Format Rules
- LLM MUST NOT quote news verbatim — summarize catalyst in 1-2 lines
- Market context must match the stock's market (not always US indices)
- Format: 前收→现价, ticker/weight/% + level, volatility, one-line catalyst + source

## Lessons Learned
- Check filesystem FIRST before web search
- execute_code reduces context 60-80% for batch ops vs sequential calls
- Anti-repetition: 3x same error → stop → try alternative
