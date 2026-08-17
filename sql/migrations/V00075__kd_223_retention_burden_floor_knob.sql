-- V00075 — KD-223: knob-ify the retention rent-burden floor (was a 0.30 literal)
--
-- The paired CEILING (RET.BurdenCeilingPct = 0.50) was already a registry knob,
-- but the affordability FLOOR was a bare 0.30 literal in retention_model.py
-- (:97, :99, :162) — while the identical value is a knob elsewhere
-- (QUAL.MaxRentToIncomeRatio = 0.30). Portability: the 30%-of-income "affordable"
-- line is a US HUD convention a different jurisdiction / cost structure may not
-- share. Same knob-ification class as #191.
--
-- Reproduction-preserving: the default is 0.30, exactly the retired literal, so
-- the V2 corpus reproduces bit-identically. Ship-blocker per Gray (2026-08-07).

IF NOT EXISTS (SELECT 1 FROM reference.ParameterRegistryDefault
               WHERE Category = 'RET' AND Name = 'BurdenFloorPct')
    INSERT INTO reference.ParameterRegistryDefault (Category, Name, Value, DataType, Description) VALUES
    ('RET','BurdenFloorPct','0.30','decimal', N'burden_floor: rent-to-income at/below which affordability_gap = 0 (fully affordable). affordability_gap = clamp((fit_rent/income - this)/(BurdenCeilingPct - this), 0, 1). DECLARED, HUD-anchored (30% affordable line). KD-223: was a hardcoded 0.30 literal in retention_model.py; knob-ified for jurisdiction portability. Must be < BurdenCeilingPct.');
GO

-- Assert the row landed as expected (house style; V00063/V00071 guard + assert).
IF NOT EXISTS (SELECT 1 FROM reference.ParameterRegistryDefault
               WHERE Category = 'RET' AND Name = 'BurdenFloorPct'
                 AND Value = '0.30' AND DataType = 'decimal')
    THROW 50075, 'V00075: RET.BurdenFloorPct did not land as 0.30/decimal', 1;
GO
