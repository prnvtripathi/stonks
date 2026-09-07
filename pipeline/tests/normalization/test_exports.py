def test_normalization_import_surface_keeps_nse_and_amfi_exports() -> None:
    import market_pipeline.normalization as normalization
    from market_pipeline.normalization import (
        AmfiScheme,
        AmfiSchemeBatch,
        NseInstrumentBatch,
        NseRowError,
        RejectedNseRow,
        normalize_amfi_schemes,
        normalize_nse_rows,
    )

    assert all(
        value is not None
        for value in (
            AmfiScheme,
            AmfiSchemeBatch,
            NseInstrumentBatch,
            NseRowError,
            RejectedNseRow,
            normalize_amfi_schemes,
            normalize_nse_rows,
        )
    )
    assert {
        "AmfiScheme",
        "AmfiSchemeBatch",
        "NseInstrumentBatch",
        "NseRowError",
        "RejectedNseRow",
        "normalize_amfi_schemes",
        "normalize_nse_rows",
    }.issubset(set(normalization.__all__))
