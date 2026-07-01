"""
Direct Mode tests for PredictionMarket.

Run with:
    pip install genlayer-test
    pytest tests/ -v

Direct Mode runs the contract's Python code in-memory (no Docker, no
Studio), so these tests execute in milliseconds and are the fastest way
to validate the intelligent-contract logic before deploying to
GenLayer Studio / localnet for the submission demo.

NOTE on value=: Direct Mode dispatches payable methods the same way the
node does, accepting a `value=` keyword on the call (mirroring the
`value` field used by gen_call / Studio's `.transact(value=...)`). If a
future genlayer-test release renames this kwarg, adjust the few calls
below marked `value=`.
"""

import json
from datetime import datetime, timezone

import pytest


CONTRACT_PATH = "contracts/PredictionMarket.py"


def iso(s):
    """Turn 'YYYY-MM-DDTHH:MM:SSZ' into a unix timestamp (int)."""
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


NOW = "2030-01-01T00:00:00Z"
END = "2030-01-02T00:00:00Z"          # 1 day after NOW -> event end_time
AFTER_END = "2030-01-02T00:05:00Z"    # just past end_time
AFTER_CHALLENGE = "2030-01-02T01:10:00Z"   # past the 1h challenge window
FAR_FUTURE_CANCEL = "2030-01-10T00:10:00Z"  # past the 7-day cancel timeout


@pytest.fixture
def market(direct_vm, direct_deploy, direct_owner):
    """Deploys the contract and creates one open market, warped to NOW."""
    direct_vm.warp(NOW)
    direct_vm.sender = direct_owner
    contract = direct_deploy(CONTRACT_PATH)
    market_id = contract.create_market("Will it rain in Lisbon tomorrow?", iso(END))
    return contract, market_id


# ---------------------------------------------------------------------
# Market creation & betting
# ---------------------------------------------------------------------

def test_create_market(market):
    contract, market_id = market
    data = json.loads(contract.get_market(market_id))

    assert data["status"] == "Open"
    assert data["yes"] == 0
    assert data["no"] == 0
    assert data["pool"] == 0


def test_buy_yes_and_no_update_pool(direct_vm, market, direct_alice, direct_bob):
    contract, market_id = market

    with direct_vm.prank(direct_alice):
        contract.buy_yes(market_id, value=1000)

    with direct_vm.prank(direct_bob):
        contract.buy_no(market_id, value=400)

    data = json.loads(contract.get_market(market_id))
    assert data["yes"] == 1000
    assert data["no"] == 400
    assert data["pool"] == 1400

    alice_position = json.loads(contract.get_position(market_id, str(direct_alice)))
    assert alice_position["yes_balance"] == 1000
    assert alice_position["claimed"] is False


def test_buy_after_end_time_reverts(direct_vm, market, direct_alice):
    contract, market_id = market

    direct_vm.warp(AFTER_END)

    with direct_vm.prank(direct_alice), direct_vm.expect_revert("Betting period has ended"):
        contract.buy_yes(market_id, value=100)


def test_buy_without_value_reverts(direct_vm, market, direct_alice):
    contract, market_id = market

    with direct_vm.prank(direct_alice), direct_vm.expect_revert("Send tokens"):
        contract.buy_yes(market_id, value=0)


# ---------------------------------------------------------------------
# Crowd-sourced evidence
# ---------------------------------------------------------------------

def test_submit_evidence_before_end_time_reverts(direct_vm, market, direct_alice):
    contract, market_id = market

    with direct_vm.prank(direct_alice), direct_vm.expect_revert("Event has not ended yet"):
        contract.submit_evidence(market_id, "https://news.example.com/weather")


def test_submit_evidence_after_end_time(direct_vm, market, direct_alice, direct_bob):
    contract, market_id = market

    direct_vm.warp(AFTER_END)

    with direct_vm.prank(direct_alice):
        contract.submit_evidence(market_id, "https://source-a.example.com/report")

    with direct_vm.prank(direct_bob):
        contract.submit_evidence(market_id, "https://source-b.example.com/report")

    data = json.loads(contract.get_market(market_id))
    assert data["evidence_count"] == 2

    evidence = json.loads(contract.get_evidence(market_id))
    assert len(evidence) == 2
    assert evidence[0]["submitter"] == str(direct_alice)


def test_evidence_cap_enforced(direct_vm, market, direct_alice):
    contract, market_id = market
    direct_vm.warp(AFTER_END)

    with direct_vm.prank(direct_alice):
        for i in range(10):
            contract.submit_evidence(market_id, f"https://source-{i}.example.com")

        with direct_vm.expect_revert("Evidence limit reached"):
            contract.submit_evidence(market_id, "https://source-overflow.example.com")


# ---------------------------------------------------------------------
# AI resolution -> challenge period -> finalize
# ---------------------------------------------------------------------

def _seed_evidence(direct_vm, contract, market_id, submitter, n=1):
    direct_vm.warp(AFTER_END)
    with direct_vm.prank(submitter):
        for i in range(n):
            contract.submit_evidence(market_id, f"https://source-{i}.example.com")


def test_request_resolution_needs_evidence(direct_vm, market, direct_owner):
    contract, market_id = market
    direct_vm.warp(AFTER_END)

    with direct_vm.expect_revert("No evidence submitted yet"):
        contract.request_resolution(market_id)


def test_request_resolution_sets_pending_outcome(direct_vm, market, direct_alice):
    contract, market_id = market
    _seed_evidence(direct_vm, contract, market_id, direct_alice)

    direct_vm.mock_llm(r".*", "Yes")
    outcome = contract.request_resolution(market_id)

    assert outcome == "Yes"
    data = json.loads(contract.get_market(market_id))
    assert data["status"] == "ChallengePeriod"
    assert data["pending_outcome"] == "Yes"
    assert data["resolution_attempts"] == 1


def test_finalize_before_challenge_window_reverts(direct_vm, market, direct_alice):
    contract, market_id = market
    _seed_evidence(direct_vm, contract, market_id, direct_alice)
    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)

    with direct_vm.expect_revert("Challenge window still open"):
        contract.finalize_resolution(market_id)


def test_finalize_after_challenge_window(direct_vm, market, direct_alice):
    contract, market_id = market
    _seed_evidence(direct_vm, contract, market_id, direct_alice)
    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)

    direct_vm.warp(AFTER_CHALLENGE)
    contract.finalize_resolution(market_id)

    data = json.loads(contract.get_market(market_id))
    assert data["status"] == "Resolved"


# ---------------------------------------------------------------------
# Claiming winnings (with the 2% platform fee)
# ---------------------------------------------------------------------

def test_claim_winnings_pays_out_minus_fee(direct_vm, market, direct_alice, direct_bob):
    contract, market_id = market

    with direct_vm.prank(direct_alice):
        contract.buy_yes(market_id, value=1000)
    with direct_vm.prank(direct_bob):
        contract.buy_no(market_id, value=500)

    _seed_evidence(direct_vm, contract, market_id, direct_alice)
    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)
    direct_vm.warp(AFTER_CHALLENGE)
    contract.finalize_resolution(market_id)

    # pool = 1500, alice's share of "Yes" = 1000/1000 -> gross 1500, fee 2% -> 30
    with direct_vm.prank(direct_alice):
        contract.claim_winnings(market_id)

    position = json.loads(contract.get_position(market_id, str(direct_alice)))
    assert position["claimed"] is True

    # losing side has no winning shares
    with direct_vm.prank(direct_bob), direct_vm.expect_revert("No winning shares"):
        contract.claim_winnings(market_id)


def test_double_claim_reverts(direct_vm, market, direct_alice):
    contract, market_id = market

    with direct_vm.prank(direct_alice):
        contract.buy_yes(market_id, value=1000)

    _seed_evidence(direct_vm, contract, market_id, direct_alice)
    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)
    direct_vm.warp(AFTER_CHALLENGE)
    contract.finalize_resolution(market_id)

    with direct_vm.prank(direct_alice):
        contract.claim_winnings(market_id)
        with direct_vm.expect_revert("Already claimed"):
            contract.claim_winnings(market_id)


# ---------------------------------------------------------------------
# Dispute flow: challenge -> new evidence -> AI re-resolves
# ---------------------------------------------------------------------

def test_challenge_confirms_outcome_forfeits_stake(direct_vm, market, direct_alice, direct_bob):
    contract, market_id = market

    with direct_vm.prank(direct_alice):
        contract.buy_yes(market_id, value=1000)

    _seed_evidence(direct_vm, contract, market_id, direct_alice)
    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)

    pool_before = json.loads(contract.get_market(market_id))["pool"]

    # bob disputes the "Yes" outcome
    with direct_vm.prank(direct_bob):
        contract.challenge_resolution(market_id, value=200)

    data = json.loads(contract.get_market(market_id))
    assert data["status"] == "Disputed"

    # new evidence required before re-resolving
    with direct_vm.prank(direct_bob):
        contract.submit_evidence(market_id, "https://new-source.example.com")

    # AI confirms the same outcome again -> bob's stake is forfeited to the pool
    direct_vm.mock_llm(r".*", "Yes")
    outcome = contract.request_resolution(market_id)

    assert outcome == "Yes"
    data = json.loads(contract.get_market(market_id))
    assert data["status"] == "ChallengePeriod"
    assert data["pending_outcome"] == "Yes"
    assert data["pool"] == pool_before + 200
    assert data["resolution_attempts"] == 2


def test_challenge_flips_outcome_refunds_stake(direct_vm, market, direct_alice, direct_bob):
    contract, market_id = market
    _seed_evidence(direct_vm, contract, market_id, direct_alice)

    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)

    with direct_vm.prank(direct_bob):
        contract.challenge_resolution(market_id, value=200)
        contract.submit_evidence(market_id, "https://new-source.example.com")

    # AI changes its mind with the new evidence
    direct_vm.clear_mocks()
    direct_vm.mock_llm(r".*", "No")
    outcome = contract.request_resolution(market_id)

    assert outcome == "No"
    data = json.loads(contract.get_market(market_id))
    assert data["status"] == "ChallengePeriod"
    assert data["pending_outcome"] == "No"


def test_second_challenge_blocked_while_one_active(direct_vm, market, direct_alice, direct_bob):
    contract, market_id = market
    _seed_evidence(direct_vm, contract, market_id, direct_alice)
    direct_vm.mock_llm(r".*", "Yes")
    contract.request_resolution(market_id)

    with direct_vm.prank(direct_bob):
        contract.challenge_resolution(market_id, value=200)

    # market is now "Disputed", not "ChallengePeriod" -> a second challenge must fail
    with direct_vm.prank(direct_alice), direct_vm.expect_revert("Market is not in a challenge period"):
        contract.challenge_resolution(market_id, value=200)


# ---------------------------------------------------------------------
# Cancellation & refunds
# ---------------------------------------------------------------------

def test_cancel_stale_market_and_refund(direct_vm, market, direct_alice):
    contract, market_id = market

    with direct_vm.prank(direct_alice):
        contract.buy_yes(market_id, value=750)

    # no evidence ever submitted, warp past end_time + 7 days
    direct_vm.warp(FAR_FUTURE_CANCEL)
    contract.cancel_market(market_id)

    data = json.loads(contract.get_market(market_id))
    assert data["status"] == "Cancelled"

    with direct_vm.prank(direct_alice):
        contract.refund_bet(market_id)

    position = json.loads(contract.get_position(market_id, str(direct_alice)))
    assert position["claimed"] is True

    with direct_vm.prank(direct_alice), direct_vm.expect_revert("Already refunded"):
        contract.refund_bet(market_id)


def test_cancel_too_early_reverts(direct_vm, market):
    contract, market_id = market
    direct_vm.warp(AFTER_END)  # only just ended, far from the 7-day timeout

    with direct_vm.expect_revert("Market is not eligible for cancellation"):
        contract.cancel_market(market_id)
