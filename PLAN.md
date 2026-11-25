# HubSpot MCP server with governed tool access

## Context

A governed way to give an AI agent access to a CRM. Scope here is the MCP server as a complete deliverable, with Claude Desktop as the agent and six tools including paid research on Opus 5.5 (your decision). The runner, rubric and timed metrics are Phase 2. This version splits the plan into 10 features, each tested separately by a system-design tester.

**Claim to prove:** through this server, even a fully compromised model cannot change the CRM without a person, write a forbidden field, exceed a hard limit, write text to Slack that no person approved, or run a tool unlogged.

| Document | Here | Why |
|---|---|---|
| yes/no in the terminal | write tools queue proposals; a separate `approve` command executes | a stdio server's terminal carries the protocol |
| private app token | Service Keys | new private apps were disabled 26 Oct 2026 |
| one token | read-only key for the server, write key for `approve` | the server can't write even if it is buggy |
| confidence rule in the agent | enforced in the server | a prompt rule can be skipped |
| 20 writes per run | 20 per day, persisted | a restart would reset "per run" |
| model writes the Slack text | server builds it | the only unapproved outside action |
| append-only JSONL | plus hash chain and redaction | plain JSONL can be edited silently |

**Trust boundary.** Model, CRM text and web text are untrusted; you are trusted. The guarantee covers changes made **through this server**. A shell, file access, a HubSpot or Slack connector, Claude in Chrome or computer use would bypass it, so claims and live tests use a Claude Desktop chat with all of those disabled; setup has you list enabled tools and stops if any remain. Requiring a terminal for `approve` is a speed bump, not a boundary.

## Architecture and shared rules

```
Claude Desktop --stdio--> server.py (one call_tool handler = guard)
                             |-> policy  |-> audit  |-> store (SQLite)
                             |-> hubspot (read key) |-> research (Claude) |-> slack
you --terminal--> approve.py (write key) -> policy, audit, store, hubspot
```

- **S1 Call order:** size check -> policy -> strict validation -> reserve caps (SQLite, committed) -> audit decision -> act -> audit result. No SQLite transaction is held while taking the audit lock or doing network I/O. If the decision can't be audited, nothing runs and the reservation is released; after a crash a reservation stays spent (fails closed). S1 reserves only the tool's `per_day` count. Limits on a resource used inside an action (research jobs, dollars, the Slack slot) are reserved by that feature just before use and recorded in the audit result; S1 does not release them.
- **S2 Text:** all outside text is untrusted. Output to the model is JSON with `"untrusted": true`, control characters escaped, lengths capped, emails and phone numbers masked.
- **S3 Network:** fixed hosts only (`api.hubapi.com`, `hooks.slack.com`, Anthropic SDK), TLS, no redirects, explicit timeouts.
- **S4 Time:** "day" is one timezone in `policy.yaml`. The store keeps the latest day seen; a clock moved backwards doesn't reset caps.
- **S5 Secrets:** server loads only `.env.server`; `approve` loads only `.env.approver`; both must be mode 0600. `state/` is 0700. Keys never reach logs or the model.
- **S6 Errors:** the model sees a short code (`blocked`, `invalid`, `busy`, `unavailable`, `limit`, `open_proposal`), never internals. Non-error statuses: `queued`, `needs_review`, `missing`.

---

## F1. Policy (`policy.py`, `policy.yaml`)

- **Does:** states what is allowed; anything absent is denied.
- **Design:** Pydantic, `extra="forbid"`. Top level: `version`, `timezone`, `writes_enabled` (kill switch), `writes_per_day` (20), `min_confidence` (0.6), `tiers` (A 70, B 40), `research` (30 calls, $3), `write_fields` per object, `tools`. Per tool: `allowed`, `approval`, `per_day`, argument limits. Tools omitted or `allowed: false` aren't listed.
- **Fixed in code, so YAML can't loosen them:** `update_lead_score` and `create_followup_task` must have `approval: true`; `write_fields` must be a subset of contact {`lead_score`, `lead_score_reason`} and task {`hs_task_subject`, `hs_task_body`, `hs_timestamp`, `hs_task_status`, `hubspot_owner_id`}; Slack `per_day` <= 1; limits have maximums. YAML can tighten only.
- **Loading:** safe loader, duplicate keys rejected, read once at server start; `approve` reads it fresh before each write. SHA-256 of the file bytes is the policy hash on every audit record.
- **Failure:** missing, invalid or loosened policy stops the process with a clear message on stderr.
- **Tests:** each rule on and off, unknown key, duplicate key, loosening attempt, missing file, kill switch.

## F2. Audit log (`audit.py`)

- **Does:** records every call and write decision, with changes detectable.
- **Record:** `seq`, `time`, `process`, `call_id`, `phase` (`decision`, `intent`, `result`, `repair`, `start`), `tool`, `args`, `decision`, `rule`, `actor`, `result`, `policy_hash`, `prev_hash`, `hash`. Canonical JSON (sorted keys, UTF-8, no floats: scaled integers); `hash = SHA-256(prev_hash + body)`; the first record is a genesis `start` with a zero hash.
- **Redaction:** if a call fails size, policy or validation, `args` is one HMAC-SHA-256 of the raw argument bytes plus their length. If it passes, only IDs, dates and integers already within the tool's declared range are logged raw, and confidence as a scaled integer; all other text is HMAC plus length. An unregistered tool name is HMAC plus length. A record is at most 8 KB. The key is `state/hmac.key`, 0600, created once with `O_EXCL` under the audit lock.
- **Append:** `flock` on a lock file (5 s wait, then fail), read the tail, write one line, `fsync`, unlock.
- **Integrity:** `audit verify [--anchor HASH]` checks sequence, chain and anchor, and lists decisions or intents with no result. It detects edits that don't recompute later hashes; full rewrite or truncation only against an outside hash, up to that point. A torn last line blocks startup until `audit repair`, which moves the bytes aside and appends a `repair` record.
- **Failure:** disk full, lock timeout or torn tail means the append fails, so S1 denies the call. If only the *result* append fails, the server stops accepting calls until restart; the missing result shows in `verify`.
- **Tests:** edit, delete, reorder, rewrite vs. anchor, truncation vs. anchor, torn line, two-process append, disk full, result-append failure, record size; no email, key, CRM phone field or free text in any line; an out-of-range integer is never raw (a digit string valid as a contact ID is logged).

## F3. Store and limits (`store.py`)

- **Does:** keeps shared state for both processes; survives restarts.
- **Design:** SQLite, WAL, `synchronous=FULL`, `busy_timeout` 5 s, `BEGIN IMMEDIATE` for every write, one connection per call, `user_version` checked at open. Single local disk.
- **Tables:** `seen(contact_id, seen_at)`; `sources(source_id, company_id, name_domain_sha, url, title, fact, expires_at)`; `research(company_id, name_domain_sha, status, expires_at)`; `proposals(id, kind, contact_id, payload, payload_sha, snapshot, display, state, created_at, expires_at, done_at, retries, version)`; `review(contact_id, reason, at)`; `counters(day, key, n)`; `spend(id, day, reserved, settled, status)`; `slack(day, state, attempts)`. `done_at` is set in the CAS to `done`. `review` has one row per contact; it is deleted when a score proposal for that contact is inserted, or when you dismiss it in `approve` (audited). No email, phone or contact message is stored.
- **Caps:** `reserve(key, limit)` increments and checks in one transaction. Reservations are released only by S1.
- **Proposal states:** `pending -> approved | rejected | expired | stale`; `approved -> executing | pending | expired | stale`; `executing -> done | failed | ambiguous | pending` (`pending` only when the intent append failed, before any send); `ambiguous -> done | failed | executing` (score only; this CAS requires `retries = 0` and sets it to 1). Every move is compare-and-swap on `(state, version)`; a partial unique index allows one open proposal (`pending`, `approved`, `executing`, `ambiguous`) per contact and kind.
- **Expiry:** checked on read; an expired `pending` is moved to `expired` in the transaction that sees it, freeing the slot.
- **Failure:** busy or corrupt store means deny. A newer schema version stops startup.
- **Tests:** two-process caps, restart, clock moved back, every legal and illegal transition, unique index, expiry frees the slot, busy.

## F4. Server and guard (`server.py`, `guard.py`)

- **Does:** the only path from a client to any tool.
- **Design:** SDK v2 **low-level `Server`** with `on_list_tools` and one `on_call_tool`. Its docs say it forwards unlisted names and doesn't apply the input schema, so one handler sees every `tools/call` with raw arguments. `MCPServer` is not used (it validates before the handler and drops extra fields), nor middleware (marked provisional). Served with `stdio_server()`.
- **Handler:** runs the whole S1 pipeline synchronously in a worker thread that isn't abandoned on cancellation, so a client timeout can't separate a decision from its result. A bounded pool of 4; when full, `busy`, also audited. Audit appends from a full pool run in one reserved slot.
- **Validation:** raw arguments over 16 KB denied; `None` becomes `{}`; strict Pydantic models with `extra="forbid"` (no coercion; booleans aren't integers; NaN and infinity rejected). The advertised schema comes from the same models.
- **Results:** `CallToolResult` with `is_error=True` and an S6 code for denials. Every exception is caught inside the handler; the detail goes to stderr only.
- **Limits of the claim:** a message that fails protocol parsing never reaches the handler; it does nothing and isn't logged. Tool annotations are set, but nothing relies on them.
- **Startup:** check env file modes, policy, store version, audit tail; write `start`; verify every policy tool has a registered handler and model.
- **Tests:** SDK in-memory client plus one real stdio subprocess. Unknown tool, extra key, wrong type, huge arguments, exception in a tool, cancelled call still completes and is audited, pool full, audit failure denies, nothing but protocol on stdout.

## F5. HubSpot read tools (`hubspot.py`)

- **`search_new_leads(days)`**: integer 1-30. Contact search, `createdate >= now - days`, newest first, limit 50, properties `jobtitle`, `message`. Returns ID, job title, message (500 chars, masked), `truncated` if more exist. Upserts `seen`. HubSpot's search index lags, so very new leads may appear on a later call; the result says so.
- **`get_company(contact_id)`**: ID is 1-20 digits and in `seen` within 24 h, else `blocked`. Reads associations live; exactly one company, else `missing`. Returns company ID, name, domain, industry, size, city, country, and `data_complete` (name, valid domain, industry and size all present).
- **Domain check:** lower-cased, IDNA-encoded hostname with a dot; no IP, port, path or credentials.
- **Client:** 10 s timeout, 250 ms spacing for search (limit 5/s), reads retry 429 and 5xx with `Retry-After`, capped at three attempts and 20 s total. Pagination isn't followed. Responses are parsed with Pydantic; a bad shape is `unavailable`.
- **Limits:** 200 calls a day each.
- **Tests:** saved responses for 0, 1, 50 and over 50 leads; 0, 1 and 2 companies; bad domains; masking; unseen ID; 429, 5xx, timeout, malformed JSON.

## F6. Research (`research.py`)

- **`research_company(contact_id)`**: needs `data_complete`'s name and domain. Returns up to 10 facts (300 chars), each with a source ID, title and URL.
- **Call:** `client.beta.messages.create`, `claude-opus-5-5`, `output_config.effort="medium"`, `max_tokens` 8000, `web_search_20260209` with `allowed_callers: ["direct"]` and `max_uses: 3`, `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), `max_retries=0`. Up to 2 `pause_turn` resumes with identical tools. 90 s overall deadline, passed as each request's timeout.
- **Input:** fixed prompt plus name (100 chars, control characters removed) and domain, marked as data. No contact data.
- **Facts come only from code:** text blocks carrying web-search citations; URL must be `https`, at most 500 chars. Uncited text is dropped. IDs are random and bound to the company. Zero facts, refusal, truncation, or a search error other than `max_uses_exceeded` returns `unavailable`.
- **Cache:** 7 days by company ID plus a hash of name and domain (a changed domain misses). `unavailable` is cached 1 hour, to stop paid retries. Because the worker isn't abandoned, a client that timed out gets the cached result on its next call.
- **Money:** `flock` held across the whole job; another caller waits 5 s, then `busy`; the cache is rechecked after the lock. Order: cache, then 30-a-day cap (hard, counts every job), then per request reserve $1.00 if settled plus reserved stays within $3. Settle tokens from `usage.iterations` when it is a non-empty list, otherwise from top-level `usage`; add searches from `usage.server_tool_use` ($10 per 1,000), using a dated price table. If a needed field is missing, or the model or outcome is unknown, keep the reservation. Tool `per_day`: 100 calls, including cache hits. Actual cost above $1.00 is recorded in full, so spend can pass $3 by at most one request's excess over its reservation. A Console spend limit you set is the backstop.
- **Tests:** saved responses for success, pause, refusal, truncation, search error, `max_uses_exceeded`, uncited text, `http` URL, fallback model, unknown model, null `iterations`; call cap, dollar cap, timeout keeps reservation, cache hit/miss/negative, two processes.

## F7. Proposals (`proposals.py`)

- **`update_lead_score(contact_id, score, reasons, confidence, source_ids)`**: integer 0-100; 1-3 reasons, 200 chars, one line; confidence 0-1; 1-5 distinct source IDs, unexpired and issued for this contact's current company ID and its current name and domain hash.
- **`create_followup_task(contact_id, subject, body, due_date)`**: subject 100, body 500 (newlines allowed), ISO date 1-60 days ahead. Owner is read from the contact; none means `blocked`.
- **Text rule:** must be NFC; rejects Unicode categories Cc (except newline in `body`), Cf, Cs, Co, Cn, Zl, Zp; rejects `<`, `>`, HTML entities, `[mcp:`, `://` and `www.`.
- **Flow:** ID in `seen` within 24 h -> live company and contact read -> rules -> one transaction: one-open check and insert. The snapshot holds current score, reason and owner; display holds company name, source links, confidence, tier and reasons, fixed at creation; F8 and F9 use these and never recompute them.
- **Needs review:** confidence below `min_confidence` or `data_complete` false upserts `review` and makes no proposal. An open proposal for that contact returns `open_proposal` instead. Confidence is self-reported, so this guides an honest model; approval is what binds a hostile one.
- **Payload:** the exact HubSpot body, canonical JSON plus SHA-256. Score: `lead_score`, and `lead_score_reason` built by code from tier and reasons. Task: allowed fields, due at 17:00 policy time, contact association, body ending with the code-added marker `[mcp:<random id>]`. `expires_at` is 24 hours after creation, or the task's due time if earlier.
- **Limits:** 60 calls a day per tool, including rejected and needs-review calls.
- **Reply:** `queued` plus the proposal ID, or `needs_review` with a fixed reason code. The server holds no write key.
- **Tests:** each limit at, below and above; every banned category; foreign, expired, duplicate and invented source IDs; needs-review paths; no owner; flood; company changed after research.

## F8. Approval (`approve.py`)

- **Start:** needs a TTY, the write key, valid policy with `writes_enabled`, healthy audit, and an exclusive `flock` held for the whole run (freed on crash); a second instance exits. Then recovery, then pending proposals oldest first. After pending proposals, list open reviews (contact ID, reason, age); `d` dismisses one, audited; nothing is sent to HubSpot.
- **Screen:** exact stored payload, company name, current HubSpot values, confidence, evidence links, age. Any non-printing character shows as an escape; nothing is stripped or shortened. Input is `y`, `n` or `q`; anything else is no decision. There is no approve-all.
- **On yes:** audit decision -> CAS to `approved` -> re-read contact -> payload SHA, expiry, policy, allowed fields on the final body, snapshot unchanged (score, reason; owner for tasks) -> one transaction: take one daily write and CAS to `executing` -> audit intent -> send exactly the stored payload -> classify -> CAS -> audit result. Failed checks, all audited: wrong payload SHA, forbidden field, changed snapshot or contact 404 -> `stale`; past expiry -> `expired`; policy off, cap reached or the re-read unavailable -> `pending`, not shown again this run. On a recovery retry, any failed check leaves the item `ambiguous` and held. A failed intent append sends nothing for that attempt: a first attempt returns `executing` to `pending`, a recovery retry returns to `ambiguous` and is held; the write stays counted. After any failed audit append, `approve` makes only that move, or the state change for a request already sent, then prints the proposal ID and exits non-zero; no later proposal is shown.
- **Classify:** 2xx `done`; 429 retried up to 3 times, `Retry-After` capped at 10 s (scores only after re-reading; if the snapshot changed or the re-read fails, stop), then `failed` on a first attempt or `ambiguous` and held on a recovery retry; any other 4xx `failed`; everything else `ambiguous`, reconciled once at the end of the run.
- **Recovery (under the lock):** `approved` returns to `pending`. `executing` becomes `ambiguous`. Score: current equals proposed is `done`; equals snapshot retries once through all checks and the cap; else held. Task: marker found in the contact's associated tasks is `done`; missing is always held, never re-sent. You resolve a held item as `done` or `failed` after looking in HubSpot; that is audited and sends nothing.
- **Accepted limit:** HubSpot offers no conditional update here, so an edit made between the re-read and the PATCH (well under a second) could be overwritten.
- **Actor:** OS username.
- **Tests:** changed payload, expired, stale, owner changed, rejected, non-TTY, kill switch, policy tightened, cap at execution, second approver refused, lock freed after crash, forbidden field; audit failure at each append; intent append failure on a recovery retry; crash at every step; timeout after acceptance; lagging association; held item both ways. Invariants: at most one task per proposal, no write without a decision and intent record, a finished score is never `stale`.

## F9. Slack summary (`slack.py`)

- **`post_slack_summary()`**: no arguments.
- **Content:** leads seen today; top 5 scores that reached `done` today (company, score, tier, approved reasons, HubSpot link from configured portal ID); counts of open reviews and pending proposals; log hash as the audit anchor. No leads gives a "no leads" message. The model can influence the counts and when the one post happens, nothing else.
- **Payload:** `plain_text` blocks for all variable text; the `text` fallback is a constant; `&`, `<`, `>` escaped; unfurl flags off; each field truncated to block limits. The company name shown is the one the approver saw.
- **Sending:** webhook must be `https://hooks.slack.com/services/...`; 10 s timeout. The S1 counter for this tool is a fixed 10 calls a day; policy `per_day` (0 or 1) means posts and is enforced by the slot. Slot moves are CAS: none -> `reserved`; `failed -> reserved` when `attempts < 3`; `reserved -> posted | failed | ambiguous`. The slot is taken inside the action, after the audit decision. Outcomes: 2xx `posted`; 4xx `failed`; 429 waits `Retry-After` if at most 20 s and retries once, then `failed`; everything else `ambiguous`. The sender holds a `flock` from reservation to final state. Another caller that cannot get it returns `busy`. A `reserved` row found while holding the lock was left by a crash and becomes `ambiguous`. `ambiguous` is never reposted that day.
- **Tests:** payload snapshot, escaping, `@here` and link text, empty day, second post, 4xx retry, 429, timeout, crash after send, two processes.

## F10. Setup and data (`check_setup.py`, `data/leads.csv`, README)

- **You do:** create two Service Keys (read: contacts, companies, contact schemas; write: contacts and tasks, plus contact, company and task reads), the Slack webhook, and both env files (`.env.server`: HubSpot read key, Anthropic key, Slack webhook, HubSpot portal ID (digits); `.env.approver`: HubSpot write key only); set a Console spend limit; add `lead_score` (number) and `lead_score_reason` (multi-line text) in HubSpot's UI; import the CSV as contacts plus companies, with a contact owner, company name and domain mapped. I don't read keys.
- **CSV:** 30 invented contacts at real companies, `example.com` emails, 3 with injected instructions in `message`. Nothing is emailed; you confirm no email workflow is on.
- **`check_setup`** (read key only): env modes, portal ID is digits, key works, both fields exist with the right type and nothing named the same, 30 contacts, each with exactly one company, expected domain and an owner. Prints pass/fail per item and changes nothing.
- **Key separation test (your OK):** PATCH a nonexistent contact with the read key must return 403 with a missing-scope error; 404 or anything else fails.
- **First live check:** whether free HubSpot allows two Service Keys. If not, stop and ask: drop the split (weaker claim, stated) or use a project-based app.
- **Claude Desktop:** config entry shown for your OK; contains a path, no secrets.
- **Tests:** `check_setup` against saved good and bad accounts; CSV has 30 unique rows, only `example.com` emails.

---

## Files

`pyproject.toml` (`mcp` 2.x, exact versions locked; `anthropic` 1.x, `httpx2`, `pydantic`, `pyyaml`, `python-dotenv`, `pytest`, `anyio`), `policy.yaml`, `.gitignore` (`.env*`, `state/`), `.env.server.example`, `.env.approver.example`, `README.md`, `hubspot_mcp/` (modules above), `data/leads.csv`, `tests/`. HubSpot API version is one constant (`2026-09`).

## Build order (your OK where marked)

1. `git init`; F1, F2, F3, F4 with a stub tool; confirm the low-level server's behavior on the locked version
2. F5, F6, F7, F8, F9 against saved responses; confirm HubSpot paths and scopes in its reference
3. F10: you create keys; live read. **OK:** key separation test
4. You add the fields and import the CSV; `check_setup`
5. **OK with cost estimate:** 3 live research calls; report real cost
6. **OK:** Claude Desktop config. Read tools, then one blocked write, one rejected and one approved proposal
7. **OK:** one Slack post, after you see the payload
8. README with diagram, sample log, known limits. Publishing only if you ask

**Tester gates on code:** after steps 2 and 8, same tester standard, per feature, looped to 10/10. The tester gets plan, code and test output, never key files. A finding we disagree on twice is yours to decide.

## End-to-end verification

- **Compromised-model suite:** a scripted client makes at least 25 unsafe calls across all features; reports blocked/total; asserts no HubSpot write, no Slack text from the model, and an audit decision for each call.
- **Live, Claude Desktop:** the 3 injected leads change nothing in HubSpot without approval. Whether the score is influenced is measured in Phase 2 with clean/injected pairs.
- **Retries and crashes:** fake clock and fault injection at every external step in F8 and F9.
- **Done when** all tests and live checks pass, both code gates score 10/10 per feature, and someone can run it from the README.

## Phase 2 (not in this plan)

`run_agent.py`, customer profile and rubric, clean/injected pairs, timed metrics on separate lead sets, demo video, optional read-only local page.

## Open risks

Two Service Keys and custom fields on free accounts; `2026-09` association paths, task type and scopes; association read-after-write; HTML rendering in task bodies; low-level server concurrency, cancellation and stdio details on the locked version (not stated in its docs); Claude Desktop timeout versus the 90 s research deadline; Slack link and unfurl handling.

## Changes made while building

- `update_lead_score` accepts an empty `source_ids` list, so a lead with no company data can reach needs review; a proposal still needs at least one valid source.
- `min_confidence` has a floor of 0.5 in code.
- `audit repair` moves only an incomplete last line. A complete but invalid line is kept as evidence and blocks startup.
- `payload_sha` covers the kind and contact ID as well as the body. A `meta` table holds the latest day.
- Any 4xx on a recovery retry is held as `ambiguous`.
- The approval prompt discards anything typed before it appeared.
- The contact owner isn't in the CSV: bulk-assign it in HubSpot after the import.
- The 20 s HubSpot read budget covers attempts, waits and stalled connections. A response trickled a byte at a time can run longer.
- Right-to-left letters are escaped on the approval screen, because they can reorder the line.
