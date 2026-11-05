# How the Trading Agent Works

> A plain-language guide to what the system does, how it decides, and what you can change. Its design as a multi-agent system is in [MAS-DESIGN.md](MAS-DESIGN.md).
> It describes the code as of 2026-10-05 (milestones M0–M9 and the M10 preparation are built).
> [CONCEPT.md](CONCEPT.md) explains why the system is designed this way. [IMPLEMENTATION.md](IMPLEMENTATION.md) explains how it is built.
> Not financial or tax advice.

---

## Contents

1. [The short version](#1-the-short-version)
2. [Trading words used in this guide](#2-trading-words-used-in-this-guide)
3. [The big picture](#3-the-big-picture)
4. [A day in the life](#4-a-day-in-the-life)
5. [End to end: from first data to a trade](#5-end-to-end-from-first-data-to-a-trade)
6. [When does it buy?](#6-when-does-it-buy)
7. [When does it sell?](#7-when-does-it-sell)
8. [The multi-agent system](#8-the-multi-agent-system)
9. [Does it work?](#9-does-it-work)
10. [Safety nets](#10-safety-nets)
11. [Your part: Telegram, dashboard, daily routine](#11-your-part-telegram-dashboard-daily-routine)
12. [Settings you can change](#12-settings-you-can-change)
13. [What the system does not do (yet)](#13-what-the-system-does-not-do-yet)

---

## 1. The short version

- The system is a **robot swing trader** for one personal account. It buys stocks it expects to rise over the next few days to three weeks, then sells them again. It never bets on falling prices (no short selling), never borrows money, and doesn't trade options.
- It watches about **140 large, liquid stocks**: the S&P 100 (US) and the DAX 40 (Germany).
- **Plain code** finds candidates with a fixed rule: "a stock in an uptrend that has dipped a little". A **News agent** adds stocks with a strong positive news catalyst. **AI models** (LLMs) then study each candidate, argue about it, and propose a trade or pass. The News agent also watches held stocks and sells one when the news breaks the reason it was bought.
- **Plain code has the final say.** A risk engine checks every proposal against hard limits and decides how many shares to buy. The AI can't place orders, choose the size, or invent prices.
- Every trade is placed with its exit already attached: a **stop** that sells if the price falls to a set level, and a **target** that sells if the price rises to a set level. They sit at the broker, so they still work if the computer is off.
- It runs on **paper** (simulated money) first. Real money (€1,000, US stocks only) comes only once the paper results beat a simple rule-based strategy after all costs, over at least 3 months and 50 trades.
- You stay in control from your phone. Telegram brings a morning briefing, alerts and an evening digest. Commands like `/pause`, `/stop` and `/exit` let you step in at any time.

---

## 2. Trading words used in this guide

| Word | Meaning |
|---|---|
| **Swing trade** | A trade held for days to a few weeks, to catch one "swing" of the price. Day trading is shorter; investing is longer. |
| **Long** | Buying a stock to sell it later at a higher price. This system only goes long. |
| **Session** | One trading day of an exchange. NYSE (US): 15:30–22:00 Berlin time. Xetra (Germany): 09:00–17:30. |
| **Daily bar** | One day's summary of a stock: open, high, low and close price, plus volume (shares traded). The system works only with daily bars, not minute-by-minute prices. |
| **Limit order** | "Buy at this price or cheaper." If the price never gets there, nothing is bought. Every entry uses one. |
| **Stop (stop-loss)** | A standing order to sell if the price falls to a set level. It caps the loss of a trade. |
| **Target (take-profit)** | A standing order to sell if the price rises to a set level. It locks in the gain. |
| **Bracket order** | Entry, stop and target sent together. When one exit fills, the broker cancels the other. |
| **Risk (R)** | What you lose if the stop is hit: (entry − stop) × shares. "1R" is that amount. Here it's at most €15 per trade. |
| **R multiple** | A result measured in units of risk. +2R means you won twice what you risked; −1R means the stop was hit. |
| **Reward to risk (R:R)** | (target − entry) ÷ (entry − stop). The system needs at least 2: the possible gain is at least twice the possible loss. |
| **Moving average (SMA, EMA)** | The average closing price over the last N days, for example SMA50 over 50 days. EMA weights recent days more. Rising averages, with the price above them, mean an **uptrend**. |
| **Pullback** | A short dip within an uptrend. Buying the dip is cheaper than buying at the top, and the stop can sit just below the dip. |
| **RSI** | Relative Strength Index, 0 to 100. Below 30 means the stock fell a lot recently ("oversold"), above 70 means it rose a lot ("overbought"). The setup wants 40–55: cooled off, but not collapsing. |
| **ATR** | Average True Range: how much the stock typically moves in a day, in currency. A stop closer than 1 ATR gets hit by normal noise. |
| **Support / resistance** | Price zones where the stock bounced (support) or stalled (resistance) before. |
| **Relative strength** | How a stock did over the last 3 months compared with its market (SPY for US, the DAX ETF for Germany). |
| **Earnings report** | A company's quarterly results. The price can jump 5–10 % overnight on them, which can skip right past a stop. The system avoids buying shortly before one. |
| **Drawdown** | How far the account has fallen from its highest value. |
| **Paper trading** | Trading with simulated money at IBKR (the paper account), using real prices. |
| **Slippage** | Getting a slightly worse price than planned. |
| **Settlement** | Money from a sale arrives one business day later in the US (T+1), two in Germany (T+2). Until then it can't buy anything. |
| **Win rate, expectancy, profit factor** | Share of winning trades; average result per trade (in R or €); total gains ÷ total losses. A profit factor above 1 means it made money. |
| **Book** | The record of the agent's trades at IBKR: `agent_paper` (paper account) or `agent_live` (real money). |
| **LLM** | Large language model, the AI behind chatbots. Here Google Gemini (and later Anthropic Claude) are used through their APIs. |

---

## 3. The big picture

```mermaid
flowchart TB
    subgraph Data["1 · Data (code)"]
        D1[Daily prices<br/>Yahoo, later IBKR]
        D2[Earnings dates]
        D3[EUR/USD rate, macro]
        D4[Company news<br/>Finnhub US, Yahoo EU]
    end
    subgraph Screen["2 · Screen (code)"]
        S1[Pullback rule picks<br/>the top 10 setups per market]
        S2[News agent's buy signals,<br/>basic rules only]
    end
    subgraph AI["3 · AI analysis and debate (LLMs)"]
        A1[Technical analyst]
        A2[Earnings analyst]
        A3[Proposer]
        A4[Critic]
    end
    subgraph Guard["4 · Final decision (code)"]
        RK[Rank by<br/>confidence × R:R]
        R[Risk engine<br/>limits, position size]
    end
    subgraph Exec["5 · Orders (code)"]
        X[Executor<br/>IBKR]
    end
    subgraph After["6 · After the buy"]
        M[Stop, target, breakeven,<br/>time stop]
        N["News agent: exit<br/>(sells above 0.7)"]
    end
    You((You<br/>Telegram + dashboard))

    Data --> Screen --> A1 & A2
    A1 & A2 --> A3 --> A4 --> RK --> R --> X --> M
    D4 --> N
    N -. advice .-> You
    You -- /pause /stop /exit --> R
    You -- /exit --> X
    X -. 🟢 🔴 ⌛ alerts .-> You
```

What runs where:

| Part | Runs as | Uses AI? |
|---|---|---|
| Data collection, quality checks | Code | No |
| Screening for candidates | Code (fixed rules) | No |
| Analysis, proposal, critique | LLMs, checked by code after every answer | Yes |
| Ranking the proposals | Code (confidence × R:R) | No |
| Risk checks and position size | Code | **Never** |
| Orders, stops, exits | Code, talking to the broker | **Never** |
| News: importance, buy signals, exits | LLM (News agent); code applies the thresholds | Yes |
| Briefing, digest | Code | No |

The whole system is one Python program (the `agent` container) plus a database, a read-only dashboard and, once the IBKR login exists, the IB Gateway that talks to Interactive Brokers. It runs on the Zenbook at home.

---

## 4. A day in the life

All times are Berlin time on a normal trading day. The jobs follow the exchange calendars, so holidays and half-days are handled automatically. For a few weeks in spring and autumn the US and Europe switch to summer time on different dates, and all US times move one hour earlier.

| Time | What happens | AI? |
|---|---|---|
| every 5 min | Heartbeat ping to an outside service, which alerts you if the pings stop | |
| 07:00 | Macro data (US rates, inflation, VIX, ECB rate) | |
| 07:15 | Earnings calendar refresh | |
| 07:00–22:30, every 30 min | **News on held stocks**: the News agent reads new headlines (and, once a day, any stock with a report within 3 sessions); a confident `exit` sells at the market | ✓ |
| 07:45 | **EU news**: headlines of every German stock not held; a confident `buy` puts the stock into the EU scan | ✓ |
| 08:15 | **EU scan**: screen German stocks on yesterday's prices (plus news candidates), analyse, propose, rank; 🧠 summary in Telegram | ✓ |
| 08:30 | 📰 **Morning briefing**: markets, EUR/USD, earnings in the next 3 days, the screen's setups, data status | |
| 09:00 | Xetra opens | |
| 09:15 | **EU placement**: risk engine checks today's EU proposals; approved ones are sent as bracket orders; 📤 summary | |
| 09:00–22:50, every 10 min | Monitor: sync with IBKR, report fills | |
| 14:15 | **US news** (as at 07:45, for US stocks) | ✓ |
| 14:45 | **US scan** (as at 08:15, for US stocks) | ✓ |
| 15:30 | NYSE opens | |
| 15:45 | **US placement** (as at 09:15) | |
| 17:30 | Xetra closes | |
| 18:00 | EU closing prices, ECB exchange rate, data quality check | |
| 18:05 | **EU end of day**: fills, stop to breakeven, time stops, EU book update; 🟢 🔴 ⌛ alerts | |
| 22:00 | NYSE closes | |
| 22:30 | US closing prices, quality check | |
| 22:35 | **US end of day** (as at 18:05, for US positions) | |
| 22:40 | Reconciliation: does IBKR hold what the agent thinks it holds? Any difference halts | |
| 23:05 | 🌙 **Evening digest**: today's proposals, placements and rejections, fills, labels to do, AI spend | |
| 03:15 | Database backup (host cron job, once set up) | |

Why scan before the open and place 15 minutes after it? The scan uses the last complete daily bars, which exist once the previous session has closed. Placing after the open avoids the jumpy first minutes, and a placement that runs more than 2 minutes late is skipped rather than run at a random time.

---

## 5. End to end: from first data to a trade

### Step 1: Collect data

After each close, the system downloads that day's prices for every stock in the universe. Every number is stored with its source and time. Earnings dates, EUR/USD and macro data arrive each morning. A repeated download changes nothing; a stock split rewrites the stock's whole history automatically.

### Step 2: Check the data

Quality checks look for missing days, impossible prices (for example a low above the high), jumps over 40 % (often an unrecorded split), zero volume and stale data. A stock with a problem in the last 20 sessions is marked **blocked**, and you get a ⚠️ once per newly blocked stock. The screen skips blocked stocks, stocks without a bar for the latest session and stocks on the blacklist in `risk.yaml`.

### Step 3: Screen for candidates (code, no AI)

The "pullback in an uptrend" rule checks every stock on the latest daily bar. All of these must be true:

| Rule | In plain words |
|---|---|
| Price above 5 (USD or EUR) | No penny stocks |
| 20-day average traded value above 20 million | Enough trading that orders fill at fair prices |
| Close above the 200-day average, and the 50-day above the 200-day | The long-term trend is up |
| The highest close of the last 20 days was 3–10 days ago | It's a fresh dip, not a new high and not a long slide |
| Close 0–3 % above the 20-day EMA or the 50-day SMA | The dip has reached a level where buyers often step in |
| RSI between 40 and 55 | Cooled off, but not collapsing |
| No earnings report in the next 3 sessions | No overnight jump risk right after buying |

An optional **market filter** (off by default, see 12.5) skips the whole market while its index ETF trades below its 200-day average or the VIX is above a limit.

The stocks that pass are ranked by **relative strength** (the strongest over 3 months first). The **top 10 per market** go to the AI (5 in lean mode, see 12.4). Of those, stocks the agent already holds get no proposal.

**News candidates.** 30 minutes before the scan, the News agent reads the new headlines of every stock in the market that the agent doesn't hold. A `buy` with confidence 0.6 or more (a clear, company-specific positive catalyst such as raised guidance) puts the stock into the scan, even if the pullback rule doesn't pass. Such a stock only needs the basic rules: fresh prices, price above 5, enough traded value, no report within 3 sessions, and the market filter. At most 3 news candidates per scan, most confident first. They go through the same analysts, proposer and critic, who also see the news (`catalyst`), and their proposals are marked `news`. Of these, **at most 1 is placed a day** (both markets together).

### Step 4: Analyse (two AI analysts)

For each candidate, code first computes all the facts: trend, moving averages, RSI, MACD, Bollinger Bands, volume, support and resistance zones, Fibonacci levels and ATR. From these it builds a **level menu**: named prices such as `ema20`, `sma50`, `swing_low_last`, `support_1`, `resistance_1`, `atr_stop_2x` or `atr_target_4x`.

- The **technical analyst** reads the facts and rates the setup (`strong_buy`, `buy`, `neutral`, `avoid`) with a draft plan. The plan names an entry, a stop and a target **from the menu**. It can't write a price of its own; an answer with a name that isn't on the menu is rejected.
- The **earnings analyst** looks at the next report and the last 8 (beat or miss, how far the price jumped). It decides whether a report falls inside the holding window (about 17 sessions) and gives a stance: no report in the window, hold through it, exit before it, or wait until after it.

After each answer, code checks it: the stop must be below the entry and the entry below the target; R:R at least 2; the stop 1–4 ATR below the entry. Any number in the text must also appear in the input, otherwise its confidence is capped at 0.5. If a check fails, the AI gets one more try with the errors explained. If it fails again, the answer is dropped.

Only `buy` and `strong_buy` ratings continue.

### Step 5: Propose (the proposer)

The proposer sees both analyses, the level menu, the rules, what the agent already holds and a **market snapshot**: the index ETF's trend, its distance to the 200-day average and its 20-day return, the VIX now and 20 days ago, US credit spreads and interest rates (the FRED values need `FRED_API_KEY`; missing ones are empty). In a weak or nervous market it is told to demand a better setup and lower its confidence. It answers either **`no_trade`** with a reason, or **`propose`** with:

- entry, stop and target, again chosen by name from the menu
- a **thesis**: why it should work, in 2–4 sentences
- an **invalidation**: the price action that would prove it wrong
- a **confidence**: its probability that the target is hit before the stop. With R:R 2, a coin-flip setup is about 0.35; most setups are 0.3–0.5.

It must pass if the earnings analyst said "wait until after the report". The same code checks as in step 4 apply.

### Step 6: Challenge (the critic)

A second AI, meant to come from a different company (Claude, while the others are Gemini), plays devil's advocate. Code turns the level names into prices first, so the critic sees the real R:R and stop distance. It looks for reasons **not** to trade: a thesis the facts don't support, a weakening trend, a stop inside normal noise, a target beyond resistance, an earnings report, too much of one sector, missing data.

Each objection is `minor`, `major` or `blocking`. The critic also adjusts the confidence (from −0.5 to +0.1).

- **`blocking`**: the proposal is stored as `blocked` and is never traded.
- Otherwise it stays `proposed`, with the adjusted confidence.
- If the critic fails or is unavailable, the proposal stays `proposed` and is marked "no critique".

Until an Anthropic key is set up, the critic runs on Gemini too (`LLM_DEV_OVERRIDES=true`).

### Step 7: Rank (code)

The proposals that survive are put in order by **confidence × R:R**: the chance to reach the target times the reward per unit of risk. A proposal with confidence 0.5 and R:R 2.5 (score 1.25) comes before one with 0.6 and 2.0 (1.2). The rank step of each stock shows the numbers.

The proposals are stored and summed up in Telegram (🧠). This is the end of the AI part.

### Step 8: Approve and size (the risk engine, code)

15 minutes after the open, the risk engine takes **today's** proposals for that market in rank order. Older proposals are never placed. For each one it runs 9 checks; a single failure rejects the trade.

| # | Check | Passes when |
|---|---|---|
| 1 | Proposal | Its status is `proposed` (not blocked, not a pass) |
| 2 | Global | The kill switch is active (not paused or halted); in live mode the live interlock is satisfied; the market is open, outside its first 15 and last 10 minutes |
| 3 | Instrument | The market is allowed in this mode (live: US only); the stock is in the universe and not on the blacklist; price ≥ 5; average traded value ≥ 20 million; no earnings in the next 3 sessions |
| 4 | Levels | Stop < entry < target; R:R ≥ 2; stop 1–4 ATR below the entry; entry at most 1 % above the current price; the current price above the stop |
| 5 | Sizing | Some shares fit within all limits (fractions are fine), and the position is worth at least €100 |
| 6 | Fees | Estimated buy and sell fees are at most 10 % of the money at risk |
| 7 | Portfolio | Fewer than 8 positions and pending entries; the sector stays ≤ 60 % of the budget; stocks that move together (correlation above 0.7) stay ≤ 60 % together |
| 8 | Loss limits | Today's loss < 3 %, this week's < 6 %, drawdown < 15 % of the budget |
| 9 | Rate limits | Fewer than 8 orders today; the stock isn't already held or pending |

The **position size** is the largest number of shares, down to 0.0001 of a share, that keeps all three limits:

$$
\text{shares} = \min\left(\frac{r \cdot \text{budget}}{\text{entry} - \text{stop}},\ \frac{p \cdot \text{budget}}{\text{entry}},\ \frac{\text{settled cash} - 10\% \cdot \text{budget}}{\text{entry}}\right)
$$

The risk share $r$ and the position share $p$ grow with the agents' **confidence** (the proposer's, adjusted by the critic). The conviction $c$ is 0 at a confidence of 0.30 or lower and 1 at 0.60 or higher, linear in between; $r = 0.75\% + c \cdot 0.75\%$ and $p = 10\% + c \cdot 15\%$. A proposal without a confidence gets $c = 0$.

In words, on a €1,000 budget: lose at most €7.50 (unsure) to €15 (sure) if the stop is hit, put €100 to €250 into one stock, and always keep €100 in cash. The `/status` check detail and the risk decision show the conviction used.

### Step 9: Place the order

An approved trade becomes a **bracket order**:

- **Entry**: buy limit at the entry price, valid until the close of the next session (2 sessions in total). If the price doesn't come down to it, the order expires (⌛) and nothing happens.
- **Stop**: sell stop, good until cancelled, active once the entry fills.
- **Target**: sell limit, good until cancelled. Stop and target are linked: when one fills, the broker cancels the other.

Prices are rounded to the exchange's price steps, always in the safe direction: entry down, stop and target up. You get a 📤 summary of what was placed and what was rejected, and why.

Every order goes to **IBKR**, to the account the gateway is logged in to (the paper account first). There is no simulator: if the gateway isn't connected at 09:15 or 15:45, nothing is placed, each proposal gets the step "not placed: IBKR not connected", and you get a 🔌 message. Each order carries a reference like `TA-AAPL-entry-1a2b3c4d` that you can search for in TWS, Client Portal and your statements; IBKR's answer, including any error message (for example a rejected fractional size), is stored with the order.

### Step 10: Manage the position

See [section 7](#7-when-does-it-sell). Every evening the system records fills (🟢 bought, 🔴 sold), moves stops, and stores the account value for the loss limits and the dashboard.

### Worked example

A US stock closed at $100.00 and typically moves $2.50 a day (ATR). EUR/USD is 1.10, so the €1,000 budget is $1,100.

1. The screen finds it: uptrend, dipped for 5 days, now 1 % above its 20-day EMA, RSI 47.
2. The technical analyst rates it `buy`: entry `ema20` = $99.50, stop `swing_low_last` = $94.50, target `resistance_1` = $109.50.
3. The earnings analyst finds no report in the next 17 sessions.
4. The proposer agrees with that plan, confidence 0.40, invalidation "close below swing_low_last".
5. The critic notes a `minor` objection (volume is light) and lowers the confidence by 0.02.
6. The risk engine checks it:
   - R:R = (109.50 − 99.50) ÷ (99.50 − 94.50) = 10 ÷ 5 = **2.0** ✓
   - Stop distance = 5 ÷ 2.50 = **2 ATR** ✓ (allowed: 1–4)
   - Entry $99.50 is below the $100 price ✓
   - Size: risk limit $16.50 ÷ $5 = 3.3; position limit $330 ÷ $99.50 = 3.3; cash limit $990 ÷ $99.50 = 9.9 → **3 shares**
   - Position $298.50 ≈ €271 (≥ €200 ✓); at risk 3 × $5 = $15 ≈ €13.60
   - Fees about $0.75 for buy and sell, 5 % of the risk ✓ (allowed: 10 %)
7. Order: buy 3 at $99.50 limit, stop $94.50, target $109.50.

What can happen next:

| Outcome | Result |
|---|---|
| The price never dips to $99.50 within 2 sessions | ⌛ Entry expires; nothing bought |
| Bought; the price falls to $94.50 | 🔴 Sold by the stop: −$15 (−1R) plus fees ≈ −€14 |
| Bought; the price rises to $109.50 | 🔴 Sold at the target: +$30 (+2R) minus fees ≈ +€27 |
| Bought; the price reaches $104.50 (+1R), then falls back | The stop moved up to $99.50 that evening, so it sells at about the entry price: roughly zero, minus fees |
| Bought; neither stop nor target hit within 15 sessions | Sold at the next open, whatever the price |

Because a win (+2R) is twice the size of a loss (−1R), the system makes money if a bit more than one trade in three reaches its target, before costs.

---

## 6. When does it buy?

The system buys a stock only if **all** of the following are true:

1. It's in the universe (S&P 100 or DAX 40), it has a price bar for the latest session, and the agent doesn't hold it yet.
2. The rule-based screen found a pullback in an uptrend on the last daily bar, and it ranked in the top 10 of its market. **Or** the News agent gave it a buy signal (confidence 0.6 or more) in the last 24 hours, it passed the basic rules, and it ranked in the top 3 news candidates.
3. The technical analyst rated it `buy` or `strong_buy` with a valid plan.
4. The earnings analyst didn't say "wait until after the report".
5. The proposer proposed it.
6. The critic didn't block it.
7. The risk engine approved it the next morning, 15 minutes after the open (all 9 checks).
8. During that session or the next one, the price dipped to the entry limit.

If several proposals pass, they are placed in rank order (confidence × R:R) until the limits are reached (8 positions, 8 orders a day, sector and cash limits). Proposals from news candidates are limited to 1 placed a day.

---

## 7. When does it sell?

| Reason | How it works | Alert |
|---|---|---|
| **Target reached** | The target order at the broker sells when the price reaches it (at least 2R) | 🔴 target |
| **Stop hit** | The stop order at the broker sells when the price falls to it. If the stock opens below the stop after bad news, it sells at that worse price. | 🔴 stop |
| **Breakeven stop** | Once the daily high reaches entry + 1R, the stop is moved up to the entry price that evening. From then on the trade can't turn into a real loss (except for fees or a gap down). Stops only ever move up. | 🔴 stop |
| **Trailing stop** (optional, off) | With `trail_atr` set (12.5): after breakeven, every evening the stop moves up to the highest close since the entry minus that many ATRs. The target stays. | 🔴 stop |
| **Time stop** | After 15 sessions without hitting the stop or the target, it sells with a market order at the next open. Capital isn't tied up in a trade that isn't working. | 🔴 time |
| **You say so** | `/exit SYMBOL` in Telegram sells the whole position with a market order at the next open | 🔴 |
| **News exit** | The News agent says the news breaks the reason for the trade, with confidence 0.7 or more. A market order sells right away (at the next open if the market is closed). | 🚪 then 🔴 news |

What does **not** sell automatically:

- **Kill switch** (`/stop` or a loss limit): no new entries, and all unfilled entries are cancelled. Open positions **keep** their stops and targets, so they are still protected and can still reach the target.
- **A less certain news exit** (🧐): below 0.7 the News agent only tells you why and suggests `/exit SYMBOL`. The decision is yours.
- **Earnings ahead**: the system doesn't buy within 3 sessions of a report, and doesn't sell before one by rule. A report within 3 sessions makes the News agent look at the position once a day, which may lead to a news exit.

```mermaid
stateDiagram-v2
    [*] --> Pending: risk engine approves, bracket sent
    Pending --> Expired: price never reached the limit in 2 sessions
    Pending --> Cancelled: kill switch halt
    Pending --> Holding: entry filled
    Holding --> Holding: stop moved to breakeven at +1R
    Holding --> Sold: target, stop, time stop or /exit
    Expired --> [*]
    Cancelled --> [*]
    Sold --> [*]
```

---

## 8. The multi-agent system

The project is also a showcase for a **multi-agent system (MAS)**: several independent decision-makers that each do one job and together reach a decision none of them makes alone. This section explains the agents, who decides what, and what happens when one fails. The MAS design itself (architecture, communication, shared memory, coordination, trust, observability, evaluation) is in [MAS-DESIGN.md](MAS-DESIGN.md).

### 8.1 The roles

| Role | Kind | Sees | Decides | Model (`config/models.yaml`) |
|---|---|---|---|---|
| Screen | Code | Daily bars of the whole universe | Which stocks are worth a look | none |
| Technical analyst | LLM | Computed indicators and the level menu | Rating, setup quality, a draft plan | `analysis`: Gemini 3.8 Flash |
| Earnings analyst | LLM | Next report, last 8 reports and price reactions | Earnings stance | `analysis`: Gemini 3.8 Flash |
| Proposer | LLM | Both analyses, the menu, rules, holdings, market snapshot | Propose or pass; plan, thesis, confidence | `proposer`: Gemini 3.8 Flash |
| Critic | LLM, other vendor | The same facts plus the plan in prices | Objections, severity, confidence change | `critic`: Claude Sonnet 5.5 (Gemini until an Anthropic key exists) |
| Risk engine | Code | Proposal, account, market facts, limits | Approve or reject; number of shares | none |
| Executor | Code | Approved orders, broker state | Places, syncs, moves stops, exits | none |
| News agent | LLM | New headlines (as untrusted text); for held stocks also the position facts and the original thesis | Importance per headline; `buy` (not held), `exit` (held) or `none`, with a confidence | `triage`: Gemini 3.1 Flash-Lite |

The AI roles don't chat freely with each other. Each runs once per stock in a fixed order, and gets the previous roles' answers as input. The order is fixed by code: `orchestrator.py` holds the whole day stage by stage (scan, place, monitor, end of day).

### 8.2 Who's the boss?

**The risk engine is the boss of every order, and you are the boss of the risk engine.**

```mermaid
flowchart TB
    You["<b>You</b><br/>settings, /pause, /stop, /exit, /reset"]
    KS["<b>Kill switch</b><br/>active · paused · halted"]
    RE["<b>Risk engine</b> (code)<br/>only stage that can approve; sets the size"]
    CR["Critic (AI)<br/>can veto: blocking"]
    PR["Proposer (AI)<br/>can pass: no_trade"]
    AN["Technical + earnings analysts (AI)<br/>can stop it: rating, wait_until_after"]
    SC["Screen (code)<br/>decides who gets looked at"]

    You --> KS --> RE --> CR --> PR --> AN --> SC
```

Read it from the bottom up: a trade needs a "yes" at every level, and any level below the risk engine can stop it. The levels above can only narrow things further.

| Level | Can stop a trade? | Can force a trade? |
|---|---|---|
| Screen | Yes: a stock that isn't picked is never analysed | No |
| Analysts | Yes: a rating below `buy`, or "wait until after the report" | No |
| Proposer | Yes: `no_trade` | No |
| Critic | Yes: `blocking` | No |
| Risk engine | Yes: any of 9 checks | It approves, but only what the AI proposed, and only within the limits |
| Kill switch | Yes: paused or halted means no new entries | No |
| You | Yes: `/pause`, `/stop`, settings | No: there is no manual buy command. `/exit` is the only manual order. |

Why it is built this way:

- **The AI never touches money directly.** Code checks enforce it: the AI code can't even import the order code. The AI returns data; code turns it into an order only after the risk engine says yes.
- **The AI never invents prices.** It picks names from a menu of prices that code computed from real data. Code turns the names back into prices.
- **The AI never decides the size.** The number of shares comes only from the risk engine's formula.
- **Two vendors.** The critic should come from a different company than the proposer, so the two are less likely to make the same mistake.
- **News is treated as data, never as instructions.** Its text is wrapped and labelled as untrusted. A manipulated article saying "buy XYZ now" can at most put XYZ in front of the analysts, the proposer, the critic and the risk engine, all of which must agree, and only 1 news trade a day is placed. A news exit can only sell what is held.

### 8.3 What happens when something fails

| Failure | Effect |
|---|---|
| The technical analyst's or the proposer's answer fails the checks twice | That stock is skipped today |
| The critic fails | The proposal stays, marked "no critique" |
| The AI budget is used up (100 %) | No more AI calls this month, so no new trades. |
| Prices for a stock look wrong | ⚠️ alert; the stock is blocked and left out of the scan until the problem is 20 sessions old |
| The agent or the computer is down | Stops and targets are already at IBKR. Missed jobs run once when it's back, if they're still useful; a late order placement is skipped. |
| The IB Gateway isn't connected | Nothing is placed, no stop is moved and no exit is sent; each skipped step is logged and you get a 🔌 message. Stops and targets already at IBKR keep working. |

---

## 9. Does it work?

Nobody knows yet whether the AI adds value. Public studies of AI trading agents are sobering, and the rule-based strategy alone showed **no edge** in a 5-year backtest: roughly break-even before costs, slightly negative after fees, far behind simply holding the index ([IMPLEMENTATION.md](IMPLEMENTATION.md) 6.3).

The answer comes from the real trades only: `agent_paper` holds every order the agent sent to the IBKR paper account and every fill IBKR reported. The dashboard shows its results after fees and AI costs, and your agree/disagree labels (`/review`) next to the outcomes. `agent_live` is the same for real money, later.

---

## 10. Safety nets

| Safety net | What it does |
|---|---|
| **Stops at the broker** | Every position has a stop and a target at the broker from the moment it's bought, so a crash, a power cut or a reboot never leaves a position unprotected (once orders go to IBKR). |
| **Hard limits in code** | €7.50 to €15 risk per trade and €100 to €250 per position (by confidence), 8 positions, €100 cash reserve, sector and correlation caps, 8 orders a day. AI output can't change them. |
| **Loss limits** | Down 3 % on a day: no new entries the next trading day. Down 6 % this week: none until Monday. Down 15 % from the peak: **halt** (see below). Measured every evening after the close (the account value at the closing prices), with a Telegram message, and checked again before each new trade. |
| **Kill switch** | Three states: **active** (normal), **paused** (no new entries; via `/pause` or a daily or weekly loss limit), **halted** (no new entries, all unfilled entries cancelled; via `/stop`, the drawdown limit or a reconciliation mismatch). |
| **Reset needs two keys** | A halt only ends with a code printed on the host (`trading-agent reset`), sent from your phone with `/reset CODE` within 15 minutes. A reset needs both the host and your phone. |
| **Reconciliation** | Compares the broker's positions with the agent's records after each US close and after every reconnect. Any difference halts the agent. |
| **Live interlock** | Real money needs three things at once: `APP_MODE=live`, a live IBKR login, and `/confirm_live CODE` with a code from the host logs after every start. Without all three, no orders. Paper mode on a live login is refused too. |
| **AI budget** | Monthly cap of $16 (about €15). Fewer candidates above 80 %, no calls at 100 %. A model without a known price is never called. |
| **Owner-only Telegram** | Messages from any other chat are ignored and logged. Messages never contain account numbers or passwords. |
| **Read-only dashboard** | It can't change anything, uses a read-only database login, and is reachable only through your Tailscale network. |
| **Heartbeat** | An outside service alerts you if the agent stops pinging. |
| **Backups** | Nightly database dump, a weekly encrypted copy for off-site storage, and a tested restore. |
| **Audit trail** | Every AI prompt and answer, every risk decision with all checks, every order change and every kill-switch change is stored. |

---

## 11. Your part: Telegram, dashboard, daily routine

### 11.1 Telegram commands

| Command | What it does |
|---|---|
| `/status` | Mode, kill switch, uptime, heartbeat, latest data, blocked stocks, next jobs, IBKR connection |
| `/briefing` | The morning briefing, now |
| `/budget` | AI spend this month and the budget mode |
| `/proposals` | The latest proposals and their status |
| `/why SYMBOL` | Thesis, critique and plan for one proposal |
| `/review` | Steps through the latest proposals with Agree / Disagree / Skip buttons; reply with a reason if you like. Labels are only used for evaluation and never change a trade. |
| `/positions` | Open positions (entry, stop, target, last close, R, P&L) and pending entries |
| `/pnl` | Results for the day, week, month and since the start |
| `/exit SYMBOL` | Sell that position at the next open (market order) |
| `/pause [reason]` | No new entries until `/resume` |
| `/resume` | Allow new entries again |
| `/stop` | Kill switch: halt (asks for confirmation) |
| `/reset CODE` | End a halt, with the code from `trading-agent reset` on the host |
| `/confirm_live CODE` | Live mode only: allow orders after a start |
| `/help` | The command list |

### 11.2 Messages you'll get

| Icon | When |
|---|---|
| ▶️ ⏹ | The agent started or is stopping |
| 🧠 | Scan result: ranked proposals, blocked ones and passes, AI cost |
| 📰 | Morning briefing; important news on a held stock; or a buy signal from the news |
| 📤 | Placement: what was ordered and what the risk engine rejected, and why |
| 🟢 | A buy filled |
| 🔴 | A position was sold (target, stop, time stop, news or `/exit`) |
| 🚪 | News exit: the market sell order was sent |
| ⌛ | An entry expired or was cancelled |
| 🧐 | The News agent thinks the reason for a trade may be gone, but isn't sure enough to sell; with `/exit SYMBOL` |
| 🌙 | Evening digest |
| 📊 | Answer to `/positions` |
| ⏸ ⛔ ▶️ | Kill switch paused, halted, or entries allowed again (also after a loss limit) |
| ⚠️ | Data problem, failed or missed job, reconciliation mismatch |
| 🔌 | IB Gateway disconnected for more than 10 minutes, and again when it's back |
| 🔐 | Live mode: confirm with `/confirm_live CODE` |

### 11.3 Dashboard

What each page shows and where to find what: [RUNNING.md](RUNNING.md) sections 1 and 2.

Open it on your phone (or any device in your tailnet) through Tailscale. Pages: **Day** (pick a day: how many stocks got past each phase, one row per stock with each phase's outcome, and for any stock every step with its inputs and outputs: indicator values, each agent's input, answer and prompts, the risk engine's checks, the orders sent to IBKR and IBKR's replies and errors, fills, stop moves and the sale), **Portfolio** (what is held at IBKR, buy orders waiting, what was sold, when and why, and any trade's whole timeline), **Costs** (AI and broker fees) and **Risk** (limit usage, correlation heatmap, rejections).

### 11.4 Daily routine (about 1 hour)

- **Morning, 10 minutes**: read the briefing and the 🧠 EU proposals. Anything you strongly disagree with? `/pause` stops the placement at 09:15.
- **Evening, 30–60 minutes**: read the digest. `/review` the day's proposals: agree or disagree, with a short reason. Check any 🧐 advice and decide on `/exit`.
- **Saturday, 1 hour**: look at the week's results on the dashboard. Which lessons should change a prompt or a rule? Changes are versioned and need a reason.

---

## 12. Settings you can change

### 12.1 How to change a setting

- **Files in `config/` and `prompts/`** are part of the code and built into the Docker image. A change is committed and pushed to GitHub, then on the Zenbook: `git pull && make build && make up` ([RUNNING.md](RUNNING.md) section 7). The agent reads them at start.
- **`.env`** lives only on the Zenbook. Edit it there, then `docker compose up -d` (a plain `restart` doesn't re-read `.env`).
- Change things outside trading sessions, or `/pause` first.
- **`config/risk.yaml` needs extra care**: every change gets its own commit with a reason, never mixed with other changes. Unknown or invalid keys stop the agent from starting, so a typo can't silently turn off a limit.

### 12.2 `.env`: switches and keys

| Setting | Current | What it does |
|---|---|---|
| `APP_MODE` | `paper` | `paper` or `live`. Live also needs a live IBKR login and `/confirm_live`. In live mode only `markets.live` is traded, with the real `agent_budget_eur`. |
| `TWS_USERID`, `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID` | | IBKR login name and connection. Port 4004 is paper, 4003 live. There is no switch for the broker: every order goes to IBKR, and nothing is placed while the gateway is disconnected. |
| `LLM_DEV_OVERRIDES` | `true` | `true`: the critic runs on Gemini (`dev_overrides` in `models.yaml`). `false` once an Anthropic key exists. |
| `LLM_FAKE` | `false` | `true` only in the local test stack (`make dev-up`): every AI role gets canned, rule-following answers and no API is called. Never on the Zenbook. |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` | | AI provider keys. Without a key, that provider's roles can't run. |
| `TRACE_ENDPOINT` | Langfuse EU | Where each run's trace goes ([OBSERVABILITY.md](OBSERVABILITY.md)). Tracing starts with the agent as soon as the Langfuse keys are in `secrets/trace_headers` (`make trace-headers`); `TRACE_ENDPOINT=` (empty) switches it off. Every AI call is stored in the database either way. |
| `TRACE_CONTENT` | `true` | `false`: the traces keep timings and tokens but no prompts or answers |
| `TRACE_ENVIRONMENT` | empty | Label in Langfuse; empty means `APP_MODE` (`paper`) |
| `TRACE_UI_URL` | your project | Langfuse project address for the "Open trace" links (dashboard, `/why`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` | | Your bot and your chat. Without the chat id, every message is ignored. |
| `FRED_API_KEY` | | US macro data; skipped without it |
| `FINNHUB_API_KEY` | | News for US stocks; without it, no news buys or news exits for US stocks (EU news comes from Yahoo, no key) |
| `SEC_EDGAR_USER_AGENT` | | Required by the SEC for filings data (not used yet) |
| `HEARTBEAT_URL`, `BACKUP_HEARTBEAT_URL` | | Outside health checks for the agent and the backup |
| `BACKUP_AGE_RECIPIENT` | | Public key for the encrypted weekly backup copy |
| `LOG_LEVEL`, `TZ`, `DB_*` | | Logging detail, time zone, database connection; the defaults work |

### 12.3 `config/risk.yaml`: the hard limits

The risk engine reads this file. "Budget" means one budget for US and Germany together: in paper mode `paper_budget_eur` (€15,000), in live mode `agent_budget_eur`.

**Capital**

| Setting | Current | What it does |
|---|---|---|
| `capital.agent_budget_eur` | 1000 | The money the agent may use, whatever the account really holds. All percentages below are of this amount. |
| `capital.min_cash_reserve_pct` | 10 | Cash that is never spent (€100) |
| `capital.paper_budget_eur` | US: 10000, EU: 15000 | Paper only: a separate simulated budget per market, so more trades fit. The budgets must differ: markets with the same budget share one account. Germany gets more because its minimum fees weigh more on small positions. |

**Per trade**

| Setting | Current | What it does | Higher means |
|---|---|---|---|
| `per_trade.min_risk_pct` / `max_risk_pct` | 0.75 / 1.5 | Most you can lose on one trade if the stop holds, at the lowest and the highest confidence (€7.50 to €15 on €1,000) | Bigger positions, bigger losses per trade |
| `per_trade.min_position_pct` / `max_position_pct` | 10 / 25 | Most money in one stock at the lowest and the highest confidence (€100 to €250 on €1,000) | Larger positions, fewer at a time |
| `per_trade.min_confidence` / `max_confidence` | 0.30 / 0.60 | The confidence at or below which positions are smallest, and at or above which they are largest. The proposer's confidence is the probability that the target is hit before the stop; with R:R 2 the break-even is 0.33 and most setups are 0.3 to 0.5. | A higher `max_confidence` makes full size rarer |
| `per_trade.min_position_eur` | 100 | Smallest position worth the fees | Fewer small trades |
| `per_trade.max_fee_to_risk_pct` | 10 | Reject if buy and sell fees exceed this share of the risk | More trades where fees eat the profit |
| `per_trade.min_risk_reward` | 2.0 | Minimum (target − entry) ÷ (entry − stop). The AI is told this rule too. | Fewer trades with bigger targets, which are hit less often |
| `per_trade.stop_atr_min` / `stop_atr_max` | 1.0 / 4.0 | Allowed stop distance in ATRs. Below 1 the stop sits in normal daily noise; above 4 the position gets very small. | |
| `per_trade.max_limit_deviation_pct` | 1.0 | The entry may be at most this much above the current price (any distance below is fine; it may simply not fill) | Allows chasing the price |
| `per_trade.require_broker_side_stop`, `order_type`, `execution.routing` | true, limit_only, smart | Fixed design rules; only these values are accepted | |

**Portfolio**

| Setting | Current | What it does |
|---|---|---|
| `portfolio.max_open_positions` | 8 | Positions plus pending entries at the same time |
| `portfolio.max_sector_pct` | 60 | Most of the budget in one sector, for example technology |
| `portfolio.max_correlated_cluster_pct` | 60 | Most of the budget in stocks that move together (correlation of daily returns above 0.7 over 60 sessions). A stock without enough history counts as correlated. |

**Loss limits** (only stop new entries; open positions keep their stops)

| Setting | Current | What it does |
|---|---|---|
| `loss_limits.daily_loss_pct` | 3 | Down €30 from one close to the next: paused through the next trading day |
| `loss_limits.weekly_loss_pct` | 6 | Down €60 this week: paused until Monday |
| `loss_limits.max_drawdown_pct` | 15 | Down €150 from the highest account value: halted until you reset. |

**Markets, instruments, execution**

| Setting | Current | What it does |
|---|---|---|
| `markets.paper` / `markets.live` | [US, EU] / [US] | Markets that may be traded in each mode |
| `instruments.min_price` | 5 | Lowest share price |
| `instruments.min_avg_daily_dollar_volume` | 20,000,000 | Lowest 20-day average traded value, in the stock's currency |
| `instruments.blacklist` | [] | Yahoo symbols that are never scanned or traded, e.g. `[TSLA, SAP.DE]` |
| `execution.max_orders_per_day` | 8 | New brackets per day, across the whole book |
| `execution.no_trading_first_minutes` / `last_minutes` | 15 / 10 | No new entries this close to the open or close |
| `execution.no_new_entries_before_earnings_days` | 3 | No new entry if a report is this many sessions away or fewer |

The universe itself comes from `universe.yaml` (12.7), the AI budget from `models.yaml` (12.4).

### 12.4 `config/models.yaml`: AI models and budget

| Setting | Current | What it does |
|---|---|---|
| `roles.triage` | Gemini 3.1 Flash-Lite | News agent (cheap and fast; it reads many headlines) |
| `roles.analysis` | Gemini 3.8 Flash | Technical and earnings analysts |
| `roles.proposer` | Gemini 3.8 Flash | Proposer |
| `roles.critic` | Claude Sonnet 5.5 | Critic; needs `ANTHROPIC_API_KEY` and a price below |
| `dev_overrides.critic` | Gemini 3.8 Flash | Used instead while `LLM_DEV_OVERRIDES=true` |
| `prices` | per model | Price per million tokens, used to estimate costs. **A model without a price is never called.** |
| `budget.monthly_usd` | 16 | The monthly AI budget (about €15) |
| `budget.lean_mode_at` | 0.8 | From 80 % spent: lean mode, fewer candidates per scan |
| `budget.hard_stop_at` | 1.0 | From 100 %: no more AI calls this month |
| `rate_limits_rpm` | 10 / 15 | Calls per minute per model, to stay inside the free tier |
| `scan.candidates` / `candidates_lean` | 10 / 5 | Candidates analysed per market and scan, normally and in lean mode. The biggest lever on AI cost. |

### 12.5 `config/strategies.yaml`: the screen and trade management

These rules pick the candidates for the AI. The trade-management settings (`entry_valid_sessions`, `breakeven_r`, `time_stop_sessions`, `trail_atr`) manage every position.

| Setting | Current | What it does |
|---|---|---|
| `min_price`, `min_avg_dollar_volume`, `avg_volume_sessions` | 5, 20 M, 20 | Price and liquidity floor of the screen |
| `high_lookback`, `pullback_sessions` | 20, [3, 10] | The pullback must start from the highest close of the last 20 days, 3–10 days ago |
| `ma_band_pct` | [0, 3] | Close 0–3 % above the EMA20 or SMA50 |
| `rsi_range` | [40, 55] | RSI window |
| `earnings_buffer_sessions` | 3 | No report this close |
| `rs_window` | 63 | Ranking: 3-month return against the market |
| `entry_valid_sessions` | 2 | How long a buy limit stays open |
| `breakeven_r` | 1.0 | Move the stop to the entry price at +1R (also for the agent) |
| `time_stop_sessions` | 15 | Sell after this many sessions (also for the agent) |
| `trail_atr` | off (`null`) | E.g. 2.0: after breakeven, trail the stop 2 ATR under the highest close since the entry (also for the agent) |
| `regime_sma` | off (`null`) | E.g. 200: no new entries in a market while its index ETF closes below this moving average (screen, and therefore the AI's candidates) |
| `regime_max_vix` | off (`null`) | E.g. 30: no new entries while the VIX closes above this (needs `FRED_API_KEY`; applies to both markets) |

The three optional rules are off. On 5 years of data none of them made the rule-based strategy profitable ([IMPLEMENTATION.md](IMPLEMENTATION.md) 6.4).

The `news:` section sets what code does with the News agent's answers:

| Setting | Current | What it does |
|---|---|---|
| `buy_confidence` | 0.6 | A buy signal this sure puts the stock into the market's next scan |
| `exit_confidence` | 0.7 | An exit this sure sells the position at the market; below it you get advice (🧐) |
| `max_candidates` | 3 | News candidates per scan and market |
| `max_trades_per_day` | 1 | News-triggered entries placed per day, both markets together |
| `signal_hours` | 24 | Older buy signals are ignored |
| `lookback_days`, `max_headlines`, `batch` | 2, 5, 8 | Days of news per request, new headlines read per stock, stocks per News agent call |

### 12.6 `config/schedule.yaml`: when jobs run

Each job runs either at a clock time (`cron`, Berlin time) or relative to an exchange's open or close (`calendar`, `anchor`, `offset_minutes`). `misfire_grace_minutes` is how late a job may still run after a downtime; later than that, it is skipped. The table in [section 4](#4-a-day-in-the-life) shows the current times. Keep the order intact: prices before the end-of-day job, scan before placement, and placement inside the trading window.

### 12.7 `config/fees.yaml`, `config/data.yaml`, `config/universe.yaml`

| File | What it controls |
|---|---|
| `fees.yaml` | IBKR commissions per market (Tiered or Fixed plan, minimums, exchange and regulatory fees). Used for the fee check and for every simulated trade. Compare with the first real statements. |
| `data.yaml` | Price history length (6 years), price source per market (`yahoo`, later `ibkr`), macro series, quality thresholds (jump size, stale days, how long a problem blocks a stock) |
| `universe.yaml` | The stocks that are watched, with sector and currency, plus the two benchmark ETFs. Generated by `scripts/build_universe.py` from the index member lists; don't edit it by hand. A larger universe (S&P 500, MDAX) means more candidates and more AI cost. |

### 12.8 `prompts/`

The instructions for each AI role, one file per version (`prompts/proposer/v2.md`). A change becomes a new version, so results before and after can be compared. Every stored AI answer records the prompt version and model it used.

---

## 13. What the system does not do (yet)

- **No short selling, no margin, no options.** Only buying shares with cash.
- **No intraday trading.** Decisions use daily bars; stops and targets at the broker react during the day, but breakeven and time stops are set in the evening.
- **No trailing stop by default** beyond the one move to breakeven (`trail_atr` can switch one on).
- **No automatic sale before earnings by rule.** The News agent looks at such positions and may exit on news.
- **Macro data only informs the proposer** (and the optional market filter). Company fundamentals (revenue, valuation) aren't used.
- **News is headlines only**, a few times a day: Finnhub for US stocks (free tier), Yahoo for EU stocks (unofficial, latest headlines only).
- **The 1 % entry check uses a delayed price** (15–20 minutes old) while the IBKR gateway is connected, and the last close without it. Real-time prices would need a paid market-data subscription.
- **Paper fills are simulated from daily bars** until the IBKR paper login is set up (runbook in [IMPLEMENTATION.md](IMPLEMENTATION.md) 15.5).
- **No proven edge.** The whole point of the paper phase is to find out whether there is one.
