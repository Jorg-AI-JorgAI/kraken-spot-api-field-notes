# Kraken Spot API field notes

Practical notes from running a production trading integration against the Kraken Spot REST API (v0). These are the things that broke for us or surprised us with real orders in a real account, why, and what fixed them. Each one cost us time or a member's trade, and none of them is spelled out clearly in the official docs.

Maintained by the team behind [JorgAI](https://jorgai.com), an auto-trading platform that places trades inside members' own brokerage and exchange accounts. The trader-facing guides are [How to automate crypto trading on Kraken](https://jorgai.com/blog/how-to-automate-crypto-trading-on-kraken) and the permission-by-permission guide to [the Kraken API key form](https://jorgai.com/blog/kraken-api-key-permissions-for-a-trading-bot-2026-which-toggles-to-turn-on-which-to-leave-off-and-why). The Crypto.com Exchange equivalent of these notes is [crypto-com-exchange-api-field-notes](https://github.com/Jorg-AI-JorgAI/crypto-com-exchange-api-field-notes).

Nothing here is affiliated with or endorsed by Kraken. Verify against the current docs at [docs.kraken.com](https://docs.kraken.com/api/) before relying on any detail; Kraken changes things. Items below were verified October 18 to 21, 2026 against the live API and our own account.

## Quick reference

| Item | Value | Notes |
|------|-------|-------|
| Auth | API key + private key | `API-Key` and `API-Sign` headers on a form-encoded POST |
| Signature | base64(HMAC-SHA512(base64decode(private key), path + SHA256(nonce + postdata))) | The nonce goes into the body and the digest |
| Nonce | Always-increasing unsigned integer per key | We use microseconds since the epoch; see trap 2 |
| Permissions a bot needs | Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades, Modify Orders, Cancel/Close Orders, Query Ledger Entries | Never Withdraw Funds; no Deposit, Export Data, WebSockets or Earn |
| IP allow-list | Optional on the key form | Use it when the caller's addresses are fixed |
| Pair names | Legacy key `XXBTZUSD`, altname `XBTUSD`, wsname `XBT/USD` | Requests accept the altname; responses may use any; see trap 4 |
| Asset codes | `XXBT`, `ZUSD`, `XETH`, `SOL` | Bitcoin is `XBT`, Dogecoin is `XDG`; `Assets` gives the altnames |
| Market buy sizing | `volume` in base units | `oflags=viqc` sizes a market buy in the quote currency instead |
| Minimums | `AssetPairs`: `ordermin` (base), `costmin` (quote), `lot_decimals`, `tick_size` | Round down before the order leaves; `ordermin` is a good dust threshold |
| Order types | market, limit, stop-loss, stop-loss-limit, take-profit, take-profit-limit, trailing-stop, trailing-stop-limit | timeinforce GTC, IOC, GTD; `cl_ord_id` for a client id |
| Bracket / OCO | Not available | One conditional close per order at most; see trap 3 |
| Errors | HTTP 200 with `{"error": ["EAPI:Invalid key", ...]}` | Read the array on every response |
| Private call counter | 15 points (starter), 20 (intermediate, pro); most calls 1, ledger and history calls 2; decays 1 point every 3 s (starter), 2 s (intermediate), 1 s (pro) | See trap 1 |
| Order counter | Per pair: 60 (starter), 125 (intermediate), 180 (pro) | A low-frequency bot never approaches it |
| Fees on buys | Taken from the quote currency by default (`fciq`) | The whole bought amount is sellable |
| Sandbox | None for spot | We proved the integration with a tiny live order |

## Trap 1: a three-coin account polled every two minutes burns the starter-tier call counter

Kraken meters private REST calls with a decaying counter: 15 points at the starter verification tier, most calls costing 1, ledger and trade-history calls costing 2, decaying one point every three seconds. That is generous for a human and tight for software that reconciles positions.

The first member to connect a Kraken account held three coins. Our reconciler read balances and trade history for each coin every couple of minutes, plus the scanner's own reads. Within an hour the key had tripped `EAPI:Rate limit exceeded` sixty times. Nothing was wrong with the account; the integration was spending the budget on reads that returned the same answer every time.

**What fixed it:** a short cache in front of the read calls, keyed by API key: balances for 15 seconds, trade history for 60 seconds, dropped the moment an order is placed or cancelled so a fill read-back or a sell-size check never sees stale numbers. See [`examples/kraken_private_call.py`](examples/kraken_private_call.py).

**The user-facing rule that follows:** a "rate limit exceeded" message to the user is useless; the cause is your polling, not their account. Log it as your own error.

## Trap 2: the nonce is per key, so two programs on one key fight each other

Every private request carries a nonce, and Kraken rejects a request whose nonce is lower than the highest it has seen for that key (`EAPI:Invalid nonce`). Two things cause it in practice: a user pasting the same key into a second tool, and your own workers racing each other. Too many invalid nonces earn a temporary ban.

**What fixed it:** microsecond nonces (parallel workers almost never collide), one retry with a fresh nonce on `EAPI:Invalid nonce` before treating it as an error, and never treating a nonce error as an authentication failure. `EAPI:Invalid key`, `EAPI:Invalid signature` and `EGeneral:Permission denied` are the member's key; `EAPI:Invalid nonce` is ordering. The "nonce window" on the key form tolerates small out-of-order arrivals; it helps, it does not fix a shared key.

## Trap 3: there is no bracket order, so every exit lives on your side

Kraken does not link a take-profit to a stop-loss. It accepts a single conditional close on an order (`close[ordertype]`, `close[price]`), which covers one exit, not a pair. If you place both a stop and a target as resting orders, both are live, and when one fills the other remains, now an unhedged order on a position you no longer hold.

**What we do:** place the entry, record the stop and target in our own system, and run the exits from a monitor that evaluates every open position on a short loop, around the clock, because the market does not close. We mark every Kraken entry `bracket_failed=True` internally so the rest of the platform knows the exchange holds no exits. The honest consequence: if your connection to Kraken drops, your exits stop with it, unlike a bracket resting at a stock broker. Design the connection monitoring and the position sizing with that in mind.

## Trap 4: three names for every pair and two odd asset codes

`XXBTZUSD`, `XBTUSD` and `XBT/USD` are the same pair, and responses can use any of them (`AddOrder` accepts the altname; `Balance` keys assets as `XXBT`, `ZUSD`, `XETH`, `SOL`). Bitcoin is `XBT` and Dogecoin is `XDG`. Build one pair table from `AssetPairs` and `Assets` at startup and map every response through it; do not string-match.

## Smaller things worth knowing

- **Errors arrive with HTTP 200.** Parse the `error` array on every response; a status check alone passes failures through.
- **Every call needs a timeout.** A stalled connection with no timeout blocked one of our worker processes for days once; nothing on the trade path should be able to wait forever.
- **Key expiration** on the key form is a free rotation reminder. Rotate by creating the new key first, confirming a balance read, then deleting the old one, so open positions never sit without their exits.
- **Key-level 2FA** (set on Kraken's Security page) requires a one-time code on every private call. Leave it off for a bot and keep 2FA on the login, which Kraken requires before it issues API keys at all.
- **Market data for scanning** does not have to come from Kraken. We size and price orders on Kraken's public `Ticker` and scan on a separate feed, which keeps the private-call budget for orders and reconciliation.
- **Tokenized stocks** on Kraken are not available to US customers and are outside spot automation.

## Who we are

JorgAI runs rules-based automated trading in members' own brokerage and exchange accounts (Schwab, Alpaca, Tradier, tastytrade, Kraken and the Crypto.com Exchange). Related notes: [schwab-trader-api-field-notes](https://github.com/Jorg-AI-JorgAI/schwab-trader-api-field-notes) and [crypto-com-exchange-api-field-notes](https://github.com/Jorg-AI-JorgAI/crypto-com-exchange-api-field-notes). Corrections welcome as issues or pull requests.

## License

MIT. See [LICENSE](LICENSE).
