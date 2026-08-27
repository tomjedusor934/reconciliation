/* =========================================================================
   WERO — datamart diagnostics
   =========================================================================

   Replay these on every environment (acceptance, then production). Each block
   states what it settles and which `parser_config` key it sets — the WERO flow
   is NOT recalibrated by a redeploy: it is a JSON edit on the `wero` source's
   `parser_config`, from Settings -> Flows.

   Context: the flow is shipped and seeded INACTIVE. Current defaults:
     wero_table               = std.Wero        (confirmed, prod query 1)
     payment_e2e_column       = EndToEndId      (confirmed, prod query 2)
     payment_init_modules     = WERO, WEROEMC   (confirmed, prod query 3)
     wero_settlement_statuses = []  (= all)     -> block A
     wero_reversal_statuses   = []  (= none)    -> block C
     payment_currency_column / return_amount_column / return_date_column
                              = null (safe fallbacks) -> block D

   PERFORMANCE: every block first narrows std.Payment to WERO payments in a
   CTE. Without that, the REPLACE()/UPPER() joins are not sargable and scan the
   ~24M rows of std.Payment.
   ========================================================================= */


/* -------------------------------------------------------------------------
   A — Match rate PER SettlementStatus
   Sets: wero_settlement_statuses

   The question: does a Failed/Rejected WERO transaction ever become a Finacle
   payment? If its rate is ~0 it never reaches Finacle and would sit in pending
   credit forever in the operational view — the whitelist should then be
   narrowed to the statuses that do match.

   COUNT(DISTINCT ...) rather than COUNT(*): a fanning-out LEFT JOIN counts the
   same WERO row several times. That is the bias in the original query 5, which
   read 107/619 on acceptance.
   ------------------------------------------------------------------------- */
WITH wero_payments AS (
    SELECT PaymentNumber,
           REPLACE(UPPER(LTRIM(RTRIM(EndToEndId))), '-', '') AS e2e
    FROM std.Payment
    WHERE IsCurrent = 1
      AND InitModule IN ('WERO', 'WEROEMC')
)
SELECT w.SettlementStatus,
       w.TransactionDirection,
       COUNT(DISTINCT w.CaptureIDMoneyTransferID) AS wero_rows,
       COUNT(DISTINCT CASE WHEN p.PaymentNumber IS NOT NULL
                           THEN w.CaptureIDMoneyTransferID END) AS matched,
       CAST(100.0 * COUNT(DISTINCT CASE WHEN p.PaymentNumber IS NOT NULL
                                        THEN w.CaptureIDMoneyTransferID END)
            / NULLIF(COUNT(DISTINCT w.CaptureIDMoneyTransferID), 0) AS decimal(5,1)) AS pct
FROM std.Wero w
LEFT JOIN wero_payments p
       ON p.e2e = REPLACE(UPPER(LTRIM(RTRIM(w.OriginatorReference))), '-', '')
WHERE w.IsCurrent = 1
GROUP BY w.SettlementStatus, w.TransactionDirection
ORDER BY wero_rows DESC;


/* -------------------------------------------------------------------------
   B — Alternative join keys, measured on the SAME denominator
   Sets: payment_e2e_column (and whether a fallback chain is worth it)

   std.Payment exposes three candidate references (EndToEndId, Uetr,
   TransactionRef) and std.Wero two (OriginatorReference,
   CaptureIDMoneyTransferID). Only change the key if the numbers justify it:
   k1 is what is implemented today.

   The multiple LEFT JOINs multiply rows; COUNT(DISTINCT) on the WERO key is
   immune to that.
   ------------------------------------------------------------------------- */
WITH wp AS (
    SELECT PaymentNumber,
           REPLACE(UPPER(LTRIM(RTRIM(EndToEndId))),     '-', '') AS e2e,
           REPLACE(UPPER(LTRIM(RTRIM(Uetr))),           '-', '') AS uetr,
           REPLACE(UPPER(LTRIM(RTRIM(TransactionRef))), '-', '') AS txnref
    FROM std.Payment
    WHERE IsCurrent = 1
      AND InitModule IN ('WERO', 'WEROEMC')
), wr AS (
    SELECT CaptureIDMoneyTransferID,
           REPLACE(UPPER(LTRIM(RTRIM(OriginatorReference))),      '-', '') AS org_ref,
           REPLACE(UPPER(LTRIM(RTRIM(CaptureIDMoneyTransferID))), '-', '') AS capture_id
    FROM std.Wero
    WHERE IsCurrent = 1
)
SELECT COUNT(DISTINCT wr.CaptureIDMoneyTransferID) AS wero_rows,
       COUNT(DISTINCT CASE WHEN p1.PaymentNumber IS NOT NULL THEN wr.CaptureIDMoneyTransferID END) AS k1_e2e_x_origref,
       COUNT(DISTINCT CASE WHEN p2.PaymentNumber IS NOT NULL THEN wr.CaptureIDMoneyTransferID END) AS k2_uetr_x_captureid,
       COUNT(DISTINCT CASE WHEN p3.PaymentNumber IS NOT NULL THEN wr.CaptureIDMoneyTransferID END) AS k3_e2e_x_captureid,
       COUNT(DISTINCT CASE WHEN p4.PaymentNumber IS NOT NULL THEN wr.CaptureIDMoneyTransferID END) AS k4_txnref_x_origref
FROM wr
LEFT JOIN wp p1 ON p1.e2e    = wr.org_ref     -- what is implemented today
LEFT JOIN wp p2 ON p2.uetr   = wr.capture_id
LEFT JOIN wp p3 ON p3.e2e    = wr.capture_id
LEFT JOIN wp p4 ON p4.txnref = wr.org_ref;


/* -------------------------------------------------------------------------
   C — The WERO counterpart of a Finacle return
   Sets: wero_reversal_statuses

   std.Wero carries NO reversal status (its four values are Accepted / Failed /
   Rejected / Settled), so nothing can currently pair with a std.[Return] under
   the #RET suffix: returns stay in pending debit, visible and unmatched.

   This lists the returns of WERO payments alongside the std.Wero row of the
   original payment. If returns systematically land on one status (Rejected?),
   that status is the counterpart we are looking for.
   ------------------------------------------------------------------------- */
SELECT r.PaymentNumber        AS return_po,
       r.OriginalPO           AS original_po,
       r.Status               AS return_status,
       r.ReturnReasonCode,
       r.ReturnSettlementAmount,
       p.EndToEndId,
       p.InitModule,
       p.Status               AS payment_status,
       w.SettlementStatus     AS wero_status,
       w.TransactionDirection AS wero_direction,
       w.TransactionAmount    AS wero_amount
FROM std.[Return] r
INNER JOIN std.Payment p
        ON p.PaymentNumber = r.OriginalPO
       AND p.IsCurrent = 1
       AND p.InitModule IN ('WERO', 'WEROEMC')
LEFT JOIN std.Wero w
       ON REPLACE(UPPER(LTRIM(RTRIM(w.OriginatorReference))), '-', '')
        = REPLACE(UPPER(LTRIM(RTRIM(p.EndToEndId))), '-', '')
      AND w.IsCurrent = 1
WHERE r.IsCurrent = 1
ORDER BY r.CreatedOn DESC;

-- Aggregated: which WERO status accompanies a return
WITH wero_returns AS (
    SELECT DISTINCT w.SettlementStatus, r.PaymentNumber
    FROM std.[Return] r
    INNER JOIN std.Payment p
            ON p.PaymentNumber = r.OriginalPO AND p.IsCurrent = 1
           AND p.InitModule IN ('WERO', 'WEROEMC')
    LEFT JOIN std.Wero w
           ON REPLACE(UPPER(LTRIM(RTRIM(w.OriginatorReference))), '-', '')
            = REPLACE(UPPER(LTRIM(RTRIM(p.EndToEndId))), '-', '')
          AND w.IsCurrent = 1
    WHERE r.IsCurrent = 1
)
SELECT COALESCE(SettlementStatus, '(no WERO row)') AS wero_status,
       COUNT(*) AS returns
FROM wero_returns
GROUP BY SettlementStatus
ORDER BY returns DESC;


/* -------------------------------------------------------------------------
   D — Columns that EXIST but may not be POPULATED
   Sets: payment_currency_column, return_amount_column, return_date_column

   Those three keys are deliberately left null in parser_config: they feed the
   currency (part of the match group key) or the amount. An empty or
   inconsistent column would break matching silently, or turn every return into
   a row error.

   Only fill them in if the non-NULL count is close to the total.
   ------------------------------------------------------------------------- */
SELECT 'std.Payment' AS tbl,
       COUNT(*)                  AS total_wero_payments,
       COUNT(SettlementCurrency) AS SettlementCurrency_non_null,
       COUNT(SettlementCcy)      AS SettlementCcy_non_null,
       COUNT(SettlementAmount)   AS SettlementAmount_non_null,
       COUNT(SettlementAmt)      AS SettlementAmt_non_null,
       COUNT(EndToEndId)         AS EndToEndId_non_null,
       COUNT(Uetr)               AS Uetr_non_null,
       COUNT(CreatedOn)          AS CreatedOn_non_null
FROM std.Payment
WHERE IsCurrent = 1 AND InitModule IN ('WERO', 'WEROEMC');

-- Currencies actually carried by WERO payments. If anything other than EUR
-- shows up, payment_currency_column MUST be set: otherwise the Finacle leg
-- falls back to default_currency=EUR and can never match its WERO counterpart,
-- because currency is part of the match group key.
SELECT SettlementCurrency, SettlementCcy, COUNT(*) AS n
FROM std.Payment
WHERE IsCurrent = 1 AND InitModule IN ('WERO', 'WEROEMC')
GROUP BY SettlementCurrency, SettlementCcy;

SELECT 'std.[Return]' AS tbl,
       COUNT(*)                        AS total_wero_returns,
       COUNT(r.ReturnSettlementAmount) AS ReturnSettlementAmount_non_null,
       COUNT(r.ReturnSettlementCcy)    AS ReturnSettlementCcy_non_null,
       COUNT(r.CreatedOn)              AS CreatedOn_non_null,
       COUNT(r.SettlementDate)         AS SettlementDate_non_null,
       COUNT(r.Status)                 AS Status_non_null,
       COUNT(r.ReturnReasonCode)       AS ReturnReasonCode_non_null
FROM std.[Return] r
INNER JOIN std.Payment p
        ON p.PaymentNumber = r.OriginalPO AND p.IsCurrent = 1
       AND p.InitModule IN ('WERO', 'WEROEMC')
WHERE r.IsCurrent = 1;

-- Currency on the WERO side. Must agree with the query above, otherwise no
-- group will ever balance: currency is part of the match group key.
SELECT Currency, COUNT(*) AS n
FROM std.Wero WHERE IsCurrent = 1 GROUP BY Currency;


/* -------------------------------------------------------------------------
   E — std.Wero empty on production: is the ODS empty too?
   Sets nothing — tells "no data yet" apart from "datamart load has not run",
   and allows manual investigation before std.Wero is fed.

   The prototype (wero_reconciliation_process.py) reads WERO from the ODS, NOT
   from the datamart: <ods_db>.WERO.RECONCILIATION, through the ODS / ODSP
   Airflow connection. No API is involved, it is plain SQL. std.Wero is
   therefore loaded DOWNSTREAM of that table.

   The production ODS database name is not in this repository: it is returned
   by resolve_ods_db() in
       /POST_HOME/programs/datamart/utils/parallel_run.py
   on the Airflow host (map: ACC -> ODS, PRD -> ODSP, TNG -> ODST).
   Replace <ods_prd> below with that value.
   ------------------------------------------------------------------------- */
SELECT COUNT(*) AS ods_wero_rows FROM <ods_prd>.WERO.RECONCILIATION;

SELECT TOP 50 CaptureIDMoneyTransferID, OriginatorReference, TransactionAmount,
              Currency, SettlementStatus, TransactionDirection,
              SettlementRelatedTimestamp
FROM <ods_prd>.WERO.RECONCILIATION
WHERE CaptureIDMoneyTransferID IS NOT NULL
ORDER BY SettlementRelatedTimestamp DESC;

-- The WERO payments already on production (21 WERO + 3 WEROEMC on 2026-08-26):
-- do they have their ODS counterpart? Few enough to eyeball, and this is the
-- test that says whether the WERO capture is alive.
SELECT p.PaymentNumber, p.EndToEndId, p.Uetr, p.InitModule, p.Status,
       p.SettlementAmount, p.SettlementCurrency, p.CreatedOn,
       o.CaptureIDMoneyTransferID, o.SettlementStatus AS ods_wero_status
FROM std.Payment p
LEFT JOIN <ods_prd>.WERO.RECONCILIATION o
       ON REPLACE(UPPER(LTRIM(RTRIM(o.OriginatorReference))), '-', '')
        = REPLACE(UPPER(LTRIM(RTRIM(p.EndToEndId))), '-', '')
WHERE p.IsCurrent = 1
  AND p.InitModule IN ('WERO', 'WEROEMC')
ORDER BY p.CreatedOn DESC;
