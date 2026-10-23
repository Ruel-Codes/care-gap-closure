"""One-command care-gap pipeline.

Runs the trust checks, builds the 90-day gap tables, exports the outreach
worklist, and writes the Tableau workbook the dashboard opens.
"""

from __future__ import annotations

import csv
import subprocess
import zipfile
from pathlib import Path

import pandas as pd
import pymysql

ROOT = Path("/Users/ruel/Desktop/data analyst")
SQL_FILE = ROOT / "sql" / "care_gap_closure.sql"
OUTPUT = ROOT / "output"
DATA = ROOT / "tableau" / "data"
WORKBOOK = ROOT / "tableau" / "Care Gap Closure.twb"
AS_OF = "2020-04-28"
FOLLOW_UP_CUTOFF = "2020-01-28"


def connect():
    return pymysql.connect(
        host="127.0.0.1",
        user="root",
        port=3306,
        database="healthcare_analytics",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def load_organizations() -> None:
    frame = pd.read_csv(ROOT / "csv" / "organizations.csv", dtype=str, keep_default_na=False)
    frame.columns = [column.strip().lower() for column in frame.columns]
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS organizations")
            cursor.execute(
                """
                CREATE TABLE organizations (
                  id CHAR(36) NOT NULL,
                  name VARCHAR(180) NOT NULL,
                  city VARCHAR(80) NULL,
                  state VARCHAR(40) NULL,
                  PRIMARY KEY (id)
                ) ENGINE=InnoDB
                """
            )
            rows = [
                (row.id, row.name, row.city or None, row.state or None)
                for row in frame.itertuples(index=False)
            ]
            cursor.executemany(
                "INSERT INTO organizations (id, name, city, state) VALUES (%s, %s, %s, %s)",
                rows,
            )
        print(f"loaded organizations: {len(frame):,}")
    finally:
        connection.close()


def run_sql() -> None:
    completed = subprocess.run(
        ["mysql", "-u", "root", "-h", "127.0.0.1", "--batch", "--raw", "healthcare_analytics"],
        input=SQL_FILE.read_text(),
        text=True,
        capture_output=True,
        check=False,
    )
    (OUTPUT / "care_gap_sql.txt").write_text(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)
    print(completed.stdout)


def fetch_checks(cursor) -> list[dict]:
    checks = [
        (
            "duplicate_patient_ids",
            "gate",
            "SELECT COUNT(*) AS n FROM (SELECT id FROM patients GROUP BY id HAVING COUNT(*) > 1) d",
        ),
        (
            "duplicate_encounter_ids",
            "gate",
            "SELECT COUNT(*) AS n FROM (SELECT id FROM encounters GROUP BY id HAVING COUNT(*) > 1) d",
        ),
        (
            "conditions_missing_patient",
            "gate",
            """
            SELECT COUNT(*) AS n
            FROM conditions c
            LEFT JOIN patients p ON p.id = c.patient_id
            WHERE p.id IS NULL
            """,
        ),
        (
            "conditions_missing_encounter",
            "gate",
            """
            SELECT COUNT(*) AS n
            FROM conditions c
            LEFT JOIN encounters e ON e.id = c.encounter_id
            WHERE e.id IS NULL
            """,
        ),
        (
            "procedures_missing_patient",
            "gate",
            """
            SELECT COUNT(*) AS n
            FROM procedures p
            LEFT JOIN patients pt ON pt.id = p.patient_id
            WHERE pt.id IS NULL
            """,
        ),
        (
            "medications_missing_patient",
            "gate",
            """
            SELECT COUNT(*) AS n
            FROM medications m
            LEFT JOIN patients pt ON pt.id = m.patient_id
            WHERE pt.id IS NULL
            """,
        ),
        (
            "missing_birthdate",
            "gate",
            "SELECT COUNT(*) AS n FROM patients WHERE birthdate IS NULL",
        ),
        (
            "missing_condition_start",
            "gate",
            "SELECT COUNT(*) AS n FROM conditions WHERE start_date IS NULL",
        ),
        (
            "condition_stop_before_start",
            "gate",
            """
            SELECT COUNT(*) AS n
            FROM conditions
            WHERE stop_date IS NOT NULL AND stop_date < start_date
            """,
        ),
        (
            "procedure_before_diagnosis",
            "flag",
            """
            SELECT COUNT(*) AS n
            FROM conditions c
            JOIN procedures p ON p.encounter_id = c.encounter_id
            WHERE DATE(p.procedure_date) < c.start_date
            """,
        ),
        (
            "medication_before_diagnosis",
            "flag",
            """
            SELECT COUNT(*) AS n
            FROM conditions c
            JOIN medications m ON m.encounter_id = c.encounter_id
            WHERE DATE(m.start_ts) < c.start_date
            """,
        ),
    ]
    results = []
    for name, kind, query in checks:
        cursor.execute(query)
        count = int(cursor.fetchone()["n"])
        status = "PASS" if count == 0 else "FAIL"
        results.append({"check": name, "kind": kind, "issues": count, "status": status})
    return results


def write_checks(results: list[dict]) -> None:
    lines = ["Care-gap data trust check", f"Extract as of {AS_OF}", ""]
    for row in results:
        lines.append(f"{row['status']:4}  {row['kind']:4}  {row['check']:32}  issues={row['issues']}")
    gate_failures = [row for row in results if row["kind"] == "gate" and row["status"] == "FAIL"]
    flagged = sum(row["issues"] for row in results if row["kind"] == "flag")
    lines.append("")
    lines.append(f"checks={len(results)}  flagged_issues={flagged}  gate_failures={len(gate_failures)}")
    text = "\n".join(lines) + "\n"
    (OUTPUT / "care_gap_validation.txt").write_text(text)
    print(text)
    if gate_failures:
        raise SystemExit("A gate check failed. Reporting stopped.")


def query_rows(cursor, sql: str) -> list[dict]:
    cursor.execute(sql)
    return list(cursor.fetchall())


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise RuntimeError(f"No rows for {path.name}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_frames(cursor) -> dict:
    summary = query_rows(
        cursor,
        """
        SELECT
          (SELECT COUNT(*) FROM patients) AS patients,
          (SELECT COUNT(*) FROM encounters) AS encounters,
          (SELECT COUNT(*) FROM condition_gap) AS eligible_conditions,
          (SELECT SUM(is_gap) FROM condition_gap) AS gap_conditions,
          (SELECT COUNT(DISTINCT patient_id) FROM condition_gap) AS eligible_patients,
          (SELECT COUNT(*) FROM patient_gap) AS gap_patients,
          (SELECT COUNT(*) FROM conditions WHERE description LIKE 'History of %') AS history_excluded,
          (SELECT COUNT(*) FROM conditions WHERE start_date > '2020-01-28') AS too_new_excluded,
          (SELECT COUNT(*) FROM outreach_worklist WHERE priority_band = 'High') AS high_band,
          (SELECT COUNT(*) FROM outreach_worklist WHERE priority_band = 'Medium') AS medium_band,
          (SELECT COUNT(*) FROM outreach_worklist WHERE priority_band = 'Low') AS low_band,
          (SELECT COUNT(*) FROM org_gap_rate) AS organizations_compared,
          (SELECT COUNT(DISTINCT provider_id) FROM outreach_worklist) AS providers_on_worklist,
          (SELECT COUNT(*) FROM patient_gap g JOIN patients p ON p.id = g.patient_id WHERE p.deathdate IS NOT NULL) AS deceased_left_off
        """,
    )[0]
    for key, value in list(summary.items()):
        summary[key] = int(value)
    summary["high_priority"] = summary.pop("high_band")
    summary["medium_priority"] = summary.pop("medium_band")
    summary["low_priority"] = summary.pop("low_band")
    summary["condition_gap_rate"] = round(100 * summary["gap_conditions"] / summary["eligible_conditions"], 1)
    summary["patient_gap_rate"] = round(100 * summary["gap_patients"] / summary["eligible_patients"], 1)

    conditions = query_rows(
        cursor,
        """
        SELECT condition_name AS `Condition`, clinical_class AS `Class`,
               gap_conditions AS `Gaps`, gap_patients AS `Patients`, gap_rate AS `GapRate`
        FROM gap_by_condition
        ORDER BY gap_conditions DESC
        LIMIT 10
        """,
    )
    opportunity = query_rows(
        cursor,
        """
        SELECT organization_name AS `Organization`, city AS `City`,
               eligible_conditions AS `Conditions`, gap_conditions AS `Gaps`, gap_rate AS `GapRate`
        FROM org_gap_rate
        ORDER BY gap_rate DESC, gap_conditions DESC
        LIMIT 8
        """,
    )
    best = query_rows(
        cursor,
        """
        SELECT organization_name AS `Organization`, city AS `City`,
               eligible_conditions AS `Conditions`, gap_conditions AS `Gaps`, gap_rate AS `GapRate`
        FROM org_gap_rate
        ORDER BY gap_rate ASC, eligible_conditions DESC
        LIMIT 8
        """,
    )
    monthly = query_rows(
        cursor,
        """
        SELECT DATE_FORMAT(month_start, '%Y-%m') AS `Month`,
               eligible_conditions AS `Conditions`,
               gap_conditions AS `Gaps`,
               gap_rate AS `GapRate`,
               prior_gap_rate AS `PriorGapRate`,
               rate_change_pts AS `RateChange`
        FROM monthly_gap_trend
        WHERE month_start >= '2017-01-01'
        ORDER BY month_start
        """,
    )
    worklist = query_rows(
        cursor,
        """
        SELECT provider_rank AS `ProviderRank`, priority_band AS `Priority`, priority_score AS `Score`,
               patient_name AS `Patient`, age_years AS `Age`, gender AS `Sex`,
               provider_name AS `Provider`, speciality AS `Speciality`,
               organization_name AS `Organization`, city AS `City`,
               gap_count AS `Gaps`, chronic_gaps AS `ChronicGaps`, urgent_gaps AS `UrgentGaps`,
               lead_condition AS `LeadCondition`, last_encounter_date AS `LastVisit`,
               days_since_visit AS `DaysSinceVisit`
        FROM outreach_worklist
        ORDER BY priority_score DESC, chronic_gaps DESC, age_years DESC, patient_name
        """,
    )
    chart_list = worklist[:12]
    kpis = [
        {"Metric": "Gap conditions", "Value": summary["gap_conditions"]},
        {"Metric": "Patients with a gap", "Value": summary["gap_patients"]},
        {"Metric": "Call first (High)", "Value": summary["high_priority"]},
        {"Metric": "Baseline gap rate", "Value": summary["condition_gap_rate"]},
    ]
    write_csv(DATA / "gaps_by_condition.csv", conditions)
    write_csv(DATA / "org_opportunity.csv", opportunity)
    write_csv(DATA / "org_best.csv", best)
    write_csv(DATA / "monthly_gap_rate.csv", monthly)
    write_csv(DATA / "worklist_top.csv", chart_list)
    write_csv(DATA / "kpis.csv", kpis)
    write_csv(OUTPUT / "outreach_worklist.csv", worklist)
    return {
        "summary": summary,
        "conditions": conditions,
        "opportunity": opportunity,
        "best": best,
        "monthly": monthly,
    }


def xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
    )


def metadata_records(columns: list[tuple[str, str]], parent: str) -> str:
    remote = {"string": "129", "integer": "20", "real": "5"}
    local = {"string": "string", "integer": "integer", "real": "real"}
    blocks = []
    for ordinal, (name, kind) in enumerate(columns):
        aggregation = "Count" if kind == "string" else "Sum"
        blocks.append(
            f"""          <metadata-record class='column'>
            <remote-name>{xml_escape(name)}</remote-name>
            <remote-type>{remote[kind]}</remote-type>
            <local-name>[{xml_escape(name)}]</local-name>
            <parent-name>[{parent}]</parent-name>
            <remote-alias>{xml_escape(name)}</remote-alias>
            <ordinal>{ordinal}</ordinal>
            <local-type>{local[kind]}</local-type>
            <aggregation>{aggregation}</aggregation>
            <contains-null>true</contains-null>
          </metadata-record>"""
        )
    return "\n".join(blocks)


def datasource_xml(caption: str, filename: str, columns: list[tuple[str, str]]) -> str:
    stem, ext = filename.rsplit(".", 1)
    table = f"{stem}#{ext}"
    parent = filename
    leaf = f"textscan.{caption}"
    relation_columns = []
    datasource_columns = []
    for ordinal, (name, kind) in enumerate(columns):
        datatype = {"string": "string", "integer": "integer", "real": "real"}[kind]
        relation_columns.append(
            f"              <column datatype='{datatype}' name='{xml_escape(name)}' ordinal='{ordinal}' />"
        )
        role = "dimension" if kind == "string" else "measure"
        kind_attr = "nominal" if kind == "string" else "quantitative"
        aggregation = "" if kind == "string" else " aggregation='Sum'"
        datasource_columns.append(
            f"      <column{aggregation} datatype='{datatype}' name='[{xml_escape(name)}]' role='{role}' type='{kind_attr}' />"
        )
    column_block = "\n".join(relation_columns)
    relation = f"""        <_.fcp.ObjectModelEncapsulateLegacy.false...relation connection='{leaf}' name='{xml_escape(filename)}' table='[{xml_escape(table)}]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
{column_block}
          </columns>
        </_.fcp.ObjectModelEncapsulateLegacy.false...relation>
        <_.fcp.ObjectModelEncapsulateLegacy.true...relation connection='{leaf}' name='{xml_escape(filename)}' table='[{xml_escape(table)}]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
{column_block}
          </columns>
        </_.fcp.ObjectModelEncapsulateLegacy.true...relation>"""
    return f"""    <datasource caption='{xml_escape(caption)}' inline='true' name='{xml_escape(caption)}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='{xml_escape(caption)}' name='{leaf}'>
            <connection class='textscan' directory='Data' filename='{xml_escape(filename)}' password='' server='' />
          </named-connection>
        </named-connections>
{relation}
        <metadata-records>
{metadata_records(columns, parent)}
          <metadata-record class='capability'>
            <remote-name />
            <remote-type>0</remote-type>
            <parent-name>[{xml_escape(parent)}]</parent-name>
            <remote-alias />
            <aggregation>Count</aggregation>
            <contains-null>true</contains-null>
            <attributes>
              <attribute datatype='string' name='character-set'>&quot;UTF-8&quot;</attribute>
              <attribute datatype='string' name='collation'>&quot;en_US&quot;</attribute>
              <attribute datatype='string' name='field-delimiter'>&quot;,&quot;</attribute>
              <attribute datatype='string' name='header-row'>&quot;true&quot;</attribute>
              <attribute datatype='string' name='locale'>&quot;en_US&quot;</attribute>
              <attribute datatype='string' name='single-char'>&quot;&quot;</attribute>
            </attributes>
          </metadata-record>
        </metadata-records>
      </connection>
      <aliases enabled='yes' />
{chr(10).join(datasource_columns)}
      <layout dim-ordering='alphabetic' dim-percentage='0.5' measure-ordering='alphabetic' measure-percentage='0.4' show-structure='true' />
      <semantic-values>
        <semantic-value key='[Country].[Name]' value='&quot;United States&quot;' />
      </semantic-values>
    </datasource>"""


def field_refs(ds: str, fields: list[tuple[str, str, str]]) -> str:
    """fields: (name, datatype, role) role is dimension or measure."""
    blocks = []
    for name, datatype, role in fields:
        kind = "nominal" if datatype == "string" else "quantitative"
        if role == "dimension" and datatype != "string":
            kind = "ordinal"
        aggregation = ""
        if role == "measure":
            aggregation = " aggregation='Sum'"
        blocks.append(
            f"            <column{aggregation} datatype='{datatype}' name='[{xml_escape(name)}]' role='{role}' type='{kind}' />"
        )
        if role == "measure":
            blocks.append(
                f"            <column-instance column='[{xml_escape(name)}]' derivation='Sum' name='[sum:{xml_escape(name)}:qk]' pivot='key' type='quantitative' />"
            )
        else:
            suffix = "nk" if datatype == "string" else "ok"
            nominal = "nominal" if datatype == "string" else "ordinal"
            blocks.append(
                f"            <column-instance column='[{xml_escape(name)}]' derivation='None' name='[none:{xml_escape(name)}:{suffix}]' pivot='key' type='{nominal}' />"
            )
    return "\n".join(blocks)


def bar_worksheet(name: str, title: str, ds: str, row: str, col: str, fields: list[tuple[str, str, str]]) -> str:
    return f"""    <worksheet name='{xml_escape(name)}'>
      <layout-options>
        <title>
          <formatted-text>
            <run fontname='Tableau Medium' fontsize='12'>{xml_escape(title)}</run>
          </formatted-text>
        </title>
      </layout-options>
      <table>
        <view>
          <datasources>
            <datasource caption='{xml_escape(ds)}' name='{xml_escape(ds)}' />
          </datasources>
          <datasource-dependencies datasource='{xml_escape(ds)}'>
{field_refs(ds, fields)}
          </datasource-dependencies>
          <sort class='computed' column='[{xml_escape(ds)}].[none:{xml_escape(row)}:nk]' direction='DESC' using='[{xml_escape(ds)}].[sum:{xml_escape(col)}:qk]' />
          <aggregation value='true' />
        </view>
        <style>
          <style-rule element='axis'>
            <format attr='title' field='[{xml_escape(ds)}].[sum:{xml_escape(col)}:qk]' scope='cols' value='{xml_escape(col)}' />
          </style-rule>
          <style-rule element='worksheet'>
            <format attr='display-field-labels' scope='rows' value='false' />
          </style-rule>
          <style-rule element='mark'>
            <format attr='mark-color' value='#1F4E79' />
          </style-rule>
        </style>
        <panes>
          <pane>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class='Bar' />
            <encodings>
              <text column='[{xml_escape(ds)}].[sum:{xml_escape(col)}:qk]' />
            </encodings>
            <style>
              <style-rule element='mark'>
                <format attr='mark-labels-show' value='true' />
              </style-rule>
            </style>
          </pane>
        </panes>
        <rows>[{xml_escape(ds)}].[none:{xml_escape(row)}:nk]</rows>
        <cols>[{xml_escape(ds)}].[sum:{xml_escape(col)}:qk]</cols>
      </table>
    </worksheet>"""


def line_worksheet() -> str:
    ds = "Monthly"
    return f"""    <worksheet name='Monthly trend'>
      <layout-options>
        <title>
          <formatted-text>
            <run fontname='Tableau Medium' fontsize='12'>Monthly gap rate, with the prior month from LAG</run>
          </formatted-text>
        </title>
      </layout-options>
      <table>
        <view>
          <datasources>
            <datasource caption='{ds}' name='{ds}' />
          </datasources>
          <datasource-dependencies datasource='{ds}'>
{field_refs(ds, [("Month", "string", "dimension"), ("GapRate", "real", "measure")])}
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style>
          <style-rule element='axis'>
            <format attr='title' field='[{ds}].[sum:GapRate:qk]' scope='rows' value='Gap rate (%)' />
            <format attr='title' field='[{ds}].[none:Month:nk]' scope='cols' value='Month' />
          </style-rule>
          <style-rule element='mark'>
            <format attr='mark-color' value='#1F4E79' />
          </style-rule>
        </style>
        <panes>
          <pane>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class='Line' />
          </pane>
        </panes>
        <rows>[{ds}].[sum:GapRate:qk]</rows>
        <cols>[{ds}].[none:Month:nk]</cols>
      </table>
    </worksheet>"""


def kpi_worksheet() -> str:
    ds = "KPIs"
    return f"""    <worksheet name='Headline'>
      <layout-options>
        <title>
          <formatted-text>
            <run fontname='Tableau Medium' fontsize='12'>Baseline, before any outreach</run>
          </formatted-text>
        </title>
      </layout-options>
      <table>
        <view>
          <datasources>
            <datasource caption='{ds}' name='{ds}' />
          </datasources>
          <datasource-dependencies datasource='{ds}'>
{field_refs(ds, [("Metric", "string", "dimension"), ("Value", "real", "measure")])}
          </datasource-dependencies>
          <aggregation value='true' />
        </view>
        <style>
          <style-rule element='worksheet'>
            <format attr='display-field-labels' scope='cols' value='false' />
          </style-rule>
        </style>
        <panes>
          <pane>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class='Text' />
            <encodings>
              <text column='[{ds}].[sum:Value:qk]' />
            </encodings>
            <style>
              <style-rule element='mark'>
                <format attr='mark-labels-show' value='true' />
                <format attr='mark-labels-cull' value='false' />
              </style-rule>
            </style>
          </pane>
        </panes>
        <rows>[{ds}].[none:Metric:nk]</rows>
        <cols></cols>
      </table>
    </worksheet>"""


def worklist_worksheet() -> str:
    ds = "Worklist"
    fields = [
        ("Patient", "string", "dimension"),
        ("Score", "integer", "measure"),
        ("Priority", "string", "dimension"),
        ("LeadCondition", "string", "dimension"),
    ]
    return f"""    <worksheet name='Call first'>
      <layout-options>
        <title>
          <formatted-text>
            <run fontname='Tableau Medium' fontsize='12'>Highest-priority patients on the outreach worklist</run>
          </formatted-text>
        </title>
      </layout-options>
      <table>
        <view>
          <datasources>
            <datasource caption='{ds}' name='{ds}' />
          </datasources>
          <datasource-dependencies datasource='{ds}'>
{field_refs(ds, fields)}
          </datasource-dependencies>
          <sort class='computed' column='[{ds}].[none:Patient:nk]' direction='DESC' using='[{ds}].[sum:Score:qk]' />
          <aggregation value='true' />
        </view>
        <style>
          <style-rule element='axis'>
            <format attr='title' field='[{ds}].[sum:Score:qk]' scope='cols' value='Priority score' />
          </style-rule>
          <style-rule element='worksheet'>
            <format attr='display-field-labels' scope='rows' value='false' />
          </style-rule>
        </style>
        <panes>
          <pane>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class='Bar' />
            <encodings>
              <color column='[{ds}].[none:Priority:nk]' />
              <text column='[{ds}].[sum:Score:qk]' />
              <tooltip column='[{ds}].[none:LeadCondition:nk]' />
            </encodings>
            <style>
              <style-rule element='mark'>
                <format attr='mark-labels-show' value='true' />
              </style-rule>
            </style>
          </pane>
        </panes>
        <rows>[{ds}].[none:Patient:nk]</rows>
        <cols>[{ds}].[sum:Score:qk]</cols>
      </table>
    </worksheet>"""


def window_xml(class_name: str, name: str) -> str:
    return f"""    <window class='{class_name}' name='{xml_escape(name)}'>
      <cards>
        <edge name='left'>
          <strip size='160'>
            <card type='pages' />
            <card type='filters' />
            <card type='marks' />
          </strip>
        </edge>
        <edge name='top'>
          <strip size='31'>
            <card type='columns' />
          </strip>
          <strip size='31'>
            <card type='rows' />
          </strip>
          <strip size='31'>
            <card type='title' />
          </strip>
        </edge>
      </cards>
    </window>"""


def write_workbook(summary: dict) -> None:
    rate = summary["condition_gap_rate"]
    patient_rate = summary["patient_gap_rate"]
    subtitle = (
        f"A care gap is a diagnosis with no medication and no procedure in the next 90 days. "
        f"{summary['gap_conditions']:,} gaps. {summary['gap_patients']:,} patients "
        f"({patient_rate}% of patients with an eligible condition). "
        f"Baseline gap rate {rate}%. No outreach has run. This is the yardstick, not a result."
    )
    datasources = "\n".join(
        [
            datasource_xml(
                "Conditions",
                "gaps_by_condition.csv",
                [("Condition", "string"), ("Class", "string"), ("Gaps", "integer"), ("Patients", "integer"), ("GapRate", "real")],
            ),
            datasource_xml(
                "Opportunity",
                "org_opportunity.csv",
                [("Organization", "string"), ("City", "string"), ("Conditions", "integer"), ("Gaps", "integer"), ("GapRate", "real")],
            ),
            datasource_xml(
                "BestFit",
                "org_best.csv",
                [("Organization", "string"), ("City", "string"), ("Conditions", "integer"), ("Gaps", "integer"), ("GapRate", "real")],
            ),
            datasource_xml(
                "Monthly",
                "monthly_gap_rate.csv",
                [("Month", "string"), ("Conditions", "integer"), ("Gaps", "integer"), ("GapRate", "real"), ("PriorGapRate", "real"), ("RateChange", "real")],
            ),
            datasource_xml(
                "Worklist",
                "worklist_top.csv",
                [
                    ("ProviderRank", "integer"),
                    ("Priority", "string"),
                    ("Score", "integer"),
                    ("Patient", "string"),
                    ("Age", "integer"),
                    ("Sex", "string"),
                    ("Provider", "string"),
                    ("Speciality", "string"),
                    ("Organization", "string"),
                    ("City", "string"),
                    ("Gaps", "integer"),
                    ("ChronicGaps", "integer"),
                    ("UrgentGaps", "integer"),
                    ("LeadCondition", "string"),
                    ("LastVisit", "string"),
                    ("DaysSinceVisit", "integer"),
                ],
            ),
            datasource_xml("KPIs", "kpis.csv", [("Metric", "string"), ("Value", "real")]),
        ]
    )
    sheets = "\n".join(
        [
            kpi_worksheet(),
            bar_worksheet(
                "Gaps by condition",
                "Where the gaps concentrate",
                "Conditions",
                "Condition",
                "Gaps",
                [("Condition", "string", "dimension"), ("Gaps", "integer", "measure"), ("Class", "string", "dimension")],
            ),
            bar_worksheet(
                "Where to expand",
                "Highest gap rate among organizations with at least 40 conditions",
                "Opportunity",
                "Organization",
                "GapRate",
                [("Organization", "string", "dimension"), ("GapRate", "real", "measure")],
            ),
            bar_worksheet(
                "Where it is already lower",
                "Lowest gap rate among organizations with at least 40 conditions",
                "BestFit",
                "Organization",
                "GapRate",
                [("Organization", "string", "dimension"), ("GapRate", "real", "measure")],
            ),
            line_worksheet(),
            worklist_worksheet(),
        ]
    )
    sheet_names = [
        "Headline",
        "Gaps by condition",
        "Where to expand",
        "Where it is already lower",
        "Monthly trend",
        "Call first",
    ]
    windows = "\n".join(window_xml("worksheet", name) for name in sheet_names)
    windows += "\n" + f"""    <window class='dashboard' maximized='true' name='Care gap closure'>
      <viewpoints>
        {''.join(f"<viewpoint name='{xml_escape(name)}' />" for name in sheet_names)}
      </viewpoints>
      <active id='-1' />
    </window>"""
    workbook = f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook locale='en_US' source-build='2026.3.0' source-platform='mac' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences>
    <preference name='ui.encoding.shelf.height' value='24' />
    <preference name='ui.shelf.height' value='26' />
  </preferences>
  <style-theme name='smooth' />
  <datasources>
{datasources}
  </datasources>
  <worksheets>
{sheets}
  </worksheets>
  <dashboards>
    <dashboard name='Care gap closure'>
      <layout-options>
        <title>
          <formatted-text>
            <run fontname='Tableau Bold' fontsize='18'>Care-gap closure: who to call first</run>
          </formatted-text>
        </title>
      </layout-options>
      <size maxheight='900' maxwidth='1400' minheight='900' minwidth='1400' />
      <zones>
        <zone h='100000' id='2' type='layout-basic' w='100000' x='0' y='0'>
          <zone h='6000' id='3' type='title' w='100000' x='0' y='0' />
          <zone h='7000' id='4' type='text' w='100000' x='0' y='6000'>
            <formatted-text>
              <run fontsize='11'>{xml_escape(subtitle)}</run>
            </formatted-text>
          </zone>
          <zone h='16000' id='5' name='Headline' show-title='true' w='100000' x='0' y='13000' />
          <zone h='28000' id='6' name='Gaps by condition' show-title='true' w='50000' x='0' y='29000' />
          <zone h='28000' id='7' name='Call first' show-title='true' w='50000' x='50000' y='29000' />
          <zone h='22000' id='8' name='Where to expand' show-title='true' w='50000' x='0' y='57000' />
          <zone h='22000' id='9' name='Where it is already lower' show-title='true' w='50000' x='50000' y='57000' />
          <zone h='21000' id='10' name='Monthly trend' show-title='true' w='100000' x='0' y='79000' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
  <windows source-height='30'>
{windows}
  </windows>
</workbook>
"""
    WORKBOOK.parent.mkdir(parents=True, exist_ok=True)
    WORKBOOK.write_text(workbook)
    packaged = WORKBOOK.with_suffix(".twbx")
    with zipfile.ZipFile(packaged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(WORKBOOK, "Care Gap Closure.twb")
        for csv_path in sorted(DATA.glob("*.csv")):
            archive.write(csv_path, f"Data/{csv_path.name}")
    print(f"packaged {packaged}")


def write_narrative(summary: dict, results: list[dict]) -> None:
    flagged = sum(row["issues"] for row in results if row["kind"] == "flag")
    checks = len(results)
    definition = f"""Care gap definition
As of {AS_OF}. Massachusetts Synthea extract.

A care gap is an eligible diagnosis with no medication and no procedure for that patient in the 90 days after the diagnosis date.

Eligible means two things. The diagnosis is not a "History of ..." finding, because that is not an active condition. The diagnosis date is on or before {FOLLOW_UP_CUTOFF}, because the extract ends {AS_OF} and a later diagnosis has not had 90 days to be treated.

Any medication or procedure in that window closes the gap, even if it was for something else. A clinician would narrow this before a real outreach program. The rule is deliberately simple so the measurement can be explained in one sentence.

Result of the rule: {summary['gap_conditions']:,} gap conditions out of {summary['eligible_conditions']:,} eligible conditions ({summary['condition_gap_rate']}%). Those gaps belong to {summary['gap_patients']:,} patients, {summary['patient_gap_rate']}% of the {summary['eligible_patients']:,} patients who have an eligible condition.

Priority is separate from the count, and it is who to call now. Deceased patients are left off the worklist. Overdose and first-trimester miscarriage raise the score only when the diagnosis was on or after 2018-04-28. Chronic gaps (hypertension, prediabetes, obesity, anemia, and related conditions) score higher, as do age 65 and older, several gaps at once, and a visit in the past year, because those patients can still be reached. High is a score of 5 or more. Medium is 3 or 4. Low is 2 or less. DENSE_RANK orders patients inside each provider so every clinician gets their own list.

{summary['history_excluded']:,} history findings and {summary['too_new_excluded']:,} diagnoses without 90 days of follow-up were left out of the rate. {summary['deceased_left_off']:,} patients with a gap had already died by the extract date and are not on the call list.
"""
    plan = f"""How we would know if outreach worked

Metric: condition gap rate. Gap conditions divided by eligible conditions.
Baseline from this extract: {summary['condition_gap_rate']}% ({summary['gap_conditions']:,} of {summary['eligible_conditions']:,}).
Patient view of the same baseline: {summary['patient_gap_rate']}% of patients with an eligible condition have at least one gap ({summary['gap_patients']:,} of {summary['eligible_patients']:,}).

After a real program, rebuild condition_gap each month with the same 90-day rule and read monthly_gap_trend. The rate_change_pts column is this month's gap rate minus last month's rate, from LAG. A falling rate, in the organizations that used the worklist, is the success signal. Compare those organizations with organizations that were not in the rollout.

This file is the measurement setup only. The records are synthetic. Nobody was called. There is no before-and-after improvement to claim.

Call-first list: {summary['high_priority']:,} High priority patients, then {summary['medium_priority']:,} Medium, then {summary['low_priority']:,} Low. The full list is output/outreach_worklist.csv, ranked inside {summary['providers_on_worklist']:,} providers. Organization comparison uses {summary['organizations_compared']:,} organizations that have at least 40 eligible conditions.
"""
    bullets = f"""Built a data-validation layer (Python + SQL) with {checks} automated checks (duplicate IDs, orphan records, illogical dates) over {summary['patients']:,} synthetic patients and {summary['encounters']:,} encounters, flagging {flagged} issues before reporting.

Defined and measured a care-gap metric (condition with no treatment within 90 days) using joins, anti-joins and HAVING, identifying {summary['gap_conditions']:,} gaps ({summary['patient_gap_rate']}% of patients with an eligible condition have at least one).

Created a prioritized outreach worklist using CASE risk bands and DENSE_RANK per provider, so a care team could contact the highest-priority {summary['high_priority']:,} patients first.

Compared gap rates across {summary['organizations_compared']:,} organizations to identify where the workflow would likely perform best, and designed a baseline of {summary['condition_gap_rate']}% plus a monthly LAG trend for measuring outcomes after rollout.

Automated the pipeline so one script reruns validation, queries and exports for a Tableau dashboard of the gap rate, the priority list, the organization comparison and the monthly trend.
"""
    (ROOT / "care_gap_definition.txt").write_text(definition)
    (ROOT / "measurement_plan.txt").write_text(plan)
    (ROOT / "resume_bullets_care_gap.txt").write_text(bullets)
    print(bullets)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    load_organizations()
    run_sql()
    connection = connect()
    try:
        with connection.cursor() as cursor:
            results = fetch_checks(cursor)
            write_checks(results)
            frames = export_frames(cursor)
    finally:
        connection.close()
    write_workbook(frames["summary"])
    write_narrative(frames["summary"], results)
    print(f"workbook: {WORKBOOK}")


if __name__ == "__main__":
    main()
