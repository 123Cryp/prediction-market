# PredictionMarket — a GenLayer Intelligent Contract

A decentralized prediction market where **the resolution itself is decided by AI reading
real evidence from the web**, with a built-in **dispute mechanism** so a single bad AI
read can be challenged, re-evaluated, and economically punished if it was wrong.

Built for the **Intelligent Contracts** track of the GenLayer campaign.

## Why this is an Intelligent Contract, not just an "LLM wrapper"

A lot of AI-on-chain demos are a single `gl.nondet.exec_prompt()` call wrapped in a
contract. This project intentionally goes further:

- **Real, non-trivial on-chain state.** Multiple users, per-user positions (`yes_balances`
  / `no_balances`), a shared pool, proportional payouts, evidence lists, dispute stakes —
  not just "prompt in, answer out."
- **Multi-round AI resolution.** The AI doesn't just answer once. It reads a
  crowd-sourced set of evidence URLs, and if its answer is challenged, it re-reads an
  *expanded* evidence set and answers again — up to `MAX_RESOLUTION_ATTEMPTS` times.
- **An actual economic dispute game.** Anyone can stake GEN to challenge a pending AI
  outcome. If the AI reconfirms its answer, the challenger's stake is forfeited to the
  winners' pool. If new evidence flips the AI's answer, the challenger is refunded. This
  is a genuine incentive mechanism, not decoration.
- **Consensus over non-deterministic output**, using `gl.eq_principle.strict_eq` so
  GenLayer's validator set has to agree on the same classified outcome before it's
  accepted on-chain.
- **Failure paths that matter.** Markets can be cancelled and refunded if no evidence
  ever appears, or if the AI can't reach a confident answer even after the maximum
  number of resolution attempts.

## How it works

```
Open  ──(event ends, evidence submitted)──►  request_resolution()
  │                                                  │
  │                                          AI reads all evidence,
  │                                          answers Yes / No / Unclear
  │                                                  │
  │                                                  ▼
  │                                          ChallengePeriod (1h window)
  │                                                  │
  │                              ┌───────────────────┴───────────────────┐
  │                     no challenge, window expires                 someone stakes
  │                              │                                  to challenge
  │                              ▼                                       │
  │                          Resolved                                Disputed
  │                    (claim_winnings enabled)                          │
  │                                                          new evidence required,
  │                                                          request_resolution() again
  │                                                                       │
  │                                          ┌────────────────────────────┤
  │                                   same outcome                 outcome flips
  │                                   (challenge fails,             (challenge wins,
  │                                    stake forfeited              stake refunded)
  │                                    to the pool)                       │
  │                                          └──────────► back to ChallengePeriod
  │                                                        (up to MAX_RESOLUTION_ATTEMPTS)
  │
  └──(7 days pass, still no evidence)──► Cancelled ──► refund_bet() for everyone
```

### Contract methods

| Method | Type | Description |
|---|---|---|
| `create_market(question, end_time)` | write | Opens a new market |
| `buy_yes(market_id)` / `buy_no(market_id)` | write, payable | Take a position before `end_time` |
| `submit_evidence(market_id, url)` | write | Crowd-source a source for the AI to read (max 10) |
| `request_resolution(market_id)` | write | AI reads all evidence and classifies Yes/No/Unclear |
| `challenge_resolution(market_id)` | write, payable | Stake GEN to dispute the pending outcome |
| `finalize_resolution(market_id)` | write | Locks in the outcome once the challenge window passes |
| `claim_winnings(market_id)` | write | Proportional payout to winners, minus a 2% platform fee |
| `cancel_market(market_id)` / `refund_bet(market_id)` | write | Escape hatch for stale/unresolvable markets |
| `get_market` / `get_evidence` / `get_position` / `total_markets` | view | Read state |

## Project structure

```
contracts/PredictionMarket.py   # the Intelligent Contract
tests/test_prediction_market.py # genlayer-test (Direct Mode) test suite
gltest.config.yaml              # test runner config
```

## Running it

**Deploy in GenLayer Studio**
1. Import `contracts/PredictionMarket.py`
2. Compile
3. Deploy (no constructor arguments needed)

**Run the test suite locally**
```bash
pip install genlayer-test
gltest
```
The tests run in Direct Mode (in-process, no Docker/Studio needed) and cover market
creation, betting windows, evidence submission, AI resolution, the full dispute/
re-resolution flow, payouts with the platform fee, and cancellation/refunds.

## Tech notes / gotchas we hit building this

- State fields can't be plain Python `int` — GenVM requires a sized type like `u256`.
- There's no `gl.block.timestamp`; the deterministic "now" inside a contract is
  `datetime.datetime.now()`.
- Sending GEN to any address (EOA or contract) is done via
  `gl.ContractAt(Address(recipient)).emit_transfer(value=amount)`, and any method that
  needs `gl.message.value` must be decorated `@gl.public.write.payable`.

## License

MIT
