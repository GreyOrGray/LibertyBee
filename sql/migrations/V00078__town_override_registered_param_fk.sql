-- V00078 — town-override integrity (lb-ba 2026-08-12 finding #6, hardening):
-- every reference.TownParameterOverride row must reference a REGISTERED param.
--
-- Write-time guarantee: the FK rejects overrides for param names that do not
-- exist in reference.ParameterRegistryDefault (cockpit/SQL typos die at the
-- door). The Scope='local' guarantee — tenant-protective 'model' mechanics can
-- NEVER be town-scoped — is enforced fail-loud by the ENGINE at registry load
-- (parameter_registry._load_town_overrides raises on any non-local override),
-- so a violating row aborts every run rather than silently no-opping. The FK
-- carries existence; the engine carries scope; neither is a warning.
--
-- Additive / backward-compatible: no data change; existing rows must already
-- satisfy the FK (WITH CHECK validates them at apply time).

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_TPO_Param')
    ALTER TABLE reference.TownParameterOverride WITH CHECK
        ADD CONSTRAINT FK_TPO_Param FOREIGN KEY (Category, Name)
        REFERENCES reference.ParameterRegistryDefault (Category, Name);
GO

-- asserts (house style)
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_TPO_Param'
               AND parent_object_id = OBJECT_ID('reference.TownParameterOverride'))
    THROW 50078, 'V00078: FK_TPO_Param missing on reference.TownParameterOverride', 1;
GO
