-- V00074 — seed the discount-grid ladders (the V2 cliff sweep as data)
-- =============================================================================
-- WHY THIS EXISTS
--
-- The site's "affordability has an exchange rate" grid (discount depth x funding
-- level) was a V1 measurement: 1,440 runs in an experiment-era table, 10 seeds a
-- cell, pre-provenance, engine 0.5.0. Gray's ruling for the V2 release: nothing
-- V1-cited remains on the page — so the grid is re-swept on V2, and per the V2
-- provenance standard every swept projection must exist as SEEDED DATA in the
-- shipped baseline (V00072 established the pattern; runtime cloning and global
-- knob flips are the anomaly this line of work exists to eliminate).
--
-- THE GRID (per depth: 10 main rungs $5.0M-$11.0M + 6 extension rungs $2.0M-$4.5M)
--   standard (10%)     200-209 / 300-305   pre-existing (V00034 / V00072)
--   deep-discount-25   400-409 / 500-505   pre-existing (V00072)
--   deep-discount-15    600-609 /  700-705   seeded here
--   deep-discount-20    800-809 /  900-905   seeded here
--   deep-discount-30   1000-1009 / 1100-1105  seeded here
--   deep-discount-35   1200-1209 / 1300-1305  seeded here
--   deep-discount-40   1400-1409 / 1500-1505  seeded here
--   deep-discount-45   1600-1609 / 1700-1705  seeded here
--   deep-discount-50   1800-1809 / 1900-1905  seeded here
--
-- ID blocks continue the V00072 convention (main ladder xx00-xx09, extension
-- xx00-xx05 in the next block). The offsets are a convenience, NOT the source of
-- truth: Kind and ScenarioTag on reference.Projection are what consumers read.
--
-- Mechanism identical to V00072: each projection carries its own
-- PROP.BelowMarketRentPct override; the default (0.1000) is never mutated.
-- (load_globals() blindness caveat from V00072 applies unchanged — below-market
-- rent is read through the projection-scoped loader; verified there.)
--
-- Idempotent: guarded per projection; safe to re-apply.
-- =============================================================================

SET NOCOUNT ON;

DECLARE @depths TABLE (
    Pct      nvarchar(400) NOT NULL,   -- the BelowMarketRentPct override value
    PctLabel varchar(4)    NOT NULL,   -- for names: '15' -> DeepDiscount15_...
    Tag      varchar(40)   NOT NULL,
    MainBase int           NOT NULL,   -- main-ladder IDs = MainBase + 0..9
    ExtBase  int           NOT NULL    -- extension IDs   = ExtBase  + 0..5
);
INSERT INTO @depths (Pct, PctLabel, Tag, MainBase, ExtBase) VALUES
    (N'0.1500', '15', 'deep-discount-15',  600,  700),
    (N'0.2000', '20', 'deep-discount-20',  800,  900),
    (N'0.3000', '30', 'deep-discount-30', 1000, 1100),
    (N'0.3500', '35', 'deep-discount-35', 1200, 1300),
    (N'0.4000', '40', 'deep-discount-40', 1400, 1500),
    (N'0.4500', '45', 'deep-discount-45', 1600, 1700),
    (N'0.5000', '50', 'deep-discount-50', 1800, 1900);

-- The funding ladder. Slot 0-9 = main (sourced from its standard counterpart
-- 200-209); slot 10-15 = extension capitals (sourced from 206, exactly as V00072's
-- 500-505 were — the 300-305 rows are copies of 206 with only capital changed, and
-- sourcing from the canonical template keeps every grid cell one hop from 206).
DECLARE @rungs TABLE (Slot int NOT NULL, SrcID int NOT NULL, Funds decimal(18,2) NOT NULL);
INSERT INTO @rungs (Slot, SrcID, Funds) VALUES
    (0, 200,  5000000.00), (1, 201,  5500000.00), (2, 202,  6000000.00),
    (3, 203,  6500000.00), (4, 204,  7000000.00), (5, 205,  7500000.00),
    (6, 206,  8000000.00), (7, 207,  9000000.00), (8, 208, 10000000.00),
    (9, 209, 11000000.00),
    (10, 206, 2000000.00), (11, 206, 2500000.00), (12, 206, 3000000.00),
    (13, 206, 3500000.00), (14, 206, 4000000.00), (15, 206, 4500000.00);

DECLARE @plan TABLE (
    NewID int NOT NULL PRIMARY KEY, SrcID int NOT NULL, Funds decimal(18,2) NOT NULL,
    Nm nvarchar(200) NOT NULL, Tag varchar(40) NOT NULL, Bmr nvarchar(400) NOT NULL,
    PctLabel varchar(4) NOT NULL
);
INSERT INTO @plan (NewID, SrcID, Funds, Nm, Tag, Bmr, PctLabel)
SELECT CASE WHEN r.Slot <= 9 THEN d.MainBase + r.Slot ELSE d.ExtBase + (r.Slot - 10) END,
       r.SrcID, r.Funds,
       N'DeepDiscount' + d.PctLabel + N'_$'
           + CONVERT(nvarchar(20), CONVERT(decimal(18,1), r.Funds / 1000000.0)) + N'M',
       d.Tag, d.Pct, d.PctLabel
FROM @depths d CROSS JOIN @rungs r;

-- --- 1. identity (FK target — must precede the override rows) ---------------------
INSERT INTO reference.Projection (ProjectionID, Name, Description, Kind, ScenarioTag)
SELECT p.NewID, p.Nm,
       N'Discount-grid rung — $' + CONVERT(nvarchar(20), CONVERT(decimal(18,1), p.Funds / 1000000.0))
       + N'M starting funds under the deep-discount-' + p.PctLabel + N' scenario: rents set '
       + p.PctLabel + N'% below market instead of the shipped 10%. One cell of the V2 '
       + N'affordability-exchange-rate grid (discount depth x funding level); identical to its '
       + N'standard counterpart in every other respect.',
       'scenario', p.Tag
FROM @plan p
WHERE NOT EXISTS (SELECT 1 FROM reference.Projection x WHERE x.ProjectionID = p.NewID);

-- --- 2. copy the source override set, replacing starting funds ---------------------
INSERT INTO reference.ParameterRegistryDefined (ProjectionID, Category, Name, Value, DataType, Description)
SELECT p.NewID, src.Category, src.Name,
       CASE WHEN src.Category = 'FIN' AND src.Name = 'StartingFunds'
                 THEN CONVERT(nvarchar(50), p.Funds)
            ELSE src.Value END,
       src.DataType,
       CASE WHEN src.Category = 'FIN' AND src.Name = 'StartingFunds'
                 THEN N'Starting capital for this grid rung.'
            ELSE src.Description END
FROM @plan p
JOIN reference.ParameterRegistryDefined src ON src.ProjectionID = p.SrcID
WHERE (src.Category <> 'PROP' OR src.Name <> 'BelowMarketRentPct')   -- depth set explicitly below
  AND NOT EXISTS (
    SELECT 1 FROM reference.ParameterRegistryDefined x
    WHERE x.ProjectionID = p.NewID AND x.Category = src.Category AND x.Name = src.Name);

-- --- 3. every grid projection carries its own depth override ------------------------
INSERT INTO reference.ParameterRegistryDefined (ProjectionID, Category, Name, Value, DataType, Description)
SELECT p.NewID, 'PROP', 'BelowMarketRentPct', p.Bmr, d.DataType,
       N'deep-discount-' + p.PctLabel + N' scenario: rents ' + p.PctLabel
       + N'% below market instead of the shipped 10%. Scoped to this projection so the '
       + N'default (0.1000) is never mutated.'
FROM @plan p
CROSS APPLY (SELECT DataType FROM reference.ParameterRegistryDefault
             WHERE Category = 'PROP' AND Name = 'BelowMarketRentPct') d
WHERE NOT EXISTS (
      SELECT 1 FROM reference.ParameterRegistryDefined x
      WHERE x.ProjectionID = p.NewID AND x.Category = 'PROP' AND x.Name = 'BelowMarketRentPct');
GO

-- --- assertions -----------------------------------------------------------------------
-- Fail loud rather than leave a half-seeded grid that would sweep silently wrong.

IF (SELECT COUNT(*) FROM reference.Projection
    WHERE ScenarioTag LIKE 'deep-discount-%' AND ScenarioTag <> 'deep-discount-25') <> 112
    THROW 51240, 'V00074: expected 112 grid projections (7 depths x 16 rungs)', 1;

-- Every grid projection carries exactly its depth's override value.
IF EXISTS (SELECT 1 FROM reference.Projection p
           JOIN (VALUES ('deep-discount-15', N'0.1500'), ('deep-discount-20', N'0.2000'),
                        ('deep-discount-30', N'0.3000'), ('deep-discount-35', N'0.3500'),
                        ('deep-discount-40', N'0.4000'), ('deep-discount-45', N'0.4500'),
                        ('deep-discount-50', N'0.5000')) v(tag, pct) ON v.tag = p.ScenarioTag
           WHERE NOT EXISTS (SELECT 1 FROM reference.ParameterRegistryDefined x
                             WHERE x.ProjectionID = p.ProjectionID
                               AND x.Category = 'PROP' AND x.Name = 'BelowMarketRentPct'
                               AND x.Value = v.pct))
    THROW 51241, 'V00074: a grid projection lacks its depth''s BelowMarketRentPct override', 1;

-- The default must be untouched.
IF NOT EXISTS (SELECT 1 FROM reference.ParameterRegistryDefault
               WHERE Category = 'PROP' AND Name = 'BelowMarketRentPct' AND Value = N'0.1000')
    THROW 51242, 'V00074: default PROP.BelowMarketRentPct is not 0.1000 — a depth leaked to the default', 1;

-- Funding landed correctly at each block's corners.
IF EXISTS (SELECT 1 FROM (VALUES (600,N'5000000.00'),(609,N'11000000.00'),(700,N'2000000.00'),(705,N'4500000.00'),
                                 (1800,N'5000000.00'),(1809,N'11000000.00'),(1900,N'2000000.00'),(1905,N'4500000.00')) v(id, funds)
           WHERE NOT EXISTS (SELECT 1 FROM reference.ParameterRegistryDefined d
                             WHERE d.ProjectionID = v.id AND d.Category = 'FIN'
                               AND d.Name = 'StartingFunds' AND d.Value = v.funds))
    THROW 51243, 'V00074: a grid rung has the wrong FIN.StartingFunds', 1;

-- Each grid projection's Defined row count must equal its source's (minus nothing:
-- the depth override replaces the source's absent BMR row 1:1 only when the source
-- carried one — corner-check via 606 vs 206 +1 instead of a fragile global equality).
IF (SELECT COUNT(*) FROM reference.ParameterRegistryDefined WHERE ProjectionID = 606)
   <> (SELECT COUNT(*) FROM reference.ParameterRegistryDefined WHERE ProjectionID = 206) + 1
    THROW 51244, 'V00074: projection 606''s override set does not mirror 206 + the depth row', 1;
