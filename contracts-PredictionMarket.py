# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json
import datetime


class PredictionMarket(gl.Contract):

    # --- core market data ---
    questions: TreeMap[str, str]
    creators: TreeMap[str, str]
    end_times: TreeMap[str, u256]
    statuses: TreeMap[str, str]
    results: TreeMap[str, bool]

    # --- resolution state machine ---
    pending_outcomes: TreeMap[str, str]
    challenge_end_times: TreeMap[str, u256]
    resolution_attempts: TreeMap[str, u256]
    evidence_count_at_last_resolution: TreeMap[str, u256]

    # --- evidence (crowd-sourced) ---
    evidence_json: TreeMap[str, str]
    evidence_count: TreeMap[str, u256]

    # --- positions / pool ---
    total_yes: TreeMap[str, u256]
    total_no: TreeMap[str, u256]
    total_pool: TreeMap[str, u256]

    yes_balances: TreeMap[str, u256]
    no_balances: TreeMap[str, u256]

    claimed: TreeMap[str, bool]

    # --- disputes ---
    active_challenger: TreeMap[str, str]
    challenge_stakes: TreeMap[str, u256]

    # --- platform ---
    market_count: u256
    fee_collected: u256
    owner: str


    VALID_OUTCOMES = ("Yes", "No", "Unclear")

    MAX_EVIDENCE = 10
    CHALLENGE_WINDOW_SECONDS = 3600          # 1 hour to dispute a pending outcome
    MAX_RESOLUTION_ATTEMPTS = 3              # cap AI resolution rounds per market
    CANCEL_TIMEOUT_SECONDS = 604800          # 7 days with no evidence -> cancellable
    PLATFORM_FEE_BPS = 200                   # 2% fee on winnings


    def __init__(self):
        self.market_count = u256(0)
        self.fee_collected = u256(0)
        self.owner = str(gl.message.sender_address)


    def _key(self, market_id, user):
        return f"{market_id}:{user}"


    def _now(self):
        return int(datetime.datetime.now().timestamp())


    def _pay(self, recipient: str, amount):
        # sends GEN to any address (EOA or contract) via a "nameless" transfer
        if amount > 0:
            gl.ContractAt(Address(recipient)).emit_transfer(value=amount)


    # ------------------------------------------------------------------
    # Market creation & betting
    # ------------------------------------------------------------------

    @gl.public.write
    def create_market(self, question: str, end_time: int):

        market_id = str(int(self.market_count))

        self.questions[market_id] = question
        self.creators[market_id] = str(gl.message.sender_address)
        self.end_times[market_id] = u256(end_time)
        self.statuses[market_id] = "Open"

        self.total_yes[market_id] = u256(0)
        self.total_no[market_id] = u256(0)
        self.total_pool[market_id] = u256(0)

        self.evidence_count[market_id] = u256(0)
        self.resolution_attempts[market_id] = u256(0)
        self.evidence_count_at_last_resolution[market_id] = u256(0)

        self.market_count = u256(int(self.market_count) + 1)

        return market_id


    @gl.public.write.payable
    def buy_yes(self, market_id: str):
        self._buy(market_id, True)


    @gl.public.write.payable
    def buy_no(self, market_id: str):
        self._buy(market_id, False)


    def _buy(self, market_id, yes):

        if self.statuses.get(market_id, "") != "Open":
            raise Exception("Market closed")

        if self._now() >= self.end_times[market_id]:
            raise Exception("Betting period has ended")

        if gl.message.value == 0:
            raise Exception("Send tokens")

        user = str(gl.message.sender_address)
        key = self._key(market_id, user)
        amount = gl.message.value

        if yes:
            self.yes_balances[key] = self.yes_balances.get(key, u256(0)) + amount
            self.total_yes[market_id] = self.total_yes.get(market_id, u256(0)) + amount
        else:
            self.no_balances[key] = self.no_balances.get(key, u256(0)) + amount
            self.total_no[market_id] = self.total_no.get(market_id, u256(0)) + amount

        self.total_pool[market_id] = self.total_pool.get(market_id, u256(0)) + amount


    # ------------------------------------------------------------------
    # Crowd-sourced evidence
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_evidence(self, market_id: str, url: str):

        status = self.statuses.get(market_id, "")
        if status not in ("Open", "ChallengePeriod", "Disputed"):
            raise Exception("Market not accepting evidence")

        if self._now() < self.end_times[market_id]:
            raise Exception("Event has not ended yet")

        count = int(self.evidence_count.get(market_id, u256(0)))
        if count >= self.MAX_EVIDENCE:
            raise Exception("Evidence limit reached")

        existing = self.evidence_json.get(market_id, "[]")
        items = json.loads(existing)
        items.append({
            "submitter": str(gl.message.sender_address),
            "url": url,
        })

        self.evidence_json[market_id] = json.dumps(items)
        self.evidence_count[market_id] = u256(count + 1)


    # ------------------------------------------------------------------
    # AI resolution (can run multiple rounds if disputed)
    # ------------------------------------------------------------------

    @gl.public.write
    def request_resolution(self, market_id: str):

        status = self.statuses.get(market_id, "")
        if status not in ("Open", "Disputed"):
            raise Exception("Market not awaiting resolution")

        if self._now() < self.end_times[market_id]:
            raise Exception("Market not ended")

        count = int(self.evidence_count.get(market_id, u256(0)))
        if count == 0:
            raise Exception("No evidence submitted yet")

        last_count = int(self.evidence_count_at_last_resolution.get(market_id, u256(0)))
        if status == "Disputed" and count <= last_count:
            raise Exception("Submit new evidence before re-resolving")

        attempts = int(self.resolution_attempts.get(market_id, u256(0)))
        if attempts >= self.MAX_RESOLUTION_ATTEMPTS:
            raise Exception("Resolution attempts exhausted, cancel the market instead")

        question = self.questions[market_id]
        items = json.loads(self.evidence_json.get(market_id, "[]"))
        urls = [item["url"] for item in items]


        def nondet():

            excerpts = []
            for i, url in enumerate(urls):
                try:
                    content = gl.nondet.web.render(url, mode="text")
                except Exception:
                    content = ""
                excerpts.append(f"Source {i + 1} ({url}):\n{content[:1500]}")

            combined_evidence = "\n\n".join(excerpts)[:6000]

            prompt = f"""
Prediction market question:

{question}

You are given evidence collected from multiple independent sources below.
Weigh all sources together and decide the real-world outcome.

{combined_evidence}

Return only one word:
Yes
No
Unclear
"""

            raw = gl.nondet.exec_prompt(prompt, response_format="text")
            answer = raw.strip().splitlines()[0].strip()

            for option in self.VALID_OUTCOMES:
                if option.lower() == answer.lower():
                    return option

            return "Unclear"


        outcome = gl.eq_principle.strict_eq(nondet)

        self.resolution_attempts[market_id] = u256(attempts + 1)
        self.evidence_count_at_last_resolution[market_id] = u256(count)

        if status == "Open":
            self._apply_first_resolution(market_id, outcome)
        else:
            self._apply_dispute_resolution(market_id, outcome)

        return outcome


    def _apply_first_resolution(self, market_id, outcome):

        if outcome in ("Yes", "No"):
            self.pending_outcomes[market_id] = outcome
            self.statuses[market_id] = "ChallengePeriod"
            self.challenge_end_times[market_id] = u256(
                self._now() + self.CHALLENGE_WINDOW_SECONDS
            )
        else:
            self.statuses[market_id] = "Disputed"


    def _apply_dispute_resolution(self, market_id, outcome):

        challenger = self.active_challenger.get(market_id, "")
        stake_key = self._key(market_id, challenger) if challenger else ""
        stake = self.challenge_stakes.get(stake_key, u256(0)) if stake_key else u256(0)
        attempts = int(self.resolution_attempts.get(market_id, u256(0)))
        attempts_left = attempts < self.MAX_RESOLUTION_ATTEMPTS

        previous_outcome = self.pending_outcomes.get(market_id, "")

        if outcome == previous_outcome and outcome in ("Yes", "No"):
            # challenge failed: outcome confirmed, stake is forfeited to the pool
            if stake > 0:
                self.total_pool[market_id] = self.total_pool.get(market_id, u256(0)) + stake

            if attempts_left:
                self.statuses[market_id] = "ChallengePeriod"
                self.challenge_end_times[market_id] = u256(
                    self._now() + self.CHALLENGE_WINDOW_SECONDS
                )
            else:
                self.results[market_id] = (outcome == "Yes")
                self.statuses[market_id] = "Resolved"

        elif outcome in ("Yes", "No"):
            # challenge succeeded: outcome flipped, refund the challenger
            if stake > 0:
                self._pay(challenger, stake)

            self.pending_outcomes[market_id] = outcome

            if attempts_left:
                self.statuses[market_id] = "ChallengePeriod"
                self.challenge_end_times[market_id] = u256(
                    self._now() + self.CHALLENGE_WINDOW_SECONDS
                )
            else:
                self.results[market_id] = (outcome == "Yes")
                self.statuses[market_id] = "Resolved"

        else:
            # still unclear
            if stake > 0:
                self._pay(challenger, stake)

            if attempts_left:
                self.statuses[market_id] = "Disputed"
            else:
                self.statuses[market_id] = "Cancelled"

        if stake_key:
            self.challenge_stakes[stake_key] = u256(0)
        self.active_challenger[market_id] = ""


    @gl.public.write.payable
    def challenge_resolution(self, market_id: str):

        if self.statuses.get(market_id, "") != "ChallengePeriod":
            raise Exception("Market is not in a challenge period")

        if self._now() >= self.challenge_end_times[market_id]:
            raise Exception("Challenge window has closed")

        if gl.message.value == 0:
            raise Exception("A stake is required to challenge")

        if self.active_challenger.get(market_id, "") != "":
            raise Exception("Market already has an active challenge")

        user = str(gl.message.sender_address)
        self.active_challenger[market_id] = user
        self.challenge_stakes[self._key(market_id, user)] = gl.message.value
        self.statuses[market_id] = "Disputed"


    @gl.public.write
    def finalize_resolution(self, market_id: str):

        if self.statuses.get(market_id, "") != "ChallengePeriod":
            raise Exception("Market is not awaiting finalization")

        if self._now() < self.challenge_end_times[market_id]:
            raise Exception("Challenge window still open")

        outcome = self.pending_outcomes[market_id]
        self.results[market_id] = (outcome == "Yes")
        self.statuses[market_id] = "Resolved"


    # ------------------------------------------------------------------
    # Cancellation & refunds (unresolvable markets)
    # ------------------------------------------------------------------

    @gl.public.write
    def cancel_market(self, market_id: str):

        status = self.statuses.get(market_id, "")

        stale_open = (
            status == "Open"
            and int(self.evidence_count.get(market_id, u256(0))) == 0
            and self._now() > self.end_times[market_id] + self.CANCEL_TIMEOUT_SECONDS
        )

        exhausted = (
            status == "Disputed"
            and int(self.resolution_attempts.get(market_id, u256(0))) >= self.MAX_RESOLUTION_ATTEMPTS
        )

        if not (stale_open or exhausted):
            raise Exception("Market is not eligible for cancellation")

        self.statuses[market_id] = "Cancelled"


    @gl.public.write
    def refund_bet(self, market_id: str):

        if self.statuses.get(market_id, "") != "Cancelled":
            raise Exception("Market is not cancelled")

        user = str(gl.message.sender_address)
        key = self._key(market_id, user)

        if self.claimed.get(key, False):
            raise Exception("Already refunded")

        amount = (
            self.yes_balances.get(key, u256(0))
            + self.no_balances.get(key, u256(0))
        )

        if amount == 0:
            raise Exception("Nothing to refund")

        self.claimed[key] = True
        self._pay(user, amount)


    # ------------------------------------------------------------------
    # Claiming winnings
    # ------------------------------------------------------------------

    @gl.public.write
    def claim_winnings(self, market_id: str):

        if self.statuses.get(market_id, "") != "Resolved":
            raise Exception("Not resolved")

        user = str(gl.message.sender_address)
        key = self._key(market_id, user)

        if self.claimed.get(key, False):
            raise Exception("Already claimed")

        if self.results[market_id]:
            user_share = self.yes_balances.get(key, u256(0))
            total = self.total_yes[market_id]
        else:
            user_share = self.no_balances.get(key, u256(0))
            total = self.total_no[market_id]

        if user_share == 0:
            raise Exception("No winning shares")

        gross_reward = (user_share * self.total_pool[market_id]) // total
        fee = (gross_reward * u256(self.PLATFORM_FEE_BPS)) // u256(10000)
        reward = gross_reward - fee

        self.claimed[key] = True
        self.fee_collected = self.fee_collected + fee

        self._pay(user, reward)


    @gl.public.write
    def withdraw_platform_fees(self):

        if str(gl.message.sender_address) != self.owner:
            raise Exception("Only owner")

        amount = self.fee_collected
        if amount == 0:
            raise Exception("Nothing to withdraw")

        self.fee_collected = u256(0)
        self._pay(self.owner, amount)


    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def get_market(self, market_id: str):

        return json.dumps({
            "question": self.questions.get(market_id, ""),
            "creator": self.creators.get(market_id, ""),
            "status": self.statuses.get(market_id, ""),
            "end_time": int(self.end_times.get(market_id, 0)),
            "yes": int(self.total_yes.get(market_id, u256(0))),
            "no": int(self.total_no.get(market_id, u256(0))),
            "pool": int(self.total_pool.get(market_id, u256(0))),
            "evidence_count": int(self.evidence_count.get(market_id, u256(0))),
            "pending_outcome": self.pending_outcomes.get(market_id, ""),
            "challenge_end_time": int(self.challenge_end_times.get(market_id, 0)),
            "resolution_attempts": int(self.resolution_attempts.get(market_id, u256(0))),
        })


    @gl.public.view
    def get_evidence(self, market_id: str):
        return self.evidence_json.get(market_id, "[]")


    @gl.public.view
    def get_position(self, market_id: str, user: str):

        key = self._key(market_id, user)
        return json.dumps({
            "yes_balance": int(self.yes_balances.get(key, u256(0))),
            "no_balance": int(self.no_balances.get(key, u256(0))),
            "claimed": self.claimed.get(key, False),
        })


    @gl.public.view
    def total_markets(self):
        return int(self.market_count)
