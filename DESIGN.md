# DESIGN.md — PredictionMarket.py

## 1. Design Goals

The contract is built around four non-negotiable properties. Every other
decision documented below is a consequence of one of these.

**Deterministic state machine.** A market's status is never inferred from
combinations of other fields (e.g. "has a pending outcome and the window
expired"). It is an explicit enum-like string, and every write to it passes
through a single choke point (`_set_status`) that checks a static transition
table. This makes the reachable state space enumerable and auditable instead
of emergent.

**No permanent fund lock.** This is the primary property the reviewer
feedback was about, and it is treated as an invariant rather than a
best-effort goal: for any state a market can be in, there must exist at
least one enabled, permissionless transaction that eventually leads to
`Resolved` (with a working claim path) or `Cancelled` (with a working refund
path). The design was produced by enumerating every state and asking "what
happens if every external actor simply stops acting from here?" — and
closing every case where the answer was "nothing, forever."

**Permissionless recovery.** None of the fund-recovery paths
(`finalize_resolution`, `cancel_market`, `refund_bet`, `claim_winnings`) are
restricted to the market creator or a privileged role. If they were, a
non-responsive or malicious creator would itself be a fund-lock vector. The
only owner-gated functions (`withdraw_platform_fees`,
`set_confidence_threshold`) are non-custodial — they never touch a bettor's
principal or winnings, only accumulated platform fee revenue and a
resolution parameter.

**AI-assisted but safety-first.** The LLM/web-fetch layer is treated as an
untrusted oracle: useful for producing an outcome and a confidence estimate,
but never trusted to single-handedly authorize an irreversible payout
without the deterministic Python layer independently re-checking its claims
(corroboration count, confidence threshold, fetch status). The AI can make
the market *indecisive*; it cannot make it *unsafe*.

## 2. Threat Model

This section enumerates the concrete adversarial and failure scenarios the
implementation was built to withstand. It does not restate the mechanisms in
full — see Section 7 for implementation detail — but maps each threat to the
specific design decision that closes it.

**Evidence spam.** An attacker resubmitting evidence to consume validator
attention or buffer capacity is given a real, non-recoverable cost: a staked
submission that turns out to be a duplicate forfeits its stake to
`fee_collected` rather than being refunded (Section 7). Submissions without
a stake remain free, so this is an economic deterrent available to callers
who choose to use it, not an access-control barrier.

**Duplicate evidence submissions.** Independent of staking, a duplicate
(URL-normalized) submission is not appended to the buffer and does not
advance `evidence_total_submitted`. This prevents an attacker from using
repeated identical submissions to evict other participants' evidence out of
the circular buffer.

**Evidence buffer exhaustion.** A buffer that rejects submissions once full
is itself an attack surface — an attacker (or simply an active market) could
fill it to block all further evidence and therefore block resolution. The
circular buffer evicts the oldest entry instead of refusing new ones, so
buffer occupancy can never be used to block submissions.

**Abandoned disputes.** A challenger (or anyone) who disputes a market and
then submits no further evidence leaves the market in `Disputed`
indefinitely if there is no independent exit. `cancel_market` permits
cancellation once `last_action_time` has not advanced for
`CANCEL_TIMEOUT_SECONDS`, regardless of which address opened the dispute or
whether it returns.

**Stale markets nobody resolves.** Symmetric to the above for `Open`
markets: if `request_resolution` is never called, `cancel_market` becomes
available once `end_time + CANCEL_TIMEOUT_SECONDS` has elapsed, independent
of evidence state.

**Double claim / double refund attempts.** Both `claim_winnings` and
`refund_bet` check and set the same `claimed` entry before any transfer is
issued. A second call against an already-claimed key is rejected before
`_pay` is reached.

**Zero winning-side bettors.** An outcome the AI resolves to, but that no
one bet on, cannot be turned into a payout. `_finalize_market_result`
checks the winning side's total before allowing a `Resolved` transition and
routes to `Cancelled` instead if it is zero, which converts an otherwise
unpayable resolution into a refundable one.

**Prompt injection in fetched sources.** Fetched page content is treated as
adversarial input by construction: it is structurally delimited in the
resolution prompt with explicit instructions not to treat it as commands,
and independently scanned in deterministic Python
(`_looks_like_injection`) to reduce the credibility score of sources
containing known injection phrasing. This is a mitigation, not a guarantee —
the marker list is coarse, and the primary defense (prompt structuring) is
a model-following instruction, not a cryptographic barrier.

**AI uncertainty / hallucinations.** A model producing a confident-sounding
but weakly-supported answer is constrained by two independent, deterministic
checks — a minimum confidence threshold and a minimum number of
independent corroborating source domains — before its Yes/No output is
accepted (Section 6). Neither check can detect a wrong answer that is
genuinely well-corroborated; they bound how much unsupported confidence the
contract will act on, not whether the model is correct.

**Failed or unreachable evidence sources.** `_fetch_source` classifies every
fetch outcome (`ok`, `empty`, `not_found`, `server_error`, `error`) rather
than treating failures as silent empty content indistinguishable from a
genuinely uninformative page. Only `ok` sources populate the default
corroboration set, so a market cannot be resolved on the basis of sources
that were never actually retrieved.

---

## 3. Design Invariants

The following properties are intended to hold for every market regardless
of which sequence of valid transactions produced its current state.

**Every non-terminal state has at least one permissionless path toward a
terminal state.** This is enforced structurally by the combination of the
`_VALID_TRANSITIONS` table and the availability of `request_resolution`,
`finalize_resolution`, and `cancel_market` to any caller — none of the
functions that advance a market's status are restricted to a specific
address. The invariant would fail only if some non-terminal status existed
with no outbound edge reachable by an unprivileged caller; the transition
table was constructed specifically to rule that out (Section 4).

**User funds are always recoverable through either a claim or a refund.**
`_finalize_market_result` is the sole path to `Resolved`, and it never takes
that path unless the winning side has a non-zero total — otherwise it
cancels. Consequently, every market that becomes `Resolved` has at least one
address that can `claim_winnings`, and every market that becomes `Cancelled`
allows every bettor to `refund_bet`. There is no third outcome.

**Terminal states are irreversible.** `Resolved` and `Cancelled` both map to
the empty set in `_VALID_TRANSITIONS`, and `_set_status` raises on any
transition not present in that table. No function in the contract attempts
to write a status for a market already in a terminal state.

**Evidence submission can never be permanently blocked.** The circular
buffer has no rejection branch based on capacity (Section 5). The
rejection conditions actually present in `submit_evidence` — a market in a
terminal status, a call made before `end_time`, or a malformed URL — are
either permanent by design (a terminal market has no further use for
evidence) or temporary and time-bounded (the pre-`end_time` gate lifts on
its own), never capacity-dependent.

**Payout and refund paths are protected against double payment.** Both
paths gate on the same `claimed` map, set to `True` before the external
transfer in `_pay` is issued. Because state is updated before the
transfer occurs, a re-entrant or repeated call observes the claim as already
settled.

**`total_pool` remains consistent with recorded betting totals.** Every
increment to `total_pool` happens exactly once, alongside the matching
increment to `total_yes` or `total_no`, inside `_buy`. Forfeited challenge
stakes are credited to `fee_collected`, never to `total_pool`, specifically
so that no value enters the pool without a corresponding bettor balance
backing it — this is what allows `refund_bet` and `claim_winnings` to compute
payouts directly from recorded balances without a separate reconciliation
step.

**AI output alone never directly authorizes an irreversible fund transfer.**
The raw leader/validator result is passed through `_derive_final_outcome`
— a deterministic function applying the confidence and corroboration checks
— before it is used anywhere that affects `statuses` or `results`. A
market can only reach `Resolved` through `_finalize_market_result`, which
consumes this already-filtered outcome, not the model's raw response.

---

## 4. State Machine

```
Open ──────────────┬──────────────► ChallengePeriod
  │                │                        │
  │ (Unclear)      │ (Yes/No)               │ (challenged)
  │                └────────────┐           ▼
  │                              │       Disputed
  ▼                              │        │   │
Cancelled ◄─────────────────────-┴────────┘   │
  ▲                                            │
  │ (attempts exhausted / stale / zero          │ (Yes/No, attempts
  │  winning-side bettors)                       │  remaining)
  │                                              ▼
  └───────────────────────────────────── ChallengePeriod
                                                │
                                                ▼
                                            Resolved
```

All transitions are enforced by `_VALID_TRANSITIONS`, a static adjacency map
keyed by current status. `Resolved` and `Cancelled` map to the empty set —
they are structurally terminal, not terminal by convention.

**Open.** Betting is enabled. Exit requires `request_resolution` (any
caller, after `end_time`) or `cancel_market` once the market has been
`Open` for longer than `end_time + CANCEL_TIMEOUT_SECONDS` with no
resolution ever attempted. The staleness path exists specifically so that a
market nobody bothers to resolve does not sit on bettors' funds
indefinitely — betting itself is the only fund-affecting action available
in this state, and it is symmetric (both sides can always be refunded via
`Cancelled`).

**ChallengePeriod.** The AI has produced a Yes/No outcome; a fixed
`CHALLENGE_WINDOW_SECONDS` window is open for anyone to post a stake and
dispute it. Exit is via `challenge_resolution` (→ `Disputed`) or
`finalize_resolution`, which is callable by anyone once the window closes —
not just the party who requested resolution. This is deliberate: requiring
the *same* address to both trigger resolution and finalize it would
reintroduce a single point of failure.

**Disputed.** A challenge is active (or the very first resolution attempt
came back `Unclear`). The only way out is another `request_resolution` call
against *new* evidence, or the staleness path in `cancel_market` (see
Failure Recovery, "Abandoned disputes"). Disputed markets do not linger
without a live requirement to move forward.

**Resolved.** Terminal. The winning side can `claim_winnings`. Reached only
through `_finalize_market_result`, which is the single function permitted to
set `results[market_id]` — see Resolution Architecture for why this
function, not the AI's raw output, is the actual authority on whether a
market resolves at all.

**Cancelled.** Terminal. Every bettor, on either side, regardless of what
the AI concluded, can call `refund_bet` for their exact original stake.
Because `Cancelled` is reachable from every non-terminal state (directly or
via one intermediate hop), it functions as the universal fallback exit for
the whole state machine — anything that would otherwise be a dead end routes
here instead.

The reason every state has a deterministic exit is structural, not
incidental: the transition table was designed by working backward from "no
node in this graph may have out-degree zero unless it is `Resolved` or
`Cancelled`," then verifying each non-terminal state has a *permissionless*
edge satisfying that, not merely a theoretical one gated behind an actor who
might never show up.

---

## 5. Evidence System

Evidence is stored per-market as a capped JSON array (`evidence_json`,
bounded by `MAX_EVIDENCE = 10`) plus a separate scalar counter
(`evidence_total_submitted`) that only ever increases.

**Circular buffer.** Once the array reaches `MAX_EVIDENCE`, the next accepted
submission evicts the oldest entry (`items[-MAX_EVIDENCE:]`) rather than
being rejected. The original design rejected submissions past the cap; that
is the exact mechanism the reviewer flagged as a deadlock, because a
`Disputed` market's only exit requires *new* evidence, and a hard cap makes
"new evidence" impossible to produce once the buffer fills. Eviction trades
long-term evidence history for guaranteed liveness — a trade this contract
always takes, since a structured resolution report (`resolution_reports`,
Section 6) and per-address reputation counters already persist the
durable, low-volume signal worth keeping, independent of whether the raw
evidence item that produced them is still in the buffer.

**`evidence_total_submitted`.** This counter exists because "does this
market have new evidence?" cannot be answered by the buffer's contents once
eviction is possible — a buffer holding the same 10 items it held an hour
ago could still represent 10 brand-new submissions. The counter is
incremented on every accepted, non-duplicate submission and snapshotted into
`evidence_submitted_at_last_resolution` at resolution time; the gating check
(`submitted <= last_seen` → reject) is therefore correct independent of
buffer occupancy.

**Overwriting vs. rejecting.** Rejecting new evidence protects historical
completeness at the cost of liveness. Overwriting protects liveness at the
cost of historical completeness. Given the fund-safety goal in Section 1,
this contract always resolves that trade-off in favor of liveness — a market
that can always eventually move is preferable to one that preserves a full
evidence trail but can stall permanently under adversarial or merely
inattentive conditions.

**Why disputed markets require fresh evidence.** Allowing `request_resolution`
to re-run against unchanged evidence would let a challenger indefinitely
flip-flop a market's status (or a resolver indefinitely retry) without
providing new information, burning `MAX_RESOLUTION_ATTEMPTS` on repeated
LLM calls that cannot possibly produce a different, evidence-grounded
answer. Requiring `evidence_total_submitted` to have advanced since the last
attempt forces every resolution round to be informationally distinct from
the last, which is also why the attempt counter is bounded — see Section 8,
"AI uncertainty."

---

## 6. Resolution Architecture

`request_resolution` drives a leader/validator pair via
`gl.vm.run_nondet_unsafe` rather than `gl.eq_principle.strict_eq`. This is a
direct consequence of the resolution output no longer being a single
enumerable word: the model now returns a structured object (outcome,
confidence, reasoning, sources used). Free-text reasoning will not match
byte-for-byte between independently-executed leader and validator calls, so
strict equality over the full response would fail validation on essentially
every transaction. Instead, `validator_fn` independently reruns the same
analysis and compares only the *deterministically derived decision*
(`_derive_final_outcome`) between its own result and the leader's proposal —
non-comparative equivalence on the decision, not the prose that produced it.
The leader's full structured report is still what gets persisted
(`resolution_reports`), since only the decision needs cross-validator
agreement, not the specific wording used to justify it.

**Confidence threshold.** `_derive_final_outcome` downgrades any Yes/No
result to `Unclear` if the model's reported confidence is below
`confidence_threshold` (default 80, owner-adjustable via
`set_confidence_threshold`). This exists because an LLM will produce a
syntactically valid Yes/No answer even from thin or ambiguous evidence; the
confidence field is the model's own signal of how much weight that answer
deserves, and the contract enforces a floor on it rather than trusting every
syntactically valid answer equally.

**Independent-source requirement.** Separately from confidence,
`_derive_final_outcome` also requires at least `MIN_CORROBORATING_SOURCES`
(2) distinct domains among `sources_used` before accepting a structured
Yes/No. A single high-confidence source is not evidence of independent
corroboration — the model could be confidently wrong, or the source itself
could be low-quality — so this check exists as a structural constraint the
model cannot talk its way around with a high confidence number alone. (The
legacy single-word protocol, kept for backward compatibility, is exempt from
this specific check since it never had multiple sources to begin with; it
still passes through the confidence-threshold check.)

**Why low confidence becomes `Unclear` instead of finalizing.** The
alternative — finalizing a low-confidence result — would convert model
uncertainty into an irreversible on-chain payout decision. Routing it to
`Unclear` instead means the market falls back into the `Disputed`/retry path
(or eventually `Cancelled` once attempts are exhausted, see Section 8),
which is the same recovery path used for genuine AI indecision. Uncertain
evidence and absent evidence are treated identically on purpose: both mean
the contract does not yet have grounds to pay anyone.

---

## 7. Security Considerations

**No fund-lock scenarios.** Enumerated and closed per-state in Section 4;
the concrete mechanisms are the circular evidence buffer (Section 5), the
zero-bettor auto-cancellation check in `_finalize_market_result` (any
`Resolved` transition first confirms the winning side's `total_yes` or
`total_no` is non-zero; if it is zero, the market is cancelled instead), and
the staleness paths in `cancel_market`.

**Double-claim prevention.** `claim_winnings` and `refund_bet` both gate on
a single shared `claimed` map keyed by `f"{market_id}:{user}"`, checked and
set to `True` before any external transfer occurs. Since a market cannot be
simultaneously `Resolved` and `Cancelled` (they are mutually exclusive
terminal states), sharing this map between the two functions introduces no
ambiguity about which payout a given `claimed` entry refers to.

**Double-refund prevention.** Same mechanism as above — `refund_bet` is
itself gated by the `claimed` map, and additionally only enabled in the
`Cancelled` state, which is terminal and cannot be re-entered.

**Duplicate evidence handling.** `submit_evidence` normalizes the incoming
URL (trimmed, lower-cased, trailing slash removed) and compares it against
every currently-stored item before appending. A duplicate is not appended,
does not increment `evidence_total_submitted`, and does not consume buffer
capacity — an attacker resubmitting the same URL repeatedly cannot use it to
evict other participants' evidence.

**Spam resistance.** `submit_evidence` is payable but does not *require*
value, preserving compatibility with zero-cost submissions. When a stake is
attached, a new/valid/unique submission refunds it in full; a duplicate
submission forfeits it to `fee_collected` instead of refunding it — this
gives duplicate/spam submissions a real, non-recoverable cost precisely
because that is the case that is otherwise "free" for an attacker to repeat.

**Prompt injection mitigation.** Two independent layers. First, the
resolution prompt structurally delimits fetched page content
(`--- SOURCE n ---` / `--- END SOURCE n ---`) and explicitly instructs the
model that anything inside those markers is inert evidence text, never
instructions — including text that impersonates a system or developer
message. Second, and independent of whether the model is fooled, fetched
content is scanned in plain deterministic Python
(`_looks_like_injection`) against a small marker list; a match reduces that
source's `credibility_score`. The second layer exists because the first is
a prompting technique, not a guarantee, and the contract does not want its
only defense against manipulated evidence to be "trust the model to notice."

**Challenge stake handling.** Every code path that leaves `Disputed`
(outcome confirmed, outcome flipped, still `Unclear`, or cancelled for
staleness) routes through `_pop_challenge_stake`, which atomically clears
`active_challenger`/`challenge_stakes` and returns the amount to the caller
for disposal. This single choke point exists so that "did we remember to
refund or forfeit the stake" is answered once, structurally, rather than
re-implemented (and potentially missed) at each of the four call sites. A
forfeited stake is credited to `fee_collected`, deliberately kept separate
from `total_pool`, so the invariant `total_pool == total_yes + total_no`
holds unconditionally — this is what guarantees `refund_bet` and
`claim_winnings` arithmetic can never be short of funds regardless of how
many challenges a market went through.

**Owner permissions.** Scoped to exactly two functions, both non-custodial:
`withdraw_platform_fees` (moves only accumulated `fee_collected` revenue,
never a bettor's principal or winnings) and `set_confidence_threshold`
(adjusts a resolution parameter within a validated 0–100 range). The owner
has no path to freeze a market, seize a bettor's funds, or bypass the
permissionless recovery mechanisms in Section 4.

---

## 8. Failure Recovery

**AI uncertainty.** An `Unclear` result on the first attempt moves `Open` →
`Disputed` directly rather than into `ChallengePeriod`, since there is no
outcome yet to challenge. Each subsequent attempt requires fresh evidence
(Section 5) and increments `resolution_attempts`. Once
`MAX_RESOLUTION_ATTEMPTS` (3) is reached without a confidence- and
corroboration-passing Yes/No, the market is cancelled rather than left to
retry indefinitely — bounding the number of retries is itself a liveness
property, since an unbounded retry loop is a liveness failure under a
different name.

**Evidence overflow.** Handled structurally by the circular buffer
(Section 5) — there is no overflow condition left to recover from, since
`submit_evidence` cannot be blocked by capacity.

**Zero winning bettors.** `_finalize_market_result` never marks a market
`Resolved` without first confirming the winning side has a non-zero total;
if it doesn't, the market is auto-cancelled and every bettor recovers funds
through the same `refund_bet` path used for every other cancellation. No
separate "unpayable" state exists because none is needed — cancellation
already has a complete, general-purpose refund mechanism.

**Abandoned disputes.** If a market is `Disputed` and no one ever calls
`request_resolution` again (e.g. the challenger disappears, or evidence
stops being submitted), `last_action_time` — updated on every resolution
attempt and challenge — stops advancing. `cancel_market` permits
cancellation once `now > last_action_time + CANCEL_TIMEOUT_SECONDS`,
independent of who the disputing party was or whether they return.

**Stale markets.** The same `cancel_market` staleness check applies to
`Open` markets that reach `end_time + CANCEL_TIMEOUT_SECONDS` without anyone
ever calling `request_resolution` — an intentionally broad condition (no
exemption for markets with zero evidence) so that any `Open` market can
eventually be cancelled purely on elapsed time, regardless of why
resolution was never requested. `ChallengePeriod` needs no staleness path
of its own since `finalize_resolution` is already unconditionally
permissionless once the window closes.

---

## 9. Known Limitations

This contract's correctness is bounded by the quality of its two external
dependencies, and no on-chain mechanism can fully compensate for either:

- **External web evidence.** Resolution quality depends on the fetched
  pages actually containing relevant, accurate information. Unreachable,
  paywalled, or JavaScript-rendered pages fail closed (recorded as a
  non-`ok` fetch status and effectively excluded from corroboration), which
  is safe but does mean a market can run out of usable evidence sources
  even when qualitatively good evidence exists off-chain but is not
  fetchable by the runtime.

- **LLM reasoning.** The confidence and corroboration checks constrain *how
  much* the contract trusts a model's output, but they do not verify the
  model's reasoning is correct — a confidently wrong, well-corroborated
  answer from multiple sources citing the same underlying error is not
  distinguishable on-chain from a confidently correct one. This is a
  fundamental limitation of AI-assisted resolution, not something this
  contract's architecture claims to solve; the challenge mechanism is the
  intended mitigation, not a formal guarantee.

- **Owner-adjustable confidence threshold.** `set_confidence_threshold` lets
  the owner lower the bar for what counts as a confident resolution. This
  is a deliberately narrow, non-custodial capability (Section 7), but
  reviewers should note it as a parameter under single-key control rather
  than a fully immutable constant.

---

## Conclusion

Every state reachable by this contract has at least one permissionless,
deterministic path to a terminal state with a working payout or refund
mechanism. No sequence of participant actions — or inaction — has been
identified that leaves user funds permanently inaccessible.
