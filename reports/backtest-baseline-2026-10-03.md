# Backtest: pullback_uptrend (US, EU)

Period 2021-10-02 to 2026-10-02, starting capital EUR 1,000. Amounts in EUR, net of modelled fees and slippage.

## Returns

| | Total return | CAGR | Max drawdown | Sharpe | Sortino |
|---|---|---|---|---|---|
| Strategy | -7.7 % | -1.6 % | -23.8 % | -0.09 | -0.13 |
| Buy and hold SPY | 86.1 % | 13.2 % | -23.0 % | 0.78 | 1.07 |
| Buy and hold EXS1.DE | 61.2 % | 10.0 % | -26.7 % | 0.64 | 0.90 |

## Trades

- Closed trades: 266 (open at the end: 1)
- Win rate: 39.8 %
- Average R: -0.03; expectancy EUR -0.30 per trade
- Profit factor: 0.94
- Net P&L: EUR -79.39; fees EUR 177.86
- Average holding period: 10.5 sessions
- Average exposure: 56 % of equity invested

## By exit reason

| Exit | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| stop | 127 | 2 % | -0.86 | -1218.66 | 85.02 |
| target | 33 | 100 % | 1.96 | 706.92 | 21.84 |
| time | 106 | 67 % | 0.35 | 432.35 | 71.00 |

## By market

| Market | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| US | 266 | 40 % | -0.03 | -79.39 | 177.86 |

## By year (exit date)

| Year | Trades | Win rate | Avg R | P&L | Fees |
|---|---|---|---|---|---|
| 2021 | 16 | 44 % | 0.20 | 23.08 | 10.34 |
| 2022 | 41 | 24 % | -0.36 | -180.07 | 27.99 |
| 2023 | 44 | 48 % | 0.21 | 96.20 | 30.08 |
| 2024 | 67 | 48 % | 0.12 | 102.10 | 45.87 |
| 2025 | 54 | 37 % | -0.13 | -54.89 | 35.62 |
| 2026 | 44 | 36 % | -0.14 | -65.80 | 27.97 |

## Signal funnel

- Setups found: 7993
- Not taken, sizing: no_capacity: 3230
- Not taken, sizing: below_minimum: 2417
- Not taken, max open positions: 842
- Not taken, already held or pending: 539
- Not taken, earnings within buffer: 453
- Not taken, fees above limit: 211
- Not taken, sector cap: 7
- Entries filled: 267

## Notes

- Survivorship bias: today's index members over the whole period, which flatters the result.
- Daily bars: if stop and target are both inside a bar the stop counts; on the entry bar only the stop is checked.
- Not modelled yet: correlation clusters and loss limits (risk engine, M8), FX conversion costs, dividends.
- Prices from Yahoo Finance; third-party fees are estimates (config/fees.yaml).
