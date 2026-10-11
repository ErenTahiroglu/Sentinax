# Test fixtures (MOCK / synthetic)

`private_portfolio_summary_unavailable.json` is a **test-only** copy of the real `GET /private/portfolio/summary` UNAVAILABLE envelope with a fixed `generated_at`.
It contains no financial data. A backend test (`test_private_portfolio_summary_api.py`) asserts that the real endpoint still produces exactly this envelope (except
`generated_at`), so the browser tests that replay it cannot drift from the real contract. It must never be imported by production code.
