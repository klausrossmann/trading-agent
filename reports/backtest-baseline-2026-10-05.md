# Backtest: pullback_uptrend (US)

Period 2021-10-02 to 2026-10-02, starting capital EUR 1,000. Amounts in EUR, net of modelled fees and slippage.

## Returns

| | Total return | CAGR | Max drawdown | Sharpe | Sortino |
|---|---|---|---|---|---|
| Strategy | -10.6 % | -2.2 % | -16.2 % | -0.47 | -0.25 |
| Buy and hold SPY | 86.1 % | 13.2 % | -23.0 % | 0.78 | 1.07 |

## Trades

- Closed trades: 48 (open at the end: 0)
- Win rate: 31.2 %
- Average R: -0.14; expectancy EUR -2.21 per trade
- Profit factor: 0.64
- Net P&L: EUR -106.09; fees EUR 31.87
- Average holding period: 8.7 sessions
- Average exposure: 8 % of equity invested

## By exit reason

| Exit | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| stop | 30 | 3 % | -0.73 | -276.61 | 20.06 |
| target | 4 | 100 % | 2.00 | 71.52 | 2.58 |
| time | 14 | 71 % | 0.53 | 99.01 | 9.23 |

## By market

| Market | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| US | 48 | 31 % | -0.14 | -106.09 | 31.87 |

## By year (exit date)

| Year | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| 2021 | 16 | 44 % | 0.20 | 23.08 | 10.34 |
| 2022 | 32 | 25 % | -0.31 | -129.17 | 21.53 |

## Signal funnel

- Setups found: 5832
- Not taken, loss limits: drawdown: 4682
- Not taken, earnings within buffer: 352
- Not taken, sizing: below_minimum: 320
- Not taken, sizing: no_capacity: 270
- Not taken, already held or pending: 89
- Not taken, max open positions: 63
- Not taken, fees above limit: 3
- Entries filled: 48

## Notes

- Survivorship bias: today's index members over the whole period, which flatters the result.
- Daily bars: if stop and target are both inside a bar the stop counts; on the entry bar only the stop is checked.
- Risk engine rules applied: sizing, positions, sector and correlation cluster caps, fee-to-risk, orders per day, settled cash, loss limits (measured at each close). Not modelled: FX conversion costs, dividends.
- Prices from Yahoo Finance; third-party fees are estimates (config/fees.yaml).

# Backtest: pullback_uptrend (EU)

Period 2021-10-02 to 2026-10-02, starting capital EUR 5,000. Amounts in EUR, net of modelled fees and slippage.

## Returns

| | Total return | CAGR | Max drawdown | Sharpe | Sortino |
|---|---|---|---|---|---|
| Strategy | -6.6 % | -1.4 % | -14.7 % | -0.16 | -0.14 |
| Buy and hold EXS1.DE | 63.3 % | 10.3 % | -26.7 % | 0.65 | 0.92 |

## Trades

- Closed trades: 116 (open at the end: 0)
- Win rate: 43.1 %
- Average R: -0.04; expectancy EUR -2.83 per trade
- Profit factor: 0.89
- Net P&L: EUR -328.37; fees EUR 429.20
- Average holding period: 12.4 sessions
- Average exposure: 20 % of equity invested

## By exit reason

| Exit | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| stop | 34 | 0 % | -1.01 | -2191.80 | 125.80 |
| target | 7 | 100 % | 2.08 | 900.07 | 25.90 |
| time | 75 | 57 % | 0.20 | 963.36 | 277.50 |

## By market

| Market | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| EU | 116 | 43 % | -0.04 | -328.37 | 429.20 |

## By year (exit date)

| Year | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| 2021 | 13 | 38 % | -0.22 | -220.54 | 48.10 |
| 2022 | 49 | 45 % | 0.01 | 203.38 | 181.30 |
| 2023 | 54 | 43 % | -0.05 | -311.21 | 199.80 |

## Signal funnel

- Setups found: 2161
- Not taken, loss limits: drawdown: 1319
- Not taken, max open positions: 283
- Not taken, already held or pending: 233
- Not taken, earnings within buffer: 116
- Not taken, fees above limit: 36
- Not taken, sizing: no_capacity: 29
- Not taken, sizing: below_minimum: 20
- Entries filled: 116

## Notes

- Survivorship bias: today's index members over the whole period, which flatters the result.
- Daily bars: if stop and target are both inside a bar the stop counts; on the entry bar only the stop is checked.
- Risk engine rules applied: sizing, positions, sector and correlation cluster caps, fee-to-risk, orders per day, settled cash, loss limits (measured at each close). Not modelled: FX conversion costs, dividends.
- Prices from Yahoo Finance; third-party fees are estimates (config/fees.yaml).
