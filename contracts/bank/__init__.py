"""Commercial bank — a two-tier money reference contract (Step 5d).

A bank is a ``BANK`` entity whose deposit balances are a *shadow ledger*
in its servicing-script ``state`` -- claims on the bank, created by
lending, NOT engine accounts and NOT base money. :mod:`contracts.bank.bank`
holds the book (the *data*); ``bank.lua`` accrues interest and reconciles
the books each tick (the *policy*). Together they prove the engine is
already a faithful two-tier monetary system: ``issue_money`` is base money;
a bank's deposits are a book.
"""
