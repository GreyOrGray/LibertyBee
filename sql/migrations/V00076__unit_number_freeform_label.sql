-- V00076 — portability: free-form unit LABELS (reference.Units.UnitNumber int -> nvarchar)
--
-- Adopters name units however their market does ("Apt A1", "Suite 2", "#3"), so the unit
-- label must be free text, not an integer. reference.Units.UnitNumber was int; the engine
-- already treats it as a STRING everywhere (the compliance rejoin CASTs it to NVARCHAR —
-- compliance_manager.py:1945; KD040 regression test), and nothing does integer math on it,
-- so widening it to nvarchar changes no behavior.
--
-- Identity model (ruling 2026-08-09): the integer engine keys stay ours — UnitID (PK, the
-- importer generates it when omitted) and PropertyID. UnitNumber now carries the adopter's
-- real label AND remains the (PropertyID, UnitNumber) compliance join key (labels are unique
-- within a property, enforced by UQ_Unit_Property).
--
-- Reproduction-preserving: existing integer UnitNumbers become their string form ("1","2"),
-- and every join already string-compares, so the V2 corpus reproduces bit-identically.
-- Portability (V3, phase 1.12). UnitNumber is inside the unique constraint UQ_Unit_Property,
-- so drop it, alter, re-add.

IF EXISTS (SELECT 1 FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id
           WHERE c.object_id = OBJECT_ID('reference.Units') AND c.name = 'UnitNumber' AND t.name = 'int')
BEGIN
    ALTER TABLE reference.Units DROP CONSTRAINT UQ_Unit_Property;
    ALTER TABLE reference.Units ALTER COLUMN UnitNumber nvarchar(50) NOT NULL;
    ALTER TABLE reference.Units ADD CONSTRAINT UQ_Unit_Property UNIQUE (PropertyID, UnitNumber);
END
GO

-- Assert the column is now nvarchar and the unique constraint is restored (house style).
IF NOT EXISTS (SELECT 1 FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id
               WHERE c.object_id = OBJECT_ID('reference.Units') AND c.name = 'UnitNumber' AND t.name = 'nvarchar')
    THROW 50076, 'V00076: reference.Units.UnitNumber was not converted to nvarchar', 1;
GO
IF NOT EXISTS (SELECT 1 FROM sys.key_constraints
               WHERE name = 'UQ_Unit_Property' AND parent_object_id = OBJECT_ID('reference.Units'))
    THROW 50076, 'V00076: UQ_Unit_Property unique constraint was not restored', 1;
GO
