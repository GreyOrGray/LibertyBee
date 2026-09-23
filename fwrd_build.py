# -*- coding: ascii -*-
"""Fair-wage + reduced-discount (fwrd) templates - Gray's scenario 3
(2026-09-04): the fair-wage basis with the tenant discount structure reduced:
sign at 5 pct below market (was 10) and tenure reductions re-tiered to
3/3/5 pct at the existing 36/72/120-month marks (was 5/5/10). TCS credit
stays 10 pct (ruled). All knobs - no engine change (rent_reduction_manager
already implements tenure accrual)."""
import psycopg

REGIONS = ["salem2026", "peabody", "danvers", "beverly", "lynn",
           "swampscott", "marblehead", "tacoma", "sf"]
PARAMS = [("PROP", "BelowMarketRentPct", "0.0500"),
          ("RR", "FirstReductionPct", "0.0300"),
          ("RR", "SecondReductionPct", "0.0300"),
          ("RR", "ThirdReductionPct", "0.0500")]

admin = psycopg.connect(host="localhost", user="libertybee", dbname="postgres",
                        autocommit=True)
for r in REGIONS:
    src, dst = f"libertybee_{r}_fw_gold", f"libertybee_{r}_fwrd_gold"
    if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dst,)).fetchone():
        print(f"{dst}: exists, skipping create")
    else:
        admin.execute(f"CREATE DATABASE {dst} TEMPLATE {src}")
    c = psycopg.connect(host="localhost", user="libertybee", dbname=dst,
                        autocommit=True)
    for cat, name, val in PARAMS:
        c.execute("UPDATE reference.ParameterRegistryDefault SET Value=%s "
                  "WHERE Category=%s AND Name=%s", (val, cat, name))
    got = c.execute("SELECT name, value FROM reference.parameterregistrydefault "
                    "WHERE (category='RR' AND name LIKE '%Pct') "
                    "OR (category='PROP' AND name='BelowMarketRentPct') "
                    "ORDER BY name").fetchall()
    c.close()
    print(f"{dst}: " + ", ".join(f"{n}={v}" for n, v in got))
admin.close()
print("FWRD BUILD COMPLETE: 9 templates")
