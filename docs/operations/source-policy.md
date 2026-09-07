# Market data source policy

The pipeline automates only the official public artifacts listed below. It does not
scrape undocumented website endpoints or use third-party market-data services.

| Source ID | Official artifacts | Automation |
| --- | --- | --- |
| `nse-eod` | NSE daily reports for end-of-day prices, volume, delivery, security, ETF and related market files | Enabled only while the terms reference permits private automation |
| `nse-filings-xbrl` | NSE corporate filings and available CSV/XBRL artifacts | Enabled only while the terms reference permits private automation |
| `amfi-nav` | AMFI daily and historical mutual-fund NAV data | Enabled |
| `nifty-500` | Official Nifty 500 closing values | Enabled only while the terms reference permits private automation |

Each adapter records its source URL, retrieval timestamp, effective date, SHA-256
checksum, adapter version and terms reference on every artifact. Raw bytes are
immutable. Repeating an identical write is idempotent; attempting to write different
bytes under an existing object key fails.

If a source's usage or retention terms do not permit the intended private automation,
its policy must be disabled. The pipeline must fail closed with a `SourcePolicyError`;
it must not substitute an undocumented endpoint or a third-party feed. Terms and
retention decisions must be reviewed before enabling an adapter in production.
