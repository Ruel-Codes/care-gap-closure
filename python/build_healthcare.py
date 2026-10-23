"""Load the Synthea CSVs into MySQL and check data quality with Pandas.

Staging tables keep the source text. sql/healthcare_analytics.sql turns that
staging data into the cleaned tables, the quality log, and the analysis views.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pymysql

ROOT = Path("/Users/ruel/Desktop/data analyst")
CSV_DIR = ROOT / "csv"
SQL_FILE = ROOT / "sql" / "healthcare_analytics.sql"
OUTPUT = ROOT / "output"

STAGING_FILES = {
    "stg_patients": "patients.csv",
    "stg_encounters": "encounters.csv",
    "stg_conditions": "conditions.csv",
    "stg_procedures": "procedures.csv",
    "stg_medications": "medications.csv",
    "stg_payers": "payers.csv",
    "stg_providers": "providers.csv",
}


def read_csv(name: str) -> pd.DataFrame:
    frame = pd.read_csv(CSV_DIR / name, dtype=str, keep_default_na=False)
    frame.columns = [column.strip().lower() for column in frame.columns]
    return frame


def parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.replace("", pd.NA), errors="coerce", utc=True).dt.tz_localize(None)


def pandas_quality_report() -> pd.DataFrame:
    patients = read_csv("patients.csv")
    encounters = read_csv("encounters.csv")
    conditions = read_csv("conditions.csv")
    procedures = read_csv("procedures.csv")
    medications = read_csv("medications.csv")

    birth = parse_ts(patients["birthdate"])
    death = parse_ts(patients["deathdate"])
    birth_by_id = dict(zip(patients["id"], birth))
    death_by_id = dict(zip(patients["id"], death))
    start = parse_ts(encounters["start"])
    stop = parse_ts(encounters["stop"])
    procedure_dt = parse_ts(procedures["date"])
    encounter_window = encounters[["id"]].copy()
    encounter_window["start"] = start
    encounter_window["stop"] = stop
    procedures = procedures.join(encounter_window.set_index("id"), on="encounter", rsuffix="_enc")

    outside = (procedure_dt.dt.normalize() < procedures["start"].dt.normalize()) | (
        procedure_dt.dt.normalize() > procedures["stop"].dt.normalize()
    )
    shared_ssn = patients["ssn"].ne("") & patients["ssn"].duplicated(keep=False)
    patient_ids = set(patients["id"])
    encounter_ids = set(encounters["id"])

    after_death = 0
    before_birth = 0
    for patient_id, started in zip(encounters["patient"], start):
        died = death_by_id.get(patient_id)
        born = birth_by_id.get(patient_id)
        if pd.notna(died) and pd.notna(started) and started.date() > died.date():
            after_death += 1
        if pd.notna(born) and pd.notna(started) and started.date() < born.date():
            before_birth += 1

    same_start = medications.duplicated(
        ["patient", "encounter", "code", "start"], keep=False
    )
    rows = [
        ("duplicate_patient_id", int(patients["id"].duplicated().sum())),
        ("duplicate_encounter_id", int(encounters["id"].duplicated().sum())),
        ("shared_ssn_rows", int(shared_ssn.sum())),
        ("condition_blank_stop", int(conditions["stop"].eq("").sum())),
        ("medication_blank_stop", int(medications["stop"].eq("").sum())),
        ("procedure_date_outside_encounter", int(outside.fillna(False).sum())),
        ("procedure_unparsed_date", int(procedure_dt.isna().sum())),
        ("encounter_stop_before_start", int((stop < start).fillna(False).sum())),
        ("encounter_after_death", after_death),
        ("encounter_before_birth", before_birth),
        ("missing_zip", int(patients["zip"].eq("").sum())),
        ("encounter_blank_reason", int(encounters["reasoncode"].eq("").sum())),
        ("orphan_condition_encounter", int((~conditions["encounter"].isin(encounter_ids)).sum())),
        ("orphan_encounter_patient", int((~encounters["patient"].isin(patient_ids)).sum())),
        ("medication_same_start_kept", int(same_start.sum())),
    ]
    report = pd.DataFrame(rows, columns=["check_name", "row_count"])
    OUTPUT.mkdir(exist_ok=True)
    report.to_csv(OUTPUT / "pandas_dq_before.csv", index=False)
    return report


def load_staging() -> None:
    connection = pymysql.connect(
        host="127.0.0.1",
        user="root",
        port=3306,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP DATABASE IF EXISTS healthcare_analytics")
            cursor.execute(
                "CREATE DATABASE healthcare_analytics "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            cursor.execute("USE healthcare_analytics")
            for table, filename in STAGING_FILES.items():
                frame = read_csv(filename)
                columns = list(frame.columns)
                column_sql = ", ".join(f"`{column}` VARCHAR(512) NOT NULL" for column in columns)
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
                cursor.execute(f"CREATE TABLE {table} ({column_sql}) ENGINE=InnoDB")
                insert_sql = (
                    f"INSERT INTO {table} ({', '.join(f'`{column}`' for column in columns)}) "
                    f"VALUES ({', '.join(['%s'] * len(columns))})"
                )
                values = list(frame.itertuples(index=False, name=None))
                for offset in range(0, len(values), 2000):
                    cursor.executemany(insert_sql, values[offset : offset + 2000])
                print(f"loaded {table}: {len(frame):,} rows")
        connection.commit()
    finally:
        connection.close()


def run_sql() -> None:
    script = SQL_FILE.read_text()
    completed = subprocess.run(
        [
            "mysql",
            "-u",
            "root",
            "-h",
            "127.0.0.1",
            "--batch",
            "--raw",
            "healthcare_analytics",
        ],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    (OUTPUT / "sql_results.txt").write_text(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)
    if completed.stderr.strip():
        print(completed.stderr)


def fetch_metrics() -> dict:
    connection = pymysql.connect(
        host="127.0.0.1",
        user="root",
        port=3306,
        database="healthcare_analytics",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    queries = {
        "quality_actions": """
            SELECT action, COUNT(*) AS issues
            FROM data_quality_log
            GROUP BY action
            ORDER BY issues DESC
        """,
        "bands": """
            SELECT cost_band,
                   COUNT(*) AS encounters,
                   ROUND(SUM(total_cost), 2) AS total_cost
            FROM v_encounter_economics
            GROUP BY cost_band
            ORDER BY FIELD(cost_band, 'Low', 'Mid', 'High')
        """,
        "classes": """
            SELECT encounter_class,
                   COUNT(*) AS encounters,
                   ROUND(SUM(total_cost), 2) AS total_cost,
                   SUM(cost_band = 'High') AS high_encounters
            FROM v_encounter_economics
            GROUP BY encounter_class
            ORDER BY total_cost DESC
        """,
        "top_patients": """
            SELECT r.cost_rank,
                   CONCAT(p.first_name, ' ', p.last_name) AS patient_name,
                   p.gender,
                   TIMESTAMPDIFF(YEAR, p.birthdate, DATE '2020-04-28') AS age_in_2020,
                   r.encounters,
                   ROUND(r.patient_cost, 2) AS patient_cost
            FROM v_patient_cost_rank r
            JOIN patients p ON p.id = r.patient_id
            WHERE r.cost_rank <= 8
            ORDER BY r.cost_rank, patient_name
        """,
        "top_conditions": """
            SELECT encounter_class, condition_description, condition_count,
                   ROUND(attributed_cost, 2) AS attributed_cost, cost_rank
            FROM v_condition_rank
            WHERE cost_rank <= 3
            ORDER BY encounter_class, cost_rank, condition_description
        """,
        "care_gaps": """
            SELECT COUNT(*) AS gap_events,
                   COUNT(DISTINCT patient_id) AS gap_patients
            FROM v_care_gaps
        """,
        "care_gap_conditions": """
            SELECT condition_description,
                   COUNT(*) AS gap_events,
                   COUNT(DISTINCT patient_id) AS patients
            FROM v_care_gaps
            GROUP BY condition_description
            ORDER BY gap_events DESC
            LIMIT 8
        """,
        "repeat_visits": """
            SELECT COUNT(*) AS patient_years,
                   COUNT(DISTINCT patient_id) AS patients
            FROM v_repeat_visit_years
        """,
        "monthly": """
            SELECT month_start, encounters, mom_change, mom_pct
            FROM v_monthly_volume
            WHERE month_start >= '2017-01-01'
            ORDER BY month_start
        """,
        "payers": """
            SELECT payers.name AS payer_name,
                   COUNT(*) AS encounters,
                   ROUND(SUM(e.total_cost), 2) AS total_cost
            FROM v_encounter_economics e
            JOIN payers ON payers.id = e.payer_id
            GROUP BY payers.name
            ORDER BY total_cost DESC
        """,
        "totals": """
            SELECT
              (SELECT COUNT(*) FROM patients) AS patients,
              (SELECT COUNT(*) FROM encounters) AS encounters,
              (SELECT COUNT(*) FROM conditions) AS conditions,
              (SELECT COUNT(*) FROM procedures) AS procedures,
              (SELECT COUNT(*) FROM medications) AS medications,
              (SELECT ROUND(SUM(total_cost), 2) FROM v_encounter_economics) AS total_cost,
              (SELECT ROUND(SUM(procedure_cost), 2) FROM v_encounter_economics) AS procedure_cost,
              (SELECT ROUND(SUM(medication_cost), 2) FROM v_encounter_economics) AS medication_cost,
              (SELECT ROUND(SUM(base_encounter_cost), 2) FROM v_encounter_economics) AS base_cost,
              (SELECT MIN(DATE(start_ts)) FROM encounters) AS first_encounter,
              (SELECT MAX(DATE(start_ts)) FROM encounters) AS last_encounter
        """,
        "post_checks": """
            SELECT 'shared_ssn' AS check_name, COUNT(*) AS row_count FROM (
                SELECT ssn FROM patients WHERE ssn IS NOT NULL GROUP BY ssn HAVING COUNT(*) > 1
            ) s
            UNION ALL
            SELECT 'procedure_outside_window', COUNT(*)
            FROM procedures pr
            JOIN encounters e ON e.id = pr.encounter_id
            WHERE DATE(pr.procedure_date) < DATE(e.start_ts)
               OR DATE(pr.procedure_date) > DATE(e.stop_ts)
            UNION ALL
            SELECT 'stop_before_start', COUNT(*)
            FROM encounters WHERE stop_ts < start_ts
            UNION ALL
            SELECT 'resolved_issues', COUNT(*)
            FROM data_quality_log
            WHERE action IN ('cleared_ssn', 'aligned_procedure_date', 'blank_to_null')
        """,
    }
    metrics: dict = {}
    try:
        with connection.cursor() as cursor:
            for name, query in queries.items():
                cursor.execute(query)
                rows = cursor.fetchall()
                metrics[name] = [
                    {key: _json_value(value) for key, value in row.items()} for row in rows
                ]
    finally:
        connection.close()
    (OUTPUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def _json_value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (int, float, str)):
        return value
    return float(value) if not isinstance(value, bool) else value


def main() -> None:
    report = pandas_quality_report()
    print(report.to_string(index=False))
    load_staging()
    run_sql()
    metrics = fetch_metrics()
    print(json.dumps(metrics["totals"], indent=2))
    print(json.dumps(metrics["quality_actions"], indent=2))
    print(json.dumps(metrics["post_checks"], indent=2))
    print(json.dumps(metrics["bands"], indent=2))
    print(json.dumps(metrics["care_gaps"], indent=2))
    print(json.dumps(metrics["repeat_visits"], indent=2))


if __name__ == "__main__":
    main()
