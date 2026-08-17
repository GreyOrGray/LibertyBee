-- V00077 — portability config surface (1.12.4a): town-scoped local params
--
-- Section 1 of the config surface (docs/.../config_surface_design.md): local-market-context params
-- (buckets 1+2) can be overridden per TOWN. The engine resolves a `Scope='local'` param for a
-- property as TownParameterOverride[property.Town] ?? ParameterRegistryDefault.
--
-- Section 2 (model-mechanics parameter SETS) REUSES the engine's existing per-config override
-- mechanism — reference.Projection + reference.ParameterRegistryDefined, selected by
-- `simulation.py --projection-id` (a "projection" already IS a named parameter set; the protected
-- canonical baseline is ParameterRegistryDefault). No new tables are needed for it.
--
-- Additive + backward-compatible: a single-town region with NO town overrides resolves every local
-- param to ParameterRegistryDefault = today's behavior, so the V2/Salem corpus reproduces to the
-- penny. Portability, phase 1.12.4a.

-- 1) property TOWN — the importer loads buildings.csv `town` here (was dropped; only State kept).
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('reference.Properties') AND name = 'Town')
    ALTER TABLE reference.Properties ADD Town nvarchar(100) NULL;
GO

-- 2) param SCOPE: 'local' (town-scopable market context, buckets 1+2) | 'model' (universal mechanics).
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('reference.ParameterRegistryDefault') AND name = 'Scope')
    ALTER TABLE reference.ParameterRegistryDefault
        ADD Scope varchar(8) NOT NULL CONSTRAINT DF_PRD_Scope DEFAULT 'model';
GO
-- classify the 45 local (buckets 1+2) params; everything else stays 'model' (the default).
UPDATE reference.ParameterRegistryDefault SET Scope = 'local'
WHERE Category + '.' + Name IN (
    'INC.EarnerMedianAnnual',
    'OPEX.PropertyTaxPerUnit', 'OPEX.InsurancePerUnit',
    'MARKET.DaysOnMarket_Mean',
    'PROP.VacancyRateBase',
    'RET.VacancyRefPct', 'RET.MoverRegionalVacancyPct',
    'INF.Surge_PropertyMean', 'INF.Surge_OpExMean',
    'MARKET.SeasonalMultiplier_Jan','MARKET.SeasonalMultiplier_Feb','MARKET.SeasonalMultiplier_Mar',
    'MARKET.SeasonalMultiplier_Apr','MARKET.SeasonalMultiplier_May','MARKET.SeasonalMultiplier_Jun',
    'MARKET.SeasonalMultiplier_Jul','MARKET.SeasonalMultiplier_Aug','MARKET.SeasonalMultiplier_Sep',
    'MARKET.SeasonalMultiplier_Oct','MARKET.SeasonalMultiplier_Nov','MARKET.SeasonalMultiplier_Dec',
    'MARKET.DaysOnMarket_SeasonalMultiplier_Jan','MARKET.DaysOnMarket_SeasonalMultiplier_Feb','MARKET.DaysOnMarket_SeasonalMultiplier_Mar',
    'MARKET.DaysOnMarket_SeasonalMultiplier_Apr','MARKET.DaysOnMarket_SeasonalMultiplier_May','MARKET.DaysOnMarket_SeasonalMultiplier_Jun',
    'MARKET.DaysOnMarket_SeasonalMultiplier_Jul','MARKET.DaysOnMarket_SeasonalMultiplier_Aug','MARKET.DaysOnMarket_SeasonalMultiplier_Sep',
    'MARKET.DaysOnMarket_SeasonalMultiplier_Oct','MARKET.DaysOnMarket_SeasonalMultiplier_Nov','MARKET.DaysOnMarket_SeasonalMultiplier_Dec',
    'MARKET.PriceMultiplier_Jan','MARKET.PriceMultiplier_Feb','MARKET.PriceMultiplier_Mar',
    'MARKET.PriceMultiplier_Apr','MARKET.PriceMultiplier_May','MARKET.PriceMultiplier_Jun',
    'MARKET.PriceMultiplier_Jul','MARKET.PriceMultiplier_Aug','MARKET.PriceMultiplier_Sep',
    'MARKET.PriceMultiplier_Oct','MARKET.PriceMultiplier_Nov','MARKET.PriceMultiplier_Dec'
);
GO

-- 3) per-town overrides for Scope='local' params (Section 1 "town knobs").
IF OBJECT_ID('reference.TownParameterOverride') IS NULL
    CREATE TABLE reference.TownParameterOverride (
        Town         nvarchar(100) NOT NULL,
        Category     varchar(16)   NOT NULL,
        Name         varchar(64)   NOT NULL,
        Value        nvarchar(400) NOT NULL,
        Source       nvarchar(1000) NULL,
        UpdatedAtUtc datetime2     NOT NULL CONSTRAINT DF_TPO_Upd DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_TownParameterOverride PRIMARY KEY (Town, Category, Name)
    );
GO

-- asserts (house style)
IF (SELECT COUNT(*) FROM reference.ParameterRegistryDefault WHERE Scope = 'local') <> 45
    THROW 50077, 'V00077: expected exactly 45 local-scope params (buckets 1+2)', 1;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('reference.Properties') AND name = 'Town')
    THROW 50077, 'V00077: reference.Properties.Town missing', 1;
GO
