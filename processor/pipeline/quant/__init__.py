"""
Quant stage: point-in-time Z-factor standardization over the market data the processor ingests.

    quant-sync     universe snapshot from rag-collector (UNIVERSE_DB) + typed price/fundamentals rows from the
                   LanceDB `documents` table (record-mode tabular chunks) -> QUANT_DB (SQLite)
    quant-zfactor  cross-sectional Z-factors, size brackets, target bracket (zero lookahead) -> QUANT_DB and the
                   `quant_factors` LanceDB table

store.py and z_factor.py are Sanidhya's company_pipeline code (Unified_Pipeline), moved without logic changes.
"""
