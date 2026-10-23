"""Care-gap dashboard. Reads the exported results, so the site does not need MySQL."""

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "tableau" / "data"
NAVY = "#1F4E79"

st.set_page_config(page_title="Care-gap closure", layout="wide")
st.title("Care-gap closure: who to call first")
st.caption(
    "A care gap is an eligible diagnosis with no medication and no procedure "
    "in the next 90 days. These records are synthetic. The 30.5% rate is the "
    "baseline before any outreach, not a result of a program."
)


def load(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / name)


kpis = load("kpis.csv")
conditions = load("gaps_by_condition.csv")
opportunity = load("org_opportunity.csv")
best = load("org_best.csv")
monthly = load("monthly_gap_rate.csv")
call_first = load("worklist_top.csv")
worklist = pd.read_csv(ROOT / "output" / "outreach_worklist.csv")

values = dict(zip(kpis["Metric"], kpis["Value"]))
left, mid_left, mid_right, right = st.columns(4)
left.metric("Gap conditions", f"{int(values['Gap conditions']):,}")
mid_left.metric("Patients with a gap", f"{int(values['Patients with a gap']):,}")
mid_right.metric("Call first", f"{int(values['Call first (High)']):,}")
right.metric("Baseline gap rate", f"{values['Baseline gap rate']}%")


def bars(frame: pd.DataFrame, label: str, value: str, title: str) -> alt.Chart:
    return (
        alt.Chart(frame)
        .mark_bar(color=NAVY)
        .encode(
            x=alt.X(f"{value}:Q", title=value),
            y=alt.Y(f"{label}:N", sort="-x", title=None),
            tooltip=[label, value],
        )
        .properties(title=title, height=320)
    )


chart_left, chart_right = st.columns(2)
with chart_left:
    st.altair_chart(
        bars(conditions, "Condition", "Gaps", "Where the gaps concentrate"),
        use_container_width=True,
    )
with chart_right:
    ranked = call_first.sort_values("Score", ascending=False)
    st.altair_chart(
        alt.Chart(ranked)
        .mark_bar()
        .encode(
            x=alt.X("Score:Q", title="Priority score"),
            y=alt.Y("Patient:N", sort="-x", title=None),
            color=alt.Color(
                "Priority:N",
                scale=alt.Scale(
                    domain=["High", "Medium", "Low"],
                    range=["#8B1E3F", "#C47B2B", "#1F4E79"],
                ),
                legend=alt.Legend(title="Priority"),
            ),
            tooltip=["Patient", "Score", "Priority", "LeadCondition", "Age"],
        )
        .properties(title="Highest-priority patients", height=320),
        use_container_width=True,
    )

org_left, org_right = st.columns(2)
with org_left:
    st.altair_chart(
        bars(
            opportunity,
            "Organization",
            "GapRate",
            "Highest gap rate, sites with at least 40 conditions",
        ),
        use_container_width=True,
    )
with org_right:
    st.altair_chart(
        bars(
            best,
            "Organization",
            "GapRate",
            "Lowest gap rate, sites with at least 40 conditions",
        ),
        use_container_width=True,
    )

st.altair_chart(
    alt.Chart(monthly)
    .mark_line(color=NAVY, point=True)
    .encode(
        x=alt.X("Month:N", title="Month", sort=None),
        y=alt.Y("GapRate:Q", title="Gap rate (%)"),
        tooltip=["Month", "GapRate", "PriorGapRate", "RateChange"],
    )
    .properties(title="Monthly gap rate", height=280),
    use_container_width=True,
)

st.subheader("Outreach worklist")
band = st.selectbox("Priority", ["High", "Medium", "Low", "All"], index=0)
shown = worklist if band == "All" else worklist[worklist["Priority"] == band]
st.dataframe(
    shown[
        [
            "ProviderRank",
            "Priority",
            "Score",
            "Patient",
            "Age",
            "LeadCondition",
            "Organization",
            "City",
            "LastVisit",
        ]
    ],
    use_container_width=True,
    hide_index=True,
)
st.caption(f"{len(shown):,} patients shown. {len(worklist):,} living patients are on the full list.")
