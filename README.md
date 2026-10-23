# Care-gap closure

Synthetic Massachusetts healthcare extract. A care gap is an eligible diagnosis with no medication and no procedure in the next 90 days. The 30.5% rate is the baseline before any outreach.

## Dashboard

```bash
pip install -r requirements.txt
streamlit run app.py
```

The page reads the exported files in `tableau/data` and `output/outreach_worklist.csv`.

## Rebuild the numbers

MySQL must be running locally, with the database already loaded by `python/build_healthcare.py`.

```bash
python python/care_gap_pipeline.py
```

The Tableau workbook is `tableau/Care Gap Closure.twbx`.
