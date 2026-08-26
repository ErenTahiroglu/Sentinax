"""
backend/engine/private — Private Personal Investment Decision Engine

Bounded context for Sentinax Private Engine.
This package contains ONLY domain contracts and interfaces.
No external API calls, no execution, no allocation algorithm yet.

Scope:
    - BIST stocks
    - TEFAS funds (money market + equity)
    - US stocks & ETFs
    - European stocks & ETFs
    - Gold (ALTIN.S1), silver, FX
    - TL bonds/bills
    - Turkey Eurobonds

Out of scope:
    - Crypto (any asset)
    - Order execution
    - Paper trading
    - ML predictions (experimental, future)
"""
