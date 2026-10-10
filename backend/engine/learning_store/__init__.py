"""
backend/engine/learning_store — Phase 28B-1A persistence adapter for learning evidence bindings.

Allowed dependency direction: learning_store -> learning (contracts) and the closed private storage types read through the learning adapters. The pure `learning` package never
imports this package. No database driver, HTTP client, clock, scheduler or economic-decision import: the reader and RPC transport are injected callables.
"""
