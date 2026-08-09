"""Reference contract library (Step 5d, platform-layer).

Each subpackage is one financial instrument — a documented Lua contract
script (the *policy*) plus the Python helpers that hold its *data* (terms,
issuance, retirement). The engine ships primitives; this library ships the
instruments built from them, as templates to adopt or fork.

See docs/actors.md "Step 5 design" for the substrate decisions each
instrument validates.
"""
