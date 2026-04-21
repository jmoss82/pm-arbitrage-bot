"""
End-of-window favorite sniping on Polymarket BTC short-duration markets.

This package is a separate strategy from the cross-platform arbitrage engine
in the repo root. It is intentionally isolated so that the old arb bot and
this new snipe bot can share clients, loggers, and deploy stack without
coupling their configuration or kill switches.
"""
