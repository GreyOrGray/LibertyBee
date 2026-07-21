-- corpus_schema.sql — the Liberty Bee corpus (v1.*) schema of record.
--
-- Creates the `v1` schema and every corpus result table. Corpus rows are tagged
-- by (Rung, Seed) rather than RunID: a corpus aggregates many independent sims,
-- each of which ran in its own freshly-restored worker database where RunID is
-- always 1. (Architecture B — see REPRODUCE.md.)
--
-- Applied ONCE to a central corpus database, NOT to ephemeral worker DBs. Its
-- lifecycle is deliberately outside the V###### engine migration sequence: the
-- corpus is an analysis artifact, not part of the simulation database.
--
-- Application — normally via the bootstrap, which also creates the database:
--   python create_corpus.py --corpus <corpus_db>
-- Or directly against an existing database:
--   sqlcmd -S localhost -d <corpus_db> -i corpus_schema.sql
--
-- Idempotent: every schema/table/column creation is IF NOT EXISTS guarded, so
-- re-running against a populated corpus is a no-op and safely upgrades an older
-- corpus in place.
--
-- Provenance: promoted 2026-07-20 from the phase-1.6 store
-- (scratch/sql/setup/v03r_baseline_results_store.sql) with the two V2-cohort
-- deltas folded in — v1.property_units.AdjustedRent (V00066 / KD-012) and
-- v1.compliance — so this file is the single canonical corpus schema rather than
-- a historical base plus patches. Before promotion the corpus schema existed only
-- inside shipped .bak files and was absent from the public bundle entirely.

-- Required for the PERSISTED computed columns in v1.run_summary.
SET QUOTED_IDENTIFIER ON;
GO

-- ---------------------------------------------------------------------------
-- Schema
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'v1')
    EXEC('CREATE SCHEMA v1');
GO

-- ---------------------------------------------------------------------------
-- v1.run_summary — one row per (Rung, Seed) sim
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'run_summary' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.run_summary (
        RunSummaryID    INT IDENTITY(1,1) PRIMARY KEY,
        Rung            DECIMAL(18,2)   NOT NULL,   -- $5000000.00 .. $7500000.00
        Seed            INT             NOT NULL,
        ProjectionID    INT             NOT NULL,   -- 200..205
        EngineVersion   NVARCHAR(20)    NOT NULL,   -- "1.0.0" for Phase 3.10.6
        EphemeralDBName NVARCHAR(200)   NOT NULL,   -- audit trail
        StartedAtUtc    DATETIME2       NOT NULL,
        CompletedAtUtc  DATETIME2       NOT NULL,
        FinalCash       DECIMAL(18,2)   NOT NULL,
        FinalCSF        DECIMAL(18,2)   NOT NULL,
        FinalEIP        DECIMAL(18,2)   NOT NULL,
        FinalCashCSF    AS (FinalCash + FinalCSF) PERSISTED,
        FinalTotal      AS (FinalCash + FinalCSF + FinalEIP) PERSISTED,
        Survived        BIT             NOT NULL,   -- 3.9.6A criterion: FinalCash + FinalCSF > 0
        LeaseCount      INT             NOT NULL,
        PropertyCount   INT             NOT NULL,
        UnitCount       INT             NOT NULL,
        HouseholdCount  INT             NOT NULL,
        PersonCount     INT             NOT NULL,   -- SUM(AdultCount + ChildCount) across all households
        AvgHouseholdSize AS (CASE WHEN HouseholdCount > 0 THEN CAST(PersonCount AS DECIMAL(10,4)) / HouseholdCount ELSE 0 END) PERSISTED,
        EvictionCount   INT             NOT NULL,
        LNRCount        INT             NOT NULL,
        EarlyBreakCount INT             NOT NULL,
        VolExitCount    INT             NOT NULL,
        RenewalCount    INT             NOT NULL,
        TCSAccruedTotal DECIMAL(18,2)   NOT NULL,
        TCSRedeemedTotal DECIMAL(18,2)  NOT NULL,
        TCSForfeitedTotal DECIMAL(18,2) NOT NULL,
        Notes           NVARCHAR(MAX)   NULL,
        CONSTRAINT UQ_v1_run_summary_rung_seed UNIQUE (Rung, Seed)
    );
    CREATE INDEX IX_v1_run_summary_Rung ON v1.run_summary (Rung);
    CREATE INDEX IX_v1_run_summary_Survived ON v1.run_summary (Survived);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.fund_ledger — FundLedger rows tagged (Rung, Seed)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'fund_ledger' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.fund_ledger (
        FundLedgerID    BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung            DECIMAL(18,2)   NOT NULL,
        Seed            INT             NOT NULL,
        EventID         BIGINT             NOT NULL,
        LedgerDate      DATE            NOT NULL,
        CashDebit       DECIMAL(18,2)   NOT NULL,
        CashCredit      DECIMAL(18,2)   NOT NULL,
        CashBalance     DECIMAL(18,2)   NOT NULL,
        CSFDebit        DECIMAL(18,2)   NOT NULL,
        CSFCredit       DECIMAL(18,2)   NOT NULL,
        CSFBalance      DECIMAL(18,2)   NOT NULL,
        EIPDebit        DECIMAL(18,2)   NOT NULL,
        EIPCredit       DECIMAL(18,2)   NOT NULL,
        EIPBalance      DECIMAL(18,2)   NOT NULL,
        CashHoldCredit  DECIMAL(18,2)   NOT NULL,
        CashHoldDebit   DECIMAL(18,2)   NOT NULL,
        CashHoldBalance DECIMAL(18,2)   NOT NULL,
        EscrowDebit     DECIMAL(18,2)   NOT NULL,
        EscrowCredit    DECIMAL(18,2)   NOT NULL,
        EscrowBalance   DECIMAL(18,2)   NOT NULL
    );
    CREATE INDEX IX_v1_fund_ledger_RungSeedDate ON v1.fund_ledger (Rung, Seed, LedgerDate DESC, EventID DESC);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.tcs_ledger — TenantCreditLedger rows tagged (Rung, Seed)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tcs_ledger' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.tcs_ledger (
        TCSLedgerID         BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                DECIMAL(18,2)   NOT NULL,
        Seed                INT             NOT NULL,
        CreditTransactionID INT             NOT NULL,
        HouseholdID         INT             NOT NULL,
        TransactionDate     DATE            NOT NULL,
        TransactionType     VARCHAR(20)     NOT NULL,
        Amount              DECIMAL(18,2)   NOT NULL,
        BalanceAfter        DECIMAL(18,2)   NOT NULL,
        RelatedCollectionID INT             NULL,
        RelatedLeaseID      INT             NULL,
        CreatedEventID      BIGINT             NOT NULL,
        Notes               NVARCHAR(500)   NULL
    );
    CREATE INDEX IX_v1_tcs_ledger_RungSeedType ON v1.tcs_ledger (Rung, Seed, TransactionType);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.lease — Lease rows tagged (Rung, Seed)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'lease' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.lease (
        LeaseRowID                  BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                        DECIMAL(18,2)   NOT NULL,
        Seed                        INT             NOT NULL,
        LeaseID                     INT             NOT NULL,
        HouseholdID                 INT             NOT NULL,
        UnitID                      INT             NOT NULL,
        VacancyID                   INT             NOT NULL,
        LeaseSignedDate             DATE            NOT NULL,
        LeaseStartDate              DATE            NOT NULL,
        LeaseEndDate                DATE            NOT NULL,
        MonthlyRent                 DECIMAL(18,2)   NOT NULL,
        LeaseStatus                 NVARCHAR(20)    NOT NULL,
        TerminationDate             DATE            NULL,
        TerminationReason           NVARCHAR(100)   NULL,
        ConsecutiveMissedPayments   INT             NOT NULL,
        RenewalDecision             NVARCHAR(30)    NULL,
        CumulativeRentReductionPct  DECIMAL(18,4)   NOT NULL,
        EffectiveMonthlyRent        DECIMAL(18,2)   NOT NULL
    );
    CREATE INDEX IX_v1_lease_RungSeed ON v1.lease (Rung, Seed);
    CREATE INDEX IX_v1_lease_RungSeedHousehold ON v1.lease (Rung, Seed, HouseholdID);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.lease_termination — LeaseTermination rows tagged (Rung, Seed)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'lease_termination' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.lease_termination (
        LeaseTerminationRowID       BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                        DECIMAL(18,2)   NOT NULL,
        Seed                        INT             NOT NULL,
        LeaseID                     INT             NOT NULL,
        TerminationType             NVARCHAR(50)    NOT NULL,
        TerminationDate             DATE            NOT NULL,
        TerminationReason           NVARCHAR(255)   NULL,
        ArrearsAtExit               DECIMAL(18,2)   NOT NULL,
        DepositWithheldAmount       DECIMAL(18,2)   NOT NULL,
        DepositForfeited            BIT             NOT NULL,
        EarlyBreakPenalty           DECIMAL(18,2)   NULL,
        EvictionFiledDate           DATE            NULL,
        EvictionExecutionDate       DATE            NULL
    );
    CREATE INDEX IX_v1_lease_termination_RungSeedType ON v1.lease_termination (Rung, Seed, TerminationType);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.lease_termination_ledger — LeaseTerminationLedger rows tagged (Rung, Seed)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'lease_termination_ledger' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.lease_termination_ledger (
        LTLedgerRowID   BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung            DECIMAL(18,2)   NOT NULL,
        Seed            INT             NOT NULL,
        LedgerID        BIGINT          NOT NULL,
        LeaseID         INT             NOT NULL,
        EventID         BIGINT             NOT NULL,
        TxnDate         DATE            NOT NULL,
        TxnType         NVARCHAR(50)    NOT NULL,
        Description     NVARCHAR(500)   NULL,
        Amount          DECIMAL(18,2)   NULL,
        Metadata        NVARCHAR(MAX)   NULL
    );
    CREATE INDEX IX_v1_lease_termination_ledger_RungSeedTxn ON v1.lease_termination_ledger (Rung, Seed, TxnType);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.household — Household rows tagged (Rung, Seed)
-- Source: simulation.Household. Used for per-household and per-person
-- tenant-impact averages and demographic breakouts.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'household' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.household (
        HouseholdRowID              BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                        DECIMAL(18,2)   NOT NULL,
        Seed                        INT             NOT NULL,
        HouseholdID                 INT             NOT NULL,
        HouseholdType               NVARCHAR(20)    NOT NULL,
        HouseholdIncomeBand         NVARCHAR(20)    NOT NULL,
        AdultCount                  INT             NOT NULL,
        ChildCount                  INT             NOT NULL,
        PersonCount                 AS (AdultCount + ChildCount) PERSISTED,
        CreatedDate                 DATE            NOT NULL,
        TCSRedemptionProbability    DECIMAL(18,4)   NOT NULL,
        SigningMonthlyIncome        DECIMAL(10,2)   NULL   -- Phase 1.10 (retention): welfare rent-burden metric
    );
    CREATE INDEX IX_v1_household_RungSeed ON v1.household (Rung, Seed);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.properties — Properties rows tagged (Rung, Seed)
-- Source: simulation.Properties. Used for portfolio-growth trajectory
-- (AcquisitionDate enables per-month owned-properties series).
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'properties' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.properties (
        PropertyRowID           BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                    DECIMAL(18,2)   NOT NULL,
        Seed                    INT             NOT NULL,
        PropertyID              INT             NOT NULL,
        PropertyType            NVARCHAR(50)    NOT NULL,
        AcquisitionDate         DATE            NOT NULL,
        BasePrice               DECIMAL(18,2)   NOT NULL,
        InflationAdjustedPrice  DECIMAL(18,2)   NOT NULL,
        TotalUnits              INT             NOT NULL
    );
    CREATE INDEX IX_v1_properties_RungSeed ON v1.properties (Rung, Seed);
    CREATE INDEX IX_v1_properties_RungSeedAcqDate ON v1.properties (Rung, Seed, AcquisitionDate);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.property_units — PropertyUnits rows tagged (Rung, Seed)
-- Source: simulation.PropertyUnits. Used for portfolio-growth (unit counts)
-- and tenant-impact. Phase 1.7c (A2, Gray-ratified): CurrentRent column DROPPED
-- vs the frozen pre-hygiene store — it was exactly BaseRent x (1 - 
-- PROP.BelowMarketRentPct), reconstructible for corpus diffs; the honest
-- below-market read is signed rent (v1.lease) vs BaseRent.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'property_units' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.property_units (
        UnitRowID       BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung            DECIMAL(18,2)   NOT NULL,
        Seed            INT             NOT NULL,
        UnitID          INT             NOT NULL,
        PropertyID      INT             NOT NULL,
        Beds            DECIMAL(4,1)    NOT NULL,
        Baths           DECIMAL(4,1)    NOT NULL,
        -- BaseRent is the bedroom-median provenance anchor; AdjustedRent is the
        -- bath-adjusted basis the engine actually charges (V00066 / KD-012).
        -- Both are kept so the adjustment stays auditable.
        BaseRent        DECIMAL(18,2)   NOT NULL,
        AdjustedRent    DECIMAL(18,2)   NULL
    );
    CREATE INDEX IX_v1_property_units_RungSeed ON v1.property_units (Rung, Seed);
    CREATE INDEX IX_v1_property_units_RungSeedProperty ON v1.property_units (Rung, Seed, PropertyID);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.projection_parameters — locked V1 projection parameters (one row per rung)
-- Source: reference.ProjectionParameters rows with ID IN (200..205). These rows
-- are inserted by V00032 and locked by V00033 inside each ephemeral worker DB,
-- so without this extract the V1 parameter surface vanishes when workers drop.
-- Focused subset (16 cols vs 94 source cols) covers the columns the V1 analyzers
-- actually read; extend the table if a future analyzer needs another column.
-- No Rung/Seed tags because V00033 guarantees identical values across all sims.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'projection_parameters' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.projection_parameters (
        ProjectionID                INT             NOT NULL PRIMARY KEY,
        ProjectionName              NVARCHAR(100)   NOT NULL,
        StartingFunds               DECIMAL(18,2)   NOT NULL,
        BelowMarketRentPct          DECIMAL(18,4)   NOT NULL,
        RentInflationRate           DECIMAL(18,4)   NOT NULL,
        InflationMode               NVARCHAR(50)    NOT NULL,  -- Phase 1.6: leg (Regime|Static); replaces the retired InflationScenarioType
        RR_FirstReductionMonths     INT             NULL,
        RR_FirstReductionPct        DECIMAL(18,4)   NULL,
        RR_SecondReductionMonths    INT             NULL,
        RR_SecondReductionPct       DECIMAL(18,4)   NULL,
        RR_ThirdReductionMonths     INT             NULL,
        RR_ThirdReductionPct        DECIMAL(18,4)   NULL,
        RR_FourthReductionMonths    INT             NULL,
        RR_FourthReductionPct       DECIMAL(18,4)   NULL,
        RR_FifthReductionMonths     INT             NULL,
        RR_FifthReductionPct        DECIMAL(18,4)   NULL
    );
END;
GO

-- ---------------------------------------------------------------------------
-- v1.inflation_schedule — InflationSchedule rows tagged (Rung, Seed)
-- Source: simulation.InflationSchedule. Phase 3.11.2 — extracted so reports
-- can reconstruct month-by-month market rent (PropertyUnits.BaseRent ×
-- cumulative RentRate factor). For V1, every (Rung, Seed) carries the same
-- deterministic Static path; we extract all 504 copies anyway for schema
-- stability (V2 stochastic-inflation work consumes the same structure).
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'inflation_schedule' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.inflation_schedule (
        InflationScheduleRowID  BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                    DECIMAL(18,2)   NOT NULL,
        Seed                    INT             NOT NULL,
        MonthIndex              INT             NOT NULL,
        InflationDate           DATE            NOT NULL,
        GeneralRate             DECIMAL(18,8)   NOT NULL,
        RentRate                DECIMAL(18,8)   NOT NULL,
        OpExRate                DECIMAL(18,8)   NOT NULL,
        PropertyRate            DECIMAL(18,8)   NOT NULL,
        ScenarioType            NVARCHAR(50)    NOT NULL,
        ScenarioPhase           NVARCHAR(50)    NULL,
        Notes                   NVARCHAR(500)   NULL
    );
    CREATE INDEX IX_v1_inflation_schedule_RungSeedMonth ON v1.inflation_schedule (Rung, Seed, MonthIndex);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.event_summary — aggregated event counts per (Rung, Seed)
-- (Full Event table is ~50K rows per sim; we summarize rather than copy verbatim.)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'event_summary' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.event_summary (
        EventSummaryID  BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung            DECIMAL(18,2)   NOT NULL,
        Seed            INT             NOT NULL,
        -- EventType / EntityType / ActionType are NULLABLE because the source
        -- simulation.Event table allows NULL on these columns and our GROUP BY
        -- preserves them as a single NULL bucket per (EventType, ActionType).
        EventType       NVARCHAR(50)    NULL,
        EntityType      NVARCHAR(50)    NULL,
        ActionType      NVARCHAR(50)    NULL,
        EventCount      INT             NOT NULL,
        TotalAmount     DECIMAL(18,2)   NULL
    );
    CREATE INDEX IX_v1_event_summary_RungSeed ON v1.event_summary (Rung, Seed);
END;
GO

-- ===========================================================================
-- Phase 3.12.4 V1 Reviewability Extracts (KD-027 fix bundle)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- v1.monthly_payment_status — per-month lease payment trace (HYBRID extract)
--
-- HYBRID STRATEGY (Kate BA review §4):
--   * For leases that EVER had a non-ON_TIME month (LATE / MISSED / REDEEMED):
--     extract verbatim per-month rows for the entire lease's history.
--   * For leases that were 100% ON_TIME throughout: extract ONE sentinel
--     summary row with MonthIndex = -1 and PaymentStatus = 'ALL_ON_TIME'.
--
-- SENTINEL ROW SEMANTICS:
--   * MonthIndex = -1 is NOT a real simulation month.
--   * PaymentStatus = 'ALL_ON_TIME' is a summary status, NOT an event status.
--   * Queries analyzing month-by-month trajectories MUST exclude MonthIndex = -1.
--   * Queries counting all-clean leases MAY include the sentinel row intentionally.
--   * Every report touching this table MUST document its handling of the sentinel.
--
-- Powers Report 04 distress refinement (Phase 3.12.4.10).
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'monthly_payment_status' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.monthly_payment_status (
        MonthlyPaymentStatusID BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung                   DECIMAL(18,2) NOT NULL,
        Seed                   INT           NOT NULL,
        LeaseID                INT           NOT NULL,
        MonthIndex             INT           NOT NULL,        -- -1 = sentinel; 1+ = real sim month
        PaymentStatus          NVARCHAR(20)  NOT NULL,        -- ON_TIME / LATE / MISSED / REDEEMED / 'ALL_ON_TIME'
        DaysLate               INT           NULL,
        AmountOwed             DECIMAL(10,2) NULL,
        AmountPaid             DECIMAL(10,2) NULL,
        LateFeeAccrued         DECIMAL(10,2) NULL,
        OnTimeMonthsCount      INT           NULL              -- populated only for the sentinel row
    );
    CREATE INDEX IX_v1_monthly_payment_status_RungSeedLease ON v1.monthly_payment_status (Rung, Seed, LeaseID);
    CREATE INDEX IX_v1_monthly_payment_status_StatusMonth ON v1.monthly_payment_status (Rung, Seed, MonthIndex, PaymentStatus);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.employees — per-employee staffing record with denormalized RoleName
--
-- Powers Report 08 Operational Scaling (Phase 3.12.4.11).
-- IsActive is always 1 in V1 (no termination path per KD-020); retained for
-- forward-compat with V2+ workforce attrition modeling. TerminatedDate likewise
-- NULL in V1 and retained for forward-compat.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'employees' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.employees (
        EmployeeRecordID BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung             DECIMAL(18,2)  NOT NULL,
        Seed             INT            NOT NULL,
        EmployeeID       INT            NOT NULL,
        RoleID           INT            NOT NULL,
        RoleName         NVARCHAR(100)  NOT NULL,  -- denormalized from reference.EmployeeRole
        HiredDate        DATE           NOT NULL,
        TerminatedDate   DATE           NULL,      -- always NULL in V1; forward-compat with V2+
        BaseSalary       DECIMAL(18,4)  NOT NULL,  -- end-of-sim value (after all raises)
        BenefitsCost     DECIMAL(18,4)  NOT NULL,
        IsActive         BIT            NOT NULL   -- always 1 in V1
    );
    CREATE INDEX IX_v1_employees_RungSeed ON v1.employees (Rung, Seed);
    CREATE INDEX IX_v1_employees_HiredDate ON v1.employees (Rung, Seed, HiredDate);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.payroll — bi-monthly payroll line items
--
-- Powers Report 08 Operational Scaling. Pairs with v1.employees via
-- (Rung, Seed, EmployeeID).
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'payroll' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.payroll (
        PayrollRecordID BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung            DECIMAL(18,2)  NOT NULL,
        Seed            INT            NOT NULL,
        PayrollID       INT            NOT NULL,
        EmployeeID      INT            NOT NULL,
        PayrollDate     DATE           NOT NULL,
        GrossPay        DECIMAL(18,4)  NOT NULL,
        BenefitsCost    DECIMAL(18,4)  NOT NULL,
        TotalCost       DECIMAL(18,4)  NOT NULL
    );
    CREATE INDEX IX_v1_payroll_RungSeed ON v1.payroll (Rung, Seed);
    CREATE INDEX IX_v1_payroll_Date ON v1.payroll (Rung, Seed, PayrollDate);
    CREATE INDEX IX_v1_payroll_Employee ON v1.payroll (Rung, Seed, EmployeeID);
END;
GO

-- ---------------------------------------------------------------------------
-- v1.compliance — per-work-item compliance detail (V2 cohort)
--
-- The earlier store carried only event_summary aggregates, which cannot separate
-- a compliance item's CASH cost from its OCCUPANCY-DELAY cost. Lead abatement
-- (KD-193) needs exactly that split, so the corpus records each work item.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'compliance' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.compliance (
        ComplianceRowID  BIGINT IDENTITY(1,1) PRIMARY KEY,
        Rung             DECIMAL(18,2) NOT NULL,
        Seed             INT           NOT NULL,
        WorkItemID       INT           NOT NULL,
        PropertyID       INT           NULL,
        UnitID           INT           NULL,
        WorkType         NVARCHAR(60)  NOT NULL,
        Status           NVARCHAR(30)  NULL,
        Severity         NVARCHAR(20)  NULL,
        CostEstimate     DECIMAL(18,2) NULL,
        ActualCost       DECIMAL(18,2) NULL,
        DurationDays     INT           NULL,
        StartDate        DATE          NULL,
        CompletedDate    DATE          NULL
    );
    CREATE INDEX IX_v1_compliance_RungSeed ON v1.compliance (Rung, Seed);
    CREATE INDEX IX_v1_compliance_WorkType ON v1.compliance (Rung, Seed, WorkType);
END;
GO

-- ---------------------------------------------------------------------------
-- In-place upgrade for corpora created before AdjustedRent existed. Fresh
-- corpora already have the column from the CREATE TABLE above; this makes the
-- script safe to re-run against an older corpus.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('v1.property_units') AND name = 'AdjustedRent')
    ALTER TABLE v1.property_units ADD AdjustedRent DECIMAL(18,2) NULL;
GO

-- ---------------------------------------------------------------------------
-- v1.corpus_meta — what generated this corpus. One row per sweep invocation.
--
-- Exists so a corpus can answer two questions about itself rather than relying
-- on anyone's memory:
--   1. "Which scenario am I?"  The sweep driver reads Scenario back and REFUSES
--      to add runs under a different one, so forgetting --scenario on a resumed
--      sweep aborts loudly instead of silently blending two populations into one
--      corpus. (A corpus is single-scenario by design — 'leg = DB'.)
--   2. "What produced me?"  Engine version + harness commit + clean/dirty tree,
--      so "we ran the artifact we ship" is a query, not a claim. A corpus
--      generated from a modified working tree is marked as such, permanently.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'corpus_meta' AND schema_id = SCHEMA_ID('v1'))
BEGIN
    CREATE TABLE v1.corpus_meta (
        MetaID         INT IDENTITY(1,1) PRIMARY KEY,
        Scenario       NVARCHAR(40)   NOT NULL,
        SweepMode      NVARCHAR(20)   NOT NULL,
        EngineVersion  NVARCHAR(20)   NOT NULL,
        HarnessCommit  NVARCHAR(40)   NULL,      -- NULL when not run from a git checkout
        HarnessDirty   BIT            NOT NULL,  -- 1 = generated from a modified tree
        HarnessRoot    NVARCHAR(260)  NULL,
        HostName       NVARCHAR(128)  NULL,
        StartedUTC     DATETIME2(0)   NOT NULL
    );
    CREATE INDEX IX_v1_corpus_meta_Scenario ON v1.corpus_meta (Scenario);
END;
GO

-- ---------------------------------------------------------------------------
-- Final sanity check
-- ---------------------------------------------------------------------------
DECLARE @table_count INT;
SELECT @table_count = COUNT(*)
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name = 'v1';
IF @table_count <> 17
    THROW 51030, 'Corpus schema setup failed: expected 17 v1.* tables (15 from the phase-1.6 store + v1.compliance + v1.corpus_meta)', 1;
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('v1.property_units') AND name = 'AdjustedRent')
    THROW 51031, 'Corpus schema setup failed: v1.property_units.AdjustedRent missing', 1;
PRINT 'Corpus schema ready: ' + CAST(@table_count AS NVARCHAR(10)) + ' tables in schema v1';
GO
