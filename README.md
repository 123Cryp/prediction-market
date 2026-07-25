# PredictionMarket — a GenLayer Intelligent Contract

A decentralized prediction market where **the resolution itself is decided by AI reading real evidence from the web**, with a built-in **dispute mechanism** so a single bad AI read can be challenged, re-evaluated, and economically punished if it was wrong.

Built for the **Intelligent Contracts** track of the GenLayer campaign.

## Why this is an Intelligent Contract, not just an "LLM wrapper"

A lot of AI-on-chain demos are a single `gl.nondet.exec_prompt()` call wrapped in a contract. This project intentionally goes further:

- **Real, non-trivial on-chain state.** Multiple users, per-user positions (`yes_balances` / `no_balances`), a shared pool, proportional payouts, a bounded evidence list, dispute stakes, resolution reports, and per-address reputation counters — not just "prompt in, answer out."

- **Multi-source AI resolution.** The AI doesn't just read one source and answer once. It reads every stored evidence URL, and its raw output is only accepted if it reports **confidence above a configurable threshold** and **corroboration from at least two independent source domains** — otherwise the contract treats it as `Unclear`, not a finalized answer.

- **An actual economic dispute game.** Anyone can stake GEN to challenge a pending AI outcome during a 1-hour challenge window. If the AI reconfirms its answer, the challenger's stake is forfeited to the platform. If new evidence flips the answer, the challenger is refunded. Up to `MAX_RESOLUTION_ATTEMPTS` rounds are allowed before the market settles.

- **Consensus over non-deterministic output**, using `gl.vm.run_nondet_unsafe` with a custom leader/validator equivalence check. Validators don't need the model's free-text reasoning to match byte-for-byte (which would fail constantly on real LLM output) — they independently re-run the same analysis and only need to agree on the *deterministically derived decision* (outcome + confidence/corroboration check), not the prose that produced it.

- **No permanent fund lock.** Every state a market can be in has a permissionless, deterministic path to either a successful payout or a full refund — including two failure modes that are easy to get wrong:
  - A disputed market whose evidence buffer is full can still accept new evidence (it's a circular buffer, oldest entry evicted) — never a dead end.
  - A resolution nobody bet on the winning side of auto-cancels instead of leaving funds unpayable.

  See `DESIGN.md` for the full architectural reasoning behind these guarantees.

## How it works

```
Open ──(event ends, evidence submitted)──► request_resolution()
                                                    │
                                     AI reads all evidence,
                                     answers Yes / No / Unclear
                                     (subject to confidence +
                                      corroboration checks)
                                                    │
                        ┌───────────────────────────┴──────────────────┐
                        ▼ (Yes/No)                          ▼ (Unclear)
                 ChallengePeriod (1h window)              Disputed
                        │                                     │
              challenge_resolution()               submit new evidence,
                        │                           request_resolution()
                        ▼                            again (up to MAX
                    Disputed ──────────────────────► attempts)
                        │
          finalize_resolution() after window closes
                        │
          ┌─────────────┴─────────────┐
          ▼ (winner has bettors)       ▼ (winner has zero bettors,
      Resolved                          or attempts exhausted, or stale)
   claim_winnings()                          Cancelled
                                          refund_bet()
```

Every non-terminal state also has a permissionless staleness escape hatch (`cancel_market`), so a market can never wait forever on an absent or uncooperative participant.

## Repository layout

- `contracts/PredictionMarket.py` — the contract
- `tests/test_prediction_market.py` — 63-test suite (genlayer-test / Direct Mode)
- `DESIGN.md` — architectural rationale: design goals, threat model, state machine, invariants, security considerations, known limitations

## Testing

```bash
pip install genlayer-test
pytest tests/ -v
```

## License

MIT — see `LICENSE`.
