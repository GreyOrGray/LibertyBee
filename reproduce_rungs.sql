-- reproduce_rungs.sql
-- ---------------------------------------------------------------------------
-- Defines the six low-funding projections (IDs 300-305, $2.0M-$4.5M) that make
-- up the under-funded + knee region of the survival curve. They are exact clones
-- of projection 206 ($8.0M) with ONLY the starting capital (and a cosmetic name)
-- changed -- which is exactly how the frozen corpus was generated.
--
-- The ten higher rungs ($5.0M-$11.0M, projections 200-209) already exist inside
-- the released Gold database, so their runs work with no setup. These six do not,
-- so run this file ONCE against a fresh reproduction database (built from Gold via
-- migration_manager) BEFORE running any run whose "ProjectionInGold" column in
-- runs_manifest.csv reads "no".
--
-- Safe to re-run: it clears projections 300-305 first, then rebuilds them.
-- ---------------------------------------------------------------------------

SET NOCOUNT ON;

IF OBJECT_ID('reference.ParameterRegistry') IS NULL
    THROW 50000, 'reference.ParameterRegistry not found -- restore the Gold seed database first.', 1;

IF NOT EXISTS (SELECT 1 FROM reference.ParameterRegistry WHERE ProjectionID = 206)
    THROW 50000, 'Template projection 206 not found -- is this the released Gold database?', 1;

DELETE FROM reference.ParameterRegistry WHERE ProjectionID IN (300, 301, 302, 303, 304, 305);

;WITH rung (ProjectionID, StartingFunds, ProjectionName) AS (
    SELECT 300, '2000000.00', 'Reproduction rung 2.0M' UNION ALL
    SELECT 301, '2500000.00', 'Reproduction rung 2.5M' UNION ALL
    SELECT 302, '3000000.00', 'Reproduction rung 3.0M' UNION ALL
    SELECT 303, '3500000.00', 'Reproduction rung 3.5M' UNION ALL
    SELECT 304, '4000000.00', 'Reproduction rung 4.0M' UNION ALL
    SELECT 305, '4500000.00', 'Reproduction rung 4.5M'
)
INSERT INTO reference.ParameterRegistry (Category, Name, ProjectionID, Value, DataType, Description)
SELECT src.Category, src.Name, r.ProjectionID,
       CASE WHEN src.Category = 'FIN' AND src.Name = 'StartingFunds'  THEN r.StartingFunds
            WHEN src.Category = 'SIM' AND src.Name = 'ProjectionName' THEN r.ProjectionName
            ELSE src.Value END,
       src.DataType, src.Description
FROM reference.ParameterRegistry src
CROSS JOIN rung r
WHERE src.ProjectionID = 206;

-- Sanity check: each new rung should have copied the same number of override rows
-- as the 206 template. If a rung shows fewer, the clone is incomplete -- stop and investigate.
SELECT ProjectionID, COUNT(*) AS RegistryRows
FROM reference.ParameterRegistry
WHERE ProjectionID IN (206, 300, 301, 302, 303, 304, 305)
GROUP BY ProjectionID
ORDER BY ProjectionID;
