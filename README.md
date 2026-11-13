# papertrade-client

Unofficial Python client for [papertrade.xyz](https://papertrade.xyz): synthetic BTC/ETH perps on
Hyperliquid's HyperEVM, up to 1000×. It reads market and account state, signs intents, enforces risk
limits before signing, submits through Papertrade's relayer, and checks every day whether the protocol has
changed under it.

> **Papertrade is real money.** Despite the name, positions are funded with real USDC and settle
> against the protocol's LP. This project is not affiliated with Papertrade. Use a dedicated wallet and
> amounts you can lose.

No strategy logic lives here. A strategy imports the client (or drives the CLI) and decides what to trade.

## How it talks to Papertrade

Papertrade has no documented trading API, but its docs allow third-party frontends: they produce the
same EIP-712 intents as papertrade.xyz and send them through Papertrade's relayer, which batches them
on-chain. This client does exactly that.

| Concern | Mechanism |
| --- | --- |
| Market / protocol state | `GET /state/trading`, `/query/protocol/*`, `/query/notices` |
| Account state | server-sent events `GET /state/user/live?wallet=…`: snapshot, patches, 10 s heartbeat |
| Open / close / cancel | `POST /intents {action, signed}`, signed by a **session key** |
| Session key | `RegisterSessionKey` signed by the **wallet**, 30-day expiry |
| Withdraw | `WithdrawToCore` signed by the wallet; amount in 6-decimal USDC |
| Prices | Hyperliquid `l2Book` best bid/ask; Papertrade fills at the BBO mid of the block that executes the batch |
| Deposits | per-user CREATE2 proxy on HyperCore, cross-checked against the factory on-chain |

### Verified against live transactions

The relayer submits every intent through the BatchExecutor contract, so recent HyperEVM blocks are a
free test vector. `papertrade_client.onchain` decodes those batches, re-derives each EIP-712 digest with
this client's encoding, recovers the signer and checks that the resulting intent id equals the one the
relayer recorded. On 2026-11-03 all 433 calls in 1,000 blocks verified, covering every intent type
(`OpenPosition`, `Close`, `RegisterSessionKey`, `WithdrawToCore`, `Stake`, `Unstake`, `Claim`,
`Distribute`). The same check runs in the daily drift monitor, and captured calls of each type are kept
in [`tests/fixtures/onchain_vectors.json`](tests/fixtures/onchain_vectors.json) as an offline regression
test (`scripts/capture_vectors.py` refreshes them).

### Protocol facts worth knowing

- Minimum margin $10, minimum position value (margin × leverage) $500, at most $10M per position and an
  account open-interest cap (live values: `papertrade markets`).
- Market orders only: open, or close in full. No stop-loss or take-profit orders; exits are the client's job.
- Liquidation is a hard bust about 4.8 bps before zero equity and forfeits the whole margin. Since contract
  set 2, your own close at or past the bust price settles as a liquidation too.
- Winning closes pay: deadband (0.2 bps), holdback band, impact haircut (at most 90% kept), 2% win fee.
  The holdback band is 0.75–6 bps on the standard curve and 0.3–10 bps on the off-hours curve that a keeper
  switches on at weekends and quiet hours. `papertrade_client.payout` implements this and reproduces the
  numbers in the docs.
- Only the relayer can submit opens and closes. While it is down positions cannot be closed, but the
  liquidation keeper keeps running.

## Install

```bash
git clone https://github.com/gmatrunich/papertrade-client && cd papertrade-client
uv sync
uv run papertrade markets        # no wallet needed
```

## Configure

```bash
uv run papertrade keygen --out .env   # a fresh wallet used only for Papertrade; written 0600
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `PAPERTRADE_WALLET_PRIVATE_KEY` | required | dedicated wallet; signs session-key registrations and withdrawals |
| `PAPERTRADE_DATA_DIR` | `~/.local/share/papertrade-client` | session keys, nonce counters, kill switch, `audit.jsonl`. Back it up. Every process trading this wallet must share it |
| `PAPERTRADE_LIVE_TRADING` | `false` | `false` makes every signed write a dry run |
| `PAPERTRADE_MAX_MARGIN_USD` | `10` | per-trade margin cap |
| `PAPERTRADE_MAX_LEVERAGE` | `50` | leverage cap |
| `PAPERTRADE_MAX_OPEN_POSITIONS` | `1` | open + pending + submitted-but-not-yet-visible opens |
| `PAPERTRADE_MAX_TOTAL_MARGIN_USD` | `10` | margin across all positions |
| `PAPERTRADE_DAILY_LOSS_LIMIT_USD` | `10` | realised loss that blocks new opens, counted from the first account update seen each UTC day |
| `PAPERTRADE_SNAPSHOT_MAX_AGE_S` | `30` | refuse opens when the account stream has been silent this long |
| `PAPERTRADE_SESSION_RENEW_BEFORE_H` | `72` | `trader.session.maybe_renew()` registers a new key when the active one has less left |
| `PAPERTRADE_EXIT_STOP_FRACTION` | `0.5` | the exit engine closes any position once the mark has covered this share of the entry-to-bust distance |
| `PAPERTRADE_TELEGRAM_BOT_TOKEN`, `PAPERTRADE_TELEGRAM_CHAT_ID` | unset | where `papertrade watchdog` sends alerts; stdout without them |

Export the file before running commands, e.g. `set -a; . ./.env; set +a`.

## Use

```bash
uv run papertrade status                      # account, session, limits, mode
uv run papertrade quote BTC long 10 50        # bust price and payout curve
uv run papertrade open BTC long 10 50         # dry run: guards + an unsigned preview
uv run papertrade open BTC long 10 50 --live  # live only with PAPERTRADE_LIVE_TRADING=true
uv run papertrade open BTC long 10 50 --live --stop 0.5 --take 1 --max-hold 2h   # with an exit plan
uv run papertrade positions
uv run papertrade close-all --live
uv run papertrade close 123 --live --force    # account stream down: sign with the stored session key
uv run papertrade kill-switch on --reason "maintenance"
uv run papertrade probe-pair                  # hedge probe, dry run: guards and the model's cost of the pair
```

```python
from papertrade_client import PapertradeClient

client = PapertradeClient.from_env().start()  # starts the account stream
from papertrade_client.exits import ExitPlan

result = client.trader.open("BTC", "long", "10", 50, exit_plan=ExitPlan(stop_pct=0.5, take_pct=1, max_hold_s=7200))
for submission in result.submissions:
    print(submission.intent_id, client.trader.wait_intent(submission.intent_id, timeout_s=90))

# In a long-running loop, now and then:
client.trader.session.maybe_renew()  # live only; registers a new session key before day 30
```

## Exits

Papertrade has no stop-loss or take-profit orders. `papertrade exits` is a long-running exit engine: once a
second it compares Hyperliquid's mid with every open position and closes the position when a rule fires:

- the **exit plan** attached at open (`exit_plan=` / `--stop`, `--take`, `--max-hold`), stored in `state.json`
  under the intent id, so a strategy process and the engine need not share anything else;
- the **safety stop** for every position, with or without a plan: `PAPERTRADE_EXIT_STOP_FRACTION` of the way
  from entry to bust (at 50× about 1% against you, leaving another 1% for the close to execute).

A close executes in the relayer's next batch, 7 to 46 s after submission on the first live trade, and a close
at or past bust settles as a liquidation, so stops belong well before bust. A close that has not resolved
after 150 s is sent again. In dry-run the engine reports what it would close and sends nothing. It writes a
heartbeat to `exits.json`, which the watchdog checks.

`papertrade probe` is an execution probe, not a strategy: one minimum-size trade ($10 × 50) with a random
side and a 5-minute holding limit, refused unless the exit engine is running live. Run on a timer it measures
latency and fills for a few cents a trade. `papertrade reconcile` matches the audit log with the trade history:
open and close latency, entry slippage against the mid at signing, exit slippage against the mark at the exit
decision, booked payout against `payout.settle`, and anything that does not line up.

### Hedge pairs

`papertrade probe-pair` opens one long and one short of the same market, margin and leverage ($10 × 50 each) and
closes both together after `--hold` (default 5 minutes). It exists to measure, for a few cents a pair, what a
hedge costs and whether it earns $PAPER at the modelled rate (`papertrade_client.trader.pair_cost_curve` and the
`pair_cost_curve` of the dry run give the model's number):

- **Limits.** The pair is held to the same limits as two opens: `PAPERTRADE_MAX_OPEN_POSITIONS` of at least 2 and
  `PAPERTRADE_MAX_TOTAL_MARGIN_USD` of at least 20. Both legs are checked before either is signed, so a refusal
  sends nothing. `probe-pair` without `--live` shows the guards' verdict and the model's pair cost.
- **Sending.** The two open intents are submitted from two threads; the relayer may still execute them in
  different batches. `papertrade reconcile` shows whether they did (`same_entry`, `entry_gap_bps`).
- **Closing.** The exit engine closes both legs in one `Close` intent when either is due (holding limit, safety
  stop), so they execute in one batch at one mark. A pair that has lost a leg (refused, liquidated, not open
  `PAIR_GRACE_S` after its partner) is no longer a hedge: the leg that is left is closed at once. If one open is
  refused after the other was sent, `probe-pair` exits 1 with `pair_broken` and the survivor's plan closes it as
  soon as it shows.
- **Report.** `papertrade reconcile` lists a `pairs` section: entry and exit gap between the legs, whether they
  closed in one transaction, the pair's net PnL against the model, $PAPER minted against the mint basis, and the
  cost per $1 of basis. `basis_per_loss` is 0.98 when the loser's basis is its full loss (less the 2% fee), and
  lower if the winning leg offset it.

## Safety model

- **Dry run by default.** A write is live only when `PAPERTRADE_LIVE_TRADING=true` and the call does not
  ask for a dry run. Dry runs sign with a throwaway key and a random nonce and return the unsigned message.
- **Two keys.** The wallet key signs session-key registrations and withdrawals. Trades are signed by the
  session key in the data directory, which Papertrade lets open and close positions but not withdraw.
- **Guards before signing**: Papertrade's limits (minimums, leverage range, market state, pauses, side and
  account open-interest caps), the limits above, account-stream freshness and session validity.
- **No blind retries.** Every open is recorded as in flight before it is sent and counts against the
  position and margin limits until the account stream shows it (or its one-hour deadline passes). If the
  relayer's answer never arrives the intent is tracked by its id, never resubmitted, so neither a retry
  nor a quick second open can double a position.
- **Shared state.** Processes that share the data directory (a strategy, the CLI, the drift monitor) see
  each other's kill switch and nonces: every update re-reads `state.json` under an exclusive file lock.
- **Kill switch** blocks opens and survives restarts. Closes never depend on it, on market data or on a
  lock held by an open; `close --force` works even when the account stream is down.
- **Audit log**: every live intent is written before it leaves, then again with the relayer's answer.

## Going live

1. `papertrade markets`, `papertrade status`, a dry-run `open`.
2. `papertrade deposit-address`; only if `matches_relayer` is true, send USDC on Hyperliquid to that
   address (at least 10 USDC must arrive; the first deposit pays a 1 USDC activation fee). Wait until
   `status` shows the balance. Only HyperCore transfers to `deposit_address` are credited: USDC sent on
   Arbitrum, Ethereum or HyperEVM, or to the wallet address itself, is not. To deposit from another chain,
   connect the wallet to papertrade.xyz and use Deposit, which bridges to the same address.
3. `PAPERTRADE_LIVE_TRADING=true papertrade session register`, then check that `session` shows an active key.
4. One minimal live trade: `open BTC long 10 50 --live`, then `close-all --live`. Check `audit.jsonl`.

## Protocol drift monitor

```bash
uv run papertrade drift                       # exit 0 ok/info, 1 warn, 2 breaking, 3 could not observe
uv run papertrade drift --kill-on-breaking    # also engage the kill switch in PAPERTRADE_DATA_DIR
```

It compares the live protocol with [`pinned.json`](src/papertrade_client/pinned.json):

| Source | Breaking | Warning |
| --- | --- | --- |
| Frontend bundle | EIP-712 types or domains, intent-id tag, core addresses | new intent types, payout-curve parameters |
| HyperEVM | any proxy implementation upgraded | |
| Relayer batches | a recent call that does not verify with our encoding | no opens/closes seen |
| `/state/trading`, account stream | | frame keys, protocol parameters |
| Docs "Protocol changes" | a new contract set | page updated |

A GitHub Actions workflow runs it daily and opens (or updates) an issue labelled `protocol-drift`.
After adapting the client to a change, re-pin with `papertrade drift --pin` and commit `pinned.json`.

## Running on a server

`deploy/install.sh` installs systemd user units on Linux. All read `~/.config/papertrade/env` (mode 0600)
and share the default data directory:

| Unit | Runs | Does |
| --- | --- | --- |
| `papertrade-exits` | always (service) | `papertrade exits`; restarted by `install.sh` so it picks up code and env changes |
| `papertrade-drift` | hourly | `papertrade drift --kill-on-breaking`, report saved as `drift.json` in the data directory |
| `papertrade-watchdog` | every 2 minutes | `papertrade watchdog`: alerts to Telegram when a problem appears or clears |
| `papertrade-reconcile` | daily, 07:10 UTC | `papertrade reconcile --since 24h`, digest to Telegram |
| `papertrade-probe` | about once an hour (at least 10 min apart) | `papertrade probe --live`; installed but **not enabled** by `install.sh`. `probe-pair` has no timer: run it by hand |

The watchdog reports: account stream unavailable or silent, kill switch engaged, Papertrade paused, session
key missing or expiring, an open accepted by the relayer but not on the stream after 5 minutes, a position
with less than 30% of the entry-to-bust distance left, drift warnings, a drift monitor that has not run or
not observed the protocol for 3 hours, and an exit engine that is silent, missing while positions are open,
or still dry-run while trading is live. Papertrade's API drops out for a few minutes several times a day, so
upstream errors alert only when they last 5 minutes. A daily summary at 07:00 UTC doubles as a
heartbeat; `papertrade watchdog --test` sends it now.

```bash
git clone https://github.com/gmatrunich/papertrade-client && cd papertrade-client
mkdir -p -m 700 ~/.config/papertrade
uv run papertrade keygen --out ~/.config/papertrade/env
grep -v PRIVATE_KEY .env.example >> ~/.config/papertrade/env   # then set the Telegram variables
deploy/install.sh                                              # again after every `git pull`
journalctl --user -u papertrade-watchdog -n 20
```

## Development

```bash
uv run pytest -m "not network"   # offline
uv run pytest -m network         # read-only checks against the live endpoints
uv run python scripts/capture_vectors.py --out tests/fixtures/onchain_vectors.json
```

## License

MIT
