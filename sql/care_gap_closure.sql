-- Care-gap closure measurement
-- Rule: an eligible condition with no medication and no procedure in the
-- 90 days after the diagnosis date. History findings are not active
-- conditions. Diagnoses after 2020-01-28 are excluded because the extract
-- ends 2020-04-28 and they do not have 90 days of follow-up.
-- Synthetic data contains no outreach program. These tables are the baseline.

USE healthcare_analytics;
SET NAMES utf8mb4;

DROP VIEW IF EXISTS v_monthly_gap_trend;
DROP VIEW IF EXISTS v_org_gap_rate;
DROP VIEW IF EXISTS v_outreach_worklist;
DROP TABLE IF EXISTS monthly_gap_trend;
DROP TABLE IF EXISTS org_gap_rate;
DROP TABLE IF EXISTS gap_by_condition;
DROP TABLE IF EXISTS outreach_worklist;
DROP TABLE IF EXISTS gap_lead;
DROP TABLE IF EXISTS patient_gap;
DROP TABLE IF EXISTS condition_gap;

CREATE TABLE condition_gap (
  condition_id INT NOT NULL,
  patient_id CHAR(36) NOT NULL,
  encounter_id CHAR(36) NOT NULL,
  start_date DATE NOT NULL,
  description VARCHAR(180) NOT NULL,
  clinical_class VARCHAR(16) NOT NULL,
  is_gap TINYINT NOT NULL,
  PRIMARY KEY (condition_id),
  KEY ix_gap_patient (patient_id, is_gap),
  KEY ix_gap_encounter (encounter_id)
) ENGINE=InnoDB;

INSERT INTO condition_gap (
  condition_id, patient_id, encounter_id, start_date, description, clinical_class, is_gap
)
SELECT
  c.id,
  c.patient_id,
  c.encounter_id,
  c.start_date,
  c.description,
  CASE
    WHEN c.description IN ('Drug overdose', 'Miscarriage in first trimester')
     AND c.start_date >= DATE('2018-04-28') THEN 'urgent'
    WHEN c.description IN (
      'Hypertension',
      'Prediabetes',
      'Body mass index 30+ - obesity (finding)',
      'Anemia (disorder)',
      'Chronic sinusitis (disorder)',
      'Osteoporosis (disorder)',
      'Atrial Fibrillation',
      'Coronary Heart Disease',
      'Hyperlipidemia',
      'Metabolic syndrome X',
      'Chronic kidney disease stage 1',
      'Chronic kidney disease stage 2',
      'Chronic kidney disease stage 3',
      'Chronic obstructive pulmonary disease'
    )
    OR c.description LIKE '%diabetes%'
    OR c.description LIKE '%Diabetes%'
    OR c.description LIKE '%asthma%'
    OR c.description LIKE '%Asthma%'
    OR c.description LIKE '%heart failure%'
    OR c.description LIKE '%Heart failure%'
    THEN 'chronic'
    ELSE 'acute'
  END,
  CASE
    WHEN NOT EXISTS (
      SELECT 1
      FROM medications m
      WHERE m.patient_id = c.patient_id
        AND DATE(m.start_ts) >= c.start_date
        AND DATE(m.start_ts) < c.start_date + INTERVAL 90 DAY
    )
    AND NOT EXISTS (
      SELECT 1
      FROM procedures p
      WHERE p.patient_id = c.patient_id
        AND DATE(p.procedure_date) >= c.start_date
        AND DATE(p.procedure_date) < c.start_date + INTERVAL 90 DAY
    )
    THEN 1 ELSE 0
  END
FROM conditions c
WHERE c.start_date <= DATE('2020-01-28')
  AND c.description NOT LIKE 'History of %';

CREATE TABLE patient_gap AS
SELECT
  patient_id,
  COUNT(*) AS gap_count,
  SUM(clinical_class = 'chronic') AS chronic_gaps,
  SUM(clinical_class = 'urgent') AS urgent_gaps
FROM condition_gap
WHERE is_gap = 1
GROUP BY patient_id;

CREATE TABLE gap_lead AS
SELECT patient_id, encounter_id, description AS lead_condition, clinical_class
FROM (
  SELECT
    patient_id,
    encounter_id,
    description,
    clinical_class,
    ROW_NUMBER() OVER (
      PARTITION BY patient_id
      ORDER BY FIELD(clinical_class, 'urgent', 'chronic', 'acute'), start_date DESC
    ) AS rn
  FROM condition_gap
  WHERE is_gap = 1
) ranked
WHERE rn = 1;

CREATE TABLE outreach_worklist AS
SELECT
  scored.patient_id,
  scored.patient_name,
  scored.gender,
  scored.age_years,
  scored.city,
  scored.provider_id,
  scored.provider_name,
  scored.speciality,
  scored.organization_name,
  scored.gap_count,
  scored.chronic_gaps,
  scored.urgent_gaps,
  scored.lead_condition,
  scored.last_encounter_date,
  scored.days_since_visit,
  scored.priority_score,
  scored.priority_band,
  DENSE_RANK() OVER (
    PARTITION BY scored.provider_id
    ORDER BY scored.priority_score DESC, scored.chronic_gaps DESC, scored.age_years DESC, scored.patient_name
  ) AS provider_rank
FROM (
  SELECT
    base.*,
    CASE
      WHEN base.priority_score >= 5 THEN 'High'
      WHEN base.priority_score >= 3 THEN 'Medium'
      ELSE 'Low'
    END AS priority_band
  FROM (
    SELECT
      g.patient_id,
      CONCAT(pt.first_name, ' ', pt.last_name) AS patient_name,
      pt.gender,
      TIMESTAMPDIFF(YEAR, pt.birthdate, DATE('2020-04-28')) AS age_years,
      pt.city,
      e.provider_id,
      pr.name AS provider_name,
      pr.speciality,
      o.name AS organization_name,
      g.gap_count,
      g.chronic_gaps,
      g.urgent_gaps,
      gap_owner.lead_condition,
      lv.last_encounter_date,
      DATEDIFF(DATE('2020-04-28'), lv.last_encounter_date) AS days_since_visit,
      (
        IF(g.urgent_gaps > 0, 3, 0)
        + IF(g.chronic_gaps >= 1, 2, 0)
        + IF(g.chronic_gaps >= 3, 1, 0)
        + IF(TIMESTAMPDIFF(YEAR, pt.birthdate, DATE('2020-04-28')) >= 75, 2,
            IF(TIMESTAMPDIFF(YEAR, pt.birthdate, DATE('2020-04-28')) >= 65, 1, 0))
        + IF(g.gap_count >= 4, 1, 0)
        + IF(DATEDIFF(DATE('2020-04-28'), lv.last_encounter_date) <= 365, 2, 0)
      ) AS priority_score
    FROM patient_gap g
    JOIN patients pt ON pt.id = g.patient_id
    JOIN gap_lead gap_owner ON gap_owner.patient_id = g.patient_id
    JOIN encounters e ON e.id = gap_owner.encounter_id
    JOIN providers pr ON pr.id = e.provider_id
    LEFT JOIN organizations o ON o.id = e.organization_id
    JOIN (
      SELECT patient_id, DATE(MAX(start_ts)) AS last_encounter_date
      FROM encounters
      GROUP BY patient_id
    ) lv ON lv.patient_id = g.patient_id
    WHERE pt.deathdate IS NULL
  ) base
) scored;

CREATE TABLE gap_by_condition AS
SELECT
  description AS condition_name,
  clinical_class,
  COUNT(*) AS eligible_conditions,
  SUM(is_gap) AS gap_conditions,
  COUNT(DISTINCT CASE WHEN is_gap = 1 THEN patient_id END) AS gap_patients,
  ROUND(100 * SUM(is_gap) / COUNT(*), 1) AS gap_rate
FROM condition_gap
GROUP BY description, clinical_class
HAVING SUM(is_gap) >= 15;

CREATE TABLE org_gap_rate AS
SELECT
  o.name AS organization_name,
  o.city,
  COUNT(*) AS eligible_conditions,
  SUM(g.is_gap) AS gap_conditions,
  ROUND(100 * SUM(g.is_gap) / COUNT(*), 1) AS gap_rate
FROM condition_gap g
JOIN encounters e ON e.id = g.encounter_id
JOIN organizations o ON o.id = e.organization_id
GROUP BY o.id, o.name, o.city
HAVING COUNT(*) >= 40;

CREATE TABLE monthly_gap_trend AS
SELECT
  month_start,
  eligible_conditions,
  gap_conditions,
  gap_rate,
  LAG(gap_rate) OVER (ORDER BY month_start) AS prior_gap_rate,
  ROUND(gap_rate - LAG(gap_rate) OVER (ORDER BY month_start), 1) AS rate_change_pts
FROM (
  SELECT
    STR_TO_DATE(DATE_FORMAT(start_date, '%Y-%m-01'), '%Y-%m-%d') AS month_start,
    COUNT(*) AS eligible_conditions,
    SUM(is_gap) AS gap_conditions,
    ROUND(100 * SUM(is_gap) / COUNT(*), 1) AS gap_rate
  FROM condition_gap
  GROUP BY STR_TO_DATE(DATE_FORMAT(start_date, '%Y-%m-01'), '%Y-%m-%d')
) monthly;

SELECT priority_band, COUNT(*) AS patients
FROM outreach_worklist
GROUP BY priority_band
ORDER BY FIELD(priority_band, 'High', 'Medium', 'Low');

SELECT
  SUM(is_gap) AS gap_conditions,
  COUNT(*) AS eligible_conditions,
  ROUND(100 * SUM(is_gap) / COUNT(*), 1) AS condition_gap_rate
FROM condition_gap;

SELECT COUNT(*) AS gap_patients FROM patient_gap;

SELECT COUNT(DISTINCT patient_id) AS eligible_patients FROM condition_gap;
