# -*- coding: ascii -*-
"""Fair-wage (fw) basis build - Gray's ruling 2026-09-04: rerun all regions at
metro MEDIAN hire-in salaries with 75th-percentile caps ('reasonable'), ladder
extended upward until success is reachable.

Wages: BLS OEWS May 2025, each region's own metro (Boston for Salem+towns,
Seattle-Tacoma for Tacoma, SF-Oakland for SF), occupations 11-9141 (PM),
11-3012 (Admin), 49-9071 (Maintenance). Base = metro MEDIAN (A13), cap = metro
75TH PERCENTILE (A14) - a career here reaches the market's upper quartile.
Benefits = 25 pct of base (declared rule, unchanged).
Ladder: existing 300-305 + 200-209, PLUS new rungs 210-216 =
$12M/$13M/$14M/$15M/$16M/$18M/$20M cloned from rung 209.
"""
import psycopg

BOSTON = {"Property Manager": (106050, 130770),
          "Administration Manager": (120670, 157000),
          "Maintenance Crew": (59670, 73000)}
SEATAC = {"Property Manager": (117990, 137610),
          "Administration Manager": (131700, 177930),
          "Maintenance Crew": (60760, 75560)}
SFOAK = {"Property Manager": (82540, 120290),
         "Administration Manager": (134660, 192150),
         "Maintenance Crew": (63630, 80140)}

REGIONS = [
    ("libertybee_salem2026_gold", "libertybee_salem2026_fw_gold", BOSTON),
    ("libertybee_peabody_gold", "libertybee_peabody_fw_gold", BOSTON),
    ("libertybee_danvers_gold", "libertybee_danvers_fw_gold", BOSTON),
    ("libertybee_beverly_gold", "libertybee_beverly_fw_gold", BOSTON),
    ("libertybee_lynn_gold", "libertybee_lynn_fw_gold", BOSTON),
    ("libertybee_swampscott_gold", "libertybee_swampscott_fw_gold", BOSTON),
    ("libertybee_marblehead_gold", "libertybee_marblehead_fw_gold", BOSTON),
    ("libertybee_tacoma_gold", "libertybee_tacoma_fw_gold", SEATAC),
    ("libertybee_sf_gold", "libertybee_sf_fw_gold", SFOAK),
]
NEW_RUNGS = [(210, 12), (211, 13), (212, 14), (213, 15), (214, 16),
             (215, 18), (216, 20)]

admin = psycopg.connect(host="localhost", user="libertybee", dbname="postgres",
                        autocommit=True)
for src, dst, wages in REGIONS:
    exists = admin.execute(
        "SELECT 1 FROM pg_database WHERE datname=%s", (dst,)).fetchone()
    if exists:
        print(f"{dst}: already exists, skipping create")
    else:
        admin.execute(f'CREATE DATABASE {dst} TEMPLATE {src}')
        print(f"{dst}: created from {src}")
    c = psycopg.connect(host="localhost", user="libertybee", dbname=dst,
                        autocommit=True)
    for role, (base, cap) in wages.items():
        c.execute("UPDATE reference.EmployeeRole SET BaseSalary=%s, SalaryCap=%s, "
                  "BenefitsCost=%s WHERE Role=%s",
                  (base, cap, round(base * 0.25), role))
    for pid, mm in NEW_RUNGS:
        have = c.execute("SELECT 1 FROM reference.Projection WHERE ProjectionID=%s",
                         (pid,)).fetchone()
        if have:
            continue
        c.execute(
            "INSERT INTO reference.Projection (ProjectionID, Name, Description, "
            "Kind, ScenarioTag, CreatedOn) "
            "SELECT %s, %s, Description, Kind, ScenarioTag, CURRENT_TIMESTAMP "
            "FROM reference.Projection WHERE ProjectionID=209",
            (pid, f"V1_Baseline_${mm}.0M_fw_ext"))
        c.execute(
            "INSERT INTO reference.ParameterRegistryDefined "
            "SELECT %s, Category, Name, "
            "CASE WHEN Category='FIN' AND Name='StartingFunds' "
            f"THEN '{mm}000000.00' ELSE Value END, DataType, Description "
            "FROM reference.ParameterRegistryDefined WHERE ProjectionID=209",
            (pid,))
    n = c.execute("SELECT COUNT(*) FROM reference.Projection "
                  "WHERE ProjectionID BETWEEN 210 AND 216").fetchone()[0]
    r = c.execute("SELECT Role, BaseSalary, SalaryCap FROM reference.EmployeeRole "
                  "ORDER BY RoleID").fetchall()
    print(f"  rungs 210-216: {n} | wages: " +
          "; ".join(f"{x[0].split()[0]} {int(x[1]):,}/{int(x[2]):,}" for x in r))
    c.close()
admin.close()
print("FW BUILD COMPLETE: 9 templates")
