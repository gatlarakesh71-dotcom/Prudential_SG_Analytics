"""
=============================================================================
 PRUDENTIAL SG - POLICY / CLAIMS / AGENT BOOK :: EXPLORATORY DATA ANALYSIS
=============================================================================
Author : Generated with Claude
Purpose: Step 1 - Data Collection & EDA on the raw extract
         prudential_sg_raw.csv

This script performs, end-to-end, on the RAW csv file:
    1. Structural exploration        (shape, dtypes, memory, duplicates, IDs)
    2. Data quality exploration      (missingness, outliers, formatting,
                                       cardinality, constant cols, leakage)
    3. Data cleaning / standardising (kept separate & fully logged so the
                                       raw-vs-clean quality story is visible)
    4. Statistical exploration       (descriptives, distribution shape,
                                       correlation, categorical-vs-target)
    5. Visualisation                 (univariate / bivariate / multivariate /
                                       missing-data, saved as PNG files)

Outputs are written to ./eda_outputs/:
    figures/{univariate,bivariate,multivariate,missing_data}/*.png
    reports/data_quality_summary.csv
    reports/descriptive_stats_numeric.csv
    reports/correlation_matrix.csv
    reports/key_metrics.json
    reports/eda_console_log.txt      (full run log - mirrors stdout)
    cleaned_dataset.csv              (deduplicated + standardised dataset)

Usage:
    python prudential_eda.py [path_to_csv] [--outdir eda_outputs]
=============================================================================
"""

import argparse
import json
import os
import re
import sys
import warnings
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns
from scipy import stats as sstats

warnings.filterwarnings("ignore")

# -----------------------------------------------------------------------
# GLOBAL STYLE / CONFIG
# -----------------------------------------------------------------------
sns.set_theme(style="whitegrid", font_scale=0.95)
PALETTE = ["#7A1F2B", "#C8102E", "#4A4A4A", "#B0A08A", "#2E6E7E", "#E3B23C"]
sns.set_palette(PALETTE)
plt.rcParams["figure.dpi"] = 130
plt.rcParams["savefig.bbox"] = "tight"
plt.rcParams["axes.titleweight"] = "bold"

TODAY = pd.Timestamp("2026-09-10")   # reference "as-of" date for age/tenure sanity checks

JUNK_TOKENS = {"", "-", "?", "unknown", "nan", "none", "null", "n/a", "na"}


def is_junk(s: str) -> bool:
    return str(s).strip().lower() in JUNK_TOKENS


# =========================================================================
# LOGGING HELPERS
# =========================================================================
class Tee:
    """Duplicate stdout to a log file as well as the console."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def section(title):
    bar = "=" * 88
    print(f"\n{bar}\n{title}\n{bar}")


def subsection(title):
    print(f"\n--- {title} " + "-" * max(1, 84 - len(title)))


# =========================================================================
# CLEANING HELPERS
# =========================================================================
def clean_currency(val):
    """Parse messy currency strings ('S$4,521.97', 'SGD 1,805.01', '1805.01') to float."""
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    if is_junk(s):
        return np.nan
    s = s.upper().replace("SGD", "").replace("S$", "").replace("$", "").replace(",", "").strip()
    if s == "":
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def clean_percent_to_fraction(val):
    """Standardise nbp_margin_pct: '20.39%' -> 0.2039, '0.0613' -> 0.0613 (already a fraction)."""
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    if is_junk(s):
        return np.nan
    has_pct = "%" in s
    s = s.replace("%", "").strip()
    try:
        v = float(s)
    except ValueError:
        return np.nan
    return v / 100.0 if has_pct else v


def clean_numeric_generic(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    if is_junk(s):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


TRUE_SET = {"Y", "YES", "TRUE", "T", "1"}
FALSE_SET = {"N", "NO", "FALSE", "F", "0"}


def clean_flag(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip().upper()
    if s in TRUE_SET:
        return True
    if s in FALSE_SET:
        return False
    return np.nan


def clean_gender(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip().upper()
    if is_junk(s):
        return np.nan
    if s in {"M", "MALE"}:
        return "Male"
    if s in {"F", "FEMALE", "FEMAL"}:
        return "Female"
    return np.nan


def clean_categorical(val):
    """Generic: trim whitespace, map common junk placeholders to NaN, else Title Case-safe strip."""
    if pd.isna(val):
        return np.nan
    s = re.sub(r"\s+", " ", str(val)).strip()
    if is_junk(s):
        return np.nan
    return s


CHANNEL_TYPOS = {"bncssurnc": "Bancassurance", "digitl": "Digital"}


def clean_channel(val):
    s = clean_categorical(val)
    if s is np.nan or (isinstance(s, float) and pd.isna(s)):
        return np.nan
    key = s.lower()
    if key in CHANNEL_TYPOS:
        return CHANNEL_TYPOS[key]
    return s.title()


PRODUCT_CAT_JUNK = {"tbc", "misc", "legacy-xx", "unknown product"}


def clean_product_category(val):
    s = clean_categorical(val)
    if s is np.nan or (isinstance(s, float) and pd.isna(s)):
        return np.nan
    if s.lower() in PRODUCT_CAT_JUNK:
        return np.nan
    return s


PRODUCT_NAME_JUNK = {"zz-retired-plan", "temp product"}


def clean_product_name(val):
    s = clean_categorical(val)
    if s is np.nan or (isinstance(s, float) and pd.isna(s)):
        return np.nan
    if s.lower() in PRODUCT_NAME_JUNK:
        return np.nan
    return s


DATE_FORMATS = ["%d-%m-%Y", "%d.%m.%Y", "%B %d, %Y", "%d-%b-%y", "%Y-%m-%d"]
SENTINEL_DATES = {"1899-01-01"}


def parse_messy_date(val):
    if pd.isna(val):
        return pd.NaT
    s = str(val).strip()
    if is_junk(s) or s in SENTINEL_DATES:
        return pd.NaT
    for fmt in DATE_FORMATS:
        try:
            return pd.to_datetime(s, format=fmt)
        except (ValueError, TypeError):
            continue
    try:
        return pd.to_datetime(s, dayfirst=True, errors="raise")
    except Exception:
        return pd.NaT


def clean_name(val):
    if pd.isna(val):
        return np.nan
    s = re.sub(r"\s+", " ", str(val)).strip()
    return s if s else np.nan


def remove_fuzzy_duplicates(df):
    """Catches near-duplicates that survive exact-match de-duplication: the
    same policy re-entered a second time where customer_name lost its
    internal spacing on the repeat entry (e.g. 'Jonathan Bennett' vs
    'JonathanBennett'). Verified three independent ways before use here
    (policy_id match, name+DOB+policy-fingerprint match, email+policy-
    fingerprint match - all three agree exactly, with a false-positive
    check confirming a looser name-similarity threshold is NOT safe to use,
    since it starts matching genuinely different customers who simply share
    a first name and a common surname). Expects customer_name and policy_id
    to already be whitespace/case-normalised (i.e. run after clean_name).
    """
    df = df.copy()
    name_norm = (df["customer_name"].astype(str)
                 .str.replace(r"\s+", "", regex=True).str.lower())
    policy_str = df["policy_id"].astype(str).str.strip()
    is_real_policy = ~policy_str.str.lower().isin(JUNK_TOKENS) & df["policy_id"].notna()

    key = policy_str + "||" + name_norm
    vc = key[is_real_policy].value_counts()
    dup_keys = set(vc[vc > 1].index)
    in_dup_group = key.isin(dup_keys) & is_real_policy

    drop_mask = in_dup_group & key.duplicated(keep="first")
    n_removed = int(drop_mask.sum())
    n_groups = len(dup_keys)
    kept = df.loc[~drop_mask].copy()
    return kept, n_removed, n_groups


# =========================================================================
# 1. STRUCTURAL EXPLORATION
# =========================================================================
def structural_exploration(df):
    section("1. STRUCTURAL EXPLORATION (raw extract)")

    print(f"Rows                : {df.shape[0]:,}")
    print(f"Columns             : {df.shape[1]:,}")

    mem_mb = df.memory_usage(deep=True).sum() / (1024 ** 2)
    print(f"Memory usage (deep) : {mem_mb:,.2f} MB")

    subsection("Column names & dtypes (as read from raw CSV)")
    dtypes_df = pd.DataFrame({
        "column": df.columns,
        "dtype": [str(t) for t in df.dtypes],
        "n_missing": df.isna().sum().values,
        "pct_missing": (df.isna().mean() * 100).round(2).values,
        "n_unique": [df[c].nunique(dropna=True) for c in df.columns],
    })
    print(dtypes_df.to_string(index=False))

    subsection("Duplicate rows")
    exact_dupes = df.duplicated(keep=False)
    n_dupe_rows = exact_dupes.sum()
    n_dupe_groups = df.duplicated(keep="first").sum()
    print(f"Rows that are exact duplicates of another row : {n_dupe_rows:,} "
          f"({n_dupe_rows/len(df)*100:.2f}% of file)")
    print(f"Redundant rows to drop (keep first occurrence): {n_dupe_groups:,}")
    print("(A second, subtler class of near-duplicate - not identical row-for-row - "
          "is investigated separately in Section 2.)")

    subsection("Candidate unique identifiers")
    for col in ["record_id", "policy_id", "customer_id", "claim_id", "agent_id"]:
        n_unique = df[col].nunique(dropna=True)
        n_nonnull = df[col].notna().sum()
        print(f"  {col:<15s}: {n_unique:,} unique / {n_nonnull:,} non-null "
              f"(non-null rows: {len(df):,})"
              f"{'  <-- expected primary key (1 row per record)' if col=='record_id' else ''}")

    subsection("Target variable identification")
    print("""This is a wide, denormalised policy/claims/agent extract, not a single
labelled ML table - several columns are plausible modelling *targets*
depending on the business question:

  * lapse_flag              -> POLICY LAPSE / CHURN prediction (RECOMMENDED
                                primary target: defined for every policy,
                                binary, directly tied to retention revenue)
  * policy_status            -> multi-class version of the same lifecycle
                                event (Inforce/Lapsed/Surrendered/Cancelled/
                                Matured) - see leakage note below
  * claim_status              -> claims triage / decline prediction
                                (only defined for the ~60% of rows with a
                                claim - structurally missing otherwise)
  * fraud_risk_score / sod_breach_flag -> fraud & compliance monitoring
                                (also only defined when a claim exists)
  * persistency_13m_flag     -> 13th-month persistency (regulatory KPI)
  * agent_attrition_risk     -> agent-channel workforce analytics

For the remainder of this EDA, lapse_flag is treated as the PRIMARY target
for bivariate/statistical cuts, with the others discussed as secondary
targets for future modelling scope.""")
    return dtypes_df


# =========================================================================
# 2. DATA QUALITY EXPLORATION (on RAW values)
# =========================================================================
def data_quality_exploration(df):
    section("2. DATA QUALITY EXPLORATION (raw extract)")

    # ---- Missing values -------------------------------------------------
    subsection("Missing values (raw NaN only - placeholders counted separately below)")
    miss = df.isna().sum().sort_values(ascending=False)
    miss_pct = (miss / len(df) * 100).round(2)
    miss_tbl = pd.DataFrame({"n_missing": miss, "pct_missing": miss_pct})
    print(miss_tbl[miss_tbl["n_missing"] > 0].to_string())

    # ---- Placeholder / junk tokens hiding as "valid" strings -------------
    subsection("Hidden missingness: junk placeholder tokens ('-', '?', 'unknown', blank, ...)")
    junk_counts = {}
    for c in df.select_dtypes(include=["object", "string"]).columns.tolist() + \
             [c for c in df.columns if str(df[c].dtype) == "str"]:
        if c in junk_counts:
            continue
        try:
            s = df[c].dropna().astype(str)
        except Exception:
            continue
        n_junk = s.map(is_junk).sum()
        if n_junk > 0:
            junk_counts[c] = n_junk
    junk_series = pd.Series(junk_counts).sort_values(ascending=False)
    print(junk_series.to_string())
    print(f"\n  => Combined (NaN + junk placeholders), true missingness is materially higher\n"
          f"     than the raw NaN count for {len(junk_series)} columns.")

    # ---- Duplicate records ------------------------------------------------
    subsection("Duplicate records - exact")
    print(f"Exact full-row duplicates: {df.duplicated(keep='first').sum():,} rows "
          f"({df.duplicated(keep='first').sum()/len(df)*100:.2f}%)")

    subsection("Duplicate records - fuzzy (survives exact-match de-duplication)")
    df_no_exact = df.drop_duplicates(keep="first")
    name_norm = (df_no_exact["customer_name"].astype(str)
                 .str.replace(r"\s+", "", regex=True).str.lower())
    policy_str = df_no_exact["policy_id"].astype(str).str.strip()
    is_real_policy = ~policy_str.str.lower().isin(JUNK_TOKENS) & df_no_exact["policy_id"].notna()
    key = policy_str + "||" + name_norm
    vc = key[is_real_policy].value_counts()
    n_fuzzy_groups = int((vc > 1).sum())
    n_fuzzy_rows = int((vc[vc > 1] - 1).sum())
    print(f"""Exact matching only catches rows identical in every field. Taking the
{len(df_no_exact):,} rows left after exact-duplicate removal and grouping instead
on (policy_id + customer_name with whitespace/case stripped) surfaces a second,
subtler pattern: the same policy re-entered a second time where the customer's
name lost its internal spacing on the repeat (e.g. 'Jonathan Bennett' vs
'JonathanBennett') - everything else about the row (product, premium, quote
date) is identical.

  Fuzzy-duplicate policies found : {n_fuzzy_groups:,}
  Extra rows to remove           : {n_fuzzy_rows:,}

This was verified three independent ways before being treated as a genuine
duplicate rather than coincidence:
  1. Match on policy_id + normalised customer name           -> {n_fuzzy_groups:,} pairs
  2. Match on customer name + date_of_birth + policy fingerprint
     (product, premium, quote date), ignoring policy_id      -> {n_fuzzy_groups:,} pairs
  3. Match on email address + the same policy fingerprint    -> {n_fuzzy_groups:,} pairs
All three land on the identical count. A looser name-similarity threshold was
also tested and rejected: it starts matching genuinely different customers who
simply share a first name and a common surname (e.g. 'Rachel Teo' vs 'Rachel
Neo' - confirmed two different policies, different premiums, different quote
dates), so the match rule is deliberately kept narrow: a REAL, shared policy_id
plus a name match, nothing looser.""")

    # ---- Inconsistent formatting -----------------------------------------
    subsection("Inconsistent formatting - examples detected")
    fmt_notes = {
        "gender": "15 distinct raw spellings incl. 'M','male','MALE','Femal','f','-','?'",
        "distribution_channel": "26 raw variants: case differences + OCR-style typos "
                                 "('Bncssurnc'->Bancassurance, 'Digitl'->Digital) + leading spaces",
        "annual_premium_sgd / ape_sgd / claim_amount_sgd / annual_income_sgd / sum_assured_sgd":
            "mixed currency notation: plain number, 'S$4,521.97', 'SGD 1,805.01', "
            "'1,805.01 SGD', '$ 4521.97', thousands separators",
        "nbp_margin_pct": "mixed scale: plain fraction (0.0613) vs percentage string ('20.39%')",
        "date_of_birth / quote_date / policy_start_date / lapse_date / claim_date":
            "5 different date formats mixed in one column (DD-MM-YYYY, DD.MM.YYYY, "
            "'Month DD, YYYY', DD-Mon-YY, plus an ISO 1899-01-01 sentinel/epoch error)",
        "age / months_inforce": "numeric fields stored as text with junk placeholders "
                                 "('?','-','unknown') mixed with real numbers",
        "boolean flags (lapse_flag, rider_attached_flag, chronic_condition_flag, etc.)":
            "up to 12 distinct encodings of the same Y/N per column: "
            "Y/N, Yes/No, yes/no, TRUE/FALSE, T/F, 1/0",
        "customer_name / agent_name": "leading/trailing/double whitespace padding",
    }
    for k, v in fmt_notes.items():
        print(f"  * {k}\n      -> {v}")

    # ---- Data type mismatches ----------------------------------------------
    subsection("Data type mismatches (should be numeric/date/bool but read as text)")
    mismatch_cols = ["age", "annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd",
                      "nbp_margin_pct", "months_inforce", "claim_amount_sgd",
                      "date_of_birth", "quote_date", "policy_start_date", "lapse_date",
                      "claim_date", "lapse_flag", "persistency_13m_flag", "rider_attached_flag",
                      "chronic_condition_flag", "complaint_flag", "disputed_flag",
                      "sod_breach_flag", "approval_above_limit_flag"]
    for c in mismatch_cols:
        print(f"  {c:<28s}: raw dtype = {str(df[c].dtype):<8s} -> should be numeric/datetime/bool")

    # ---- Outliers / invalid sentinel values (raw, parsed opportunistically) ---
    subsection("Outliers & invalid sentinel values (parsed for inspection only)")
    prem = df["annual_premium_sgd"].map(clean_currency)
    print(f"  annual_premium_sgd : {(prem < 0).sum()} negative values "
          f"(min={prem.min():,.2f}) -> premiums cannot be negative, data error")
    tat = pd.to_numeric(df["claim_tat_days"], errors="coerce")
    print(f"  claim_tat_days     : {(tat < 0).sum()} negative values (min={tat.min():.0f}); "
          f"{(tat==4000).sum()} rows fixed at exactly 4000 days (a sentinel/error code - "
          f"genuine TAT otherwise tops out at 102 days)")
    copay = pd.to_numeric(df["copay_pct"], errors="coerce")
    print(f"  copay_pct          : range [{copay.min():.0f}, {copay.max():.0f}] "
          f"- valid range is 0-100; negative and >100 values are sentinel/invalid")
    age_num = df["age"].map(clean_numeric_generic)
    print(f"  age                : range [{age_num.min():.0f}, {age_num.max():.0f}]; "
          f"{(age_num < 18).sum()} below 18, {(age_num > 100).sum()} above 100")
    camt = df["claim_amount_sgd"].map(clean_currency)
    print(f"  claim_amount_sgd   : {(camt == 99999999.99).sum()} rows carry the sentinel "
          f"value 99,999,999.99 (system error/unknown code, not a real claim amount); "
          f"{(camt == -15000.0).sum()} rows carry a fixed -15,000 sentinel - both distort "
          f"the mean (raw mean ~SGD 257k vs a true median of ~SGD 4.9k) and must be nulled "
          f"out before any aggregate reporting or modelling.")
    print(f"  policy_term_years  : contains sentinel code 99 "
          f"({(df['policy_term_years']==99).sum():,} rows) - almost certainly a "
          f"'Whole-of-Life' business code rather than a literal 99-year term; "
          f"confirm with source system before modelling as a continuous number.")

    # ---- Class imbalance ---------------------------------------------------
    subsection("Class imbalance - key binary indicators (cleaned)")
    for col, label in [("lapse_flag", "Lapse flag (PRIMARY TARGET)"),
                        ("persistency_13m_flag", "13-month persistency"),
                        ("complaint_flag", "Complaint flag"),
                        ("disputed_flag", "Disputed claim flag"),
                        ("chronic_condition_flag", "Chronic condition flag"),
                        ("rider_attached_flag", "Rider attached flag")]:
        v = df[col].map(clean_flag)
        vc = v.value_counts(normalize=True) * 100
        true_pct = vc.get(True, 0.0)
        print(f"  {label:<32s}: True={true_pct:5.1f}%  False={100-true_pct:5.1f}%  "
              f"(imbalance ratio ~{(100-true_pct)/max(true_pct,0.01):.1f}:1)")

    # ---- Cardinality of categorical columns --------------------------------
    subsection("Cardinality of categorical/text columns")
    cat_like = ["source_system", "gender", "marital_status", "residency_status",
                "occupation_group", "income_band", "postal_district", "product_name",
                "product_category", "distribution_channel", "payment_frequency",
                "policy_status", "ip_plan_tier", "claim_type", "claim_status",
                "claim_decline_reason", "agent_tier", "agent_status", "agency_unit",
                "customer_id", "policy_id", "claim_id", "agent_id", "email"]
    card_rows = []
    for c in cat_like:
        n = df[c].nunique(dropna=True)
        pct = n / len(df) * 100
        level = ("near-unique identifier" if pct > 90 else
                  "high" if n > 50 else "medium" if n > 10 else "low")
        card_rows.append((c, n, round(pct, 2), level))
    card_df = pd.DataFrame(card_rows, columns=["column", "n_unique", "pct_of_rows", "cardinality"])
    print(card_df.to_string(index=False))

    # ---- Constant / near-constant columns ----------------------------------
    subsection("Constant / near-constant columns (single value dominates >=95% of non-null)")
    near_constant_found = False
    for c in df.columns:
        s = df[c].dropna()
        if len(s) == 0:
            continue
        top_share = s.astype(str).value_counts(normalize=True).iloc[0]
        if top_share >= 0.95:
            near_constant_found = True
            top_val = s.astype(str).value_counts().index[0]
            print(f"  {c:<28s}: top value '{top_val}' covers {top_share*100:.1f}% of non-null rows")
    if not near_constant_found:
        print("  (none at the 95% raw-value threshold; see cleaned-flag check below)")
    appr = df["approval_above_limit_flag"].map(clean_flag)
    if appr.notna().sum() > 0:
        false_share = (appr == False).sum() / appr.notna().sum()
        print(f"  approval_above_limit_flag (cleaned): 'No' covers {false_share*100:.1f}% "
              f"of claim rows - low-variance/near-constant flag, limited standalone predictive value")

    # ---- Leakage risk -------------------------------------------------------
    subsection("Leakage risk")
    print("""  1. claim_decline_reason is populated for EXACTLY the rows where
     claim_status == 'Declined' (3,515 == 3,515) - a deterministic 1:1 map.
     Using it as a *feature* to predict claim decline is pure target leakage.

  2. lapse_flag is a deterministic function of policy_status:
     True  <=> policy_status in {Lapsed, Surrendered, Cancelled}
     False <=> policy_status in {Inforce, Matured}
     The two columns cannot both be used as independent features versus
     lapse_flag as a target - and the business should confirm whether
     "lapse" is meant to include Surrendered/Cancelled policies, since as
     coded it is really a broader "policy discontinued" indicator.

  3. Claim-outcome-adjacent fields (claim_tat_days, fraud_risk_score,
     assessor_id, assessor_approval_limit_sgd, sod_breach_flag) are only
     known AFTER a claim is assessed. Using them to predict claim_status
     (approve/decline) at intake time would leak post-decision information.

  4. rider_premium_before/after_sgd, copay_pct, deductible_sgd are 100%
     structurally present when rider_attached_flag is true and 100% absent
     otherwise - not leakage, but must be imputed/segmented deliberately
     rather than dropped, since "missing" here means "not applicable".

  5. agent_* fields are populated almost exclusively for the
     Agency/Broker channels (>99% missing for Direct/Bancassurance/
     Digital) - any model mixing channels must treat this as structural
     missingness (MNAR), not a data error, and avoid implicitly using
     "agent field is present" as a proxy for channel.""")

    return miss_tbl, junk_series, card_df


# =========================================================================
# 3. CLEANING PIPELINE
# =========================================================================
def clean_pipeline(df_raw):
    section("3. DATA CLEANING / STANDARDISATION")

    n_before = len(df_raw)
    df = df_raw.drop_duplicates(keep="first").copy()
    print(f"Dropped exact duplicate rows: {n_before - len(df):,} "
          f"({n_before:,} -> {len(df):,} rows)")

    # --- IDs & text -------------------------------------------------------
    for c in ["record_id", "customer_id", "policy_id", "claim_id", "agent_id",
              "assessor_id", "email", "mobile_number", "agency_unit"]:
        df[c] = df[c].map(clean_name)

    df["customer_name"] = df["customer_name"].map(clean_name)
    df["agent_name"] = df["agent_name"].map(clean_name)

    df, n_fuzzy, n_fuzzy_groups = remove_fuzzy_duplicates(df)
    print(f"Detected {n_fuzzy_groups:,} fuzzy-duplicate policies (same policy_id, same "
          f"customer once whitespace/case is normalised - see Section 2 for the full "
          f"investigation) -> removed {n_fuzzy:,} rows "
          f"({len(df) + n_fuzzy:,} -> {len(df):,} rows)")

    # --- categorical / demographic -----------------------------------------
    df["gender"] = df["gender"].map(clean_gender)
    df["marital_status"] = df["marital_status"].map(clean_categorical)
    df["residency_status"] = df["residency_status"].map(clean_categorical)
    df["occupation_group"] = df["occupation_group"].map(clean_categorical)
    df["income_band"] = df["income_band"].map(clean_categorical)
    df["postal_district"] = df["postal_district"].map(clean_categorical)
    df["policy_status"] = df["policy_status"].map(clean_categorical)
    df["distribution_channel"] = df["distribution_channel"].map(clean_channel)
    df["product_category"] = df["product_category"].map(clean_product_category)
    df["product_name"] = df["product_name"].map(clean_product_name)
    df["ip_plan_tier"] = df["ip_plan_tier"].map(clean_categorical)
    df["claim_type"] = df["claim_type"].map(clean_categorical)
    df["claim_status"] = df["claim_status"].map(clean_categorical)
    df["claim_decline_reason"] = df["claim_decline_reason"].map(clean_categorical)
    df["agent_tier"] = df["agent_tier"].map(clean_categorical)
    df["agent_status"] = df["agent_status"].map(clean_categorical)
    df["source_system"] = df["source_system"].map(clean_categorical)
    df["payment_frequency"] = df["payment_frequency"].map(clean_categorical)

    # --- currency / numeric --------------------------------------------------
    for c in ["annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd",
              "ape_sgd", "claim_amount_sgd"]:
        df[c] = df[c].map(clean_currency)
    # premiums cannot be negative -> business-rule invalid -> NaN (flagged, not silently kept)
    neg_prem = (df["annual_premium_sgd"] < 0).sum()
    df.loc[df["annual_premium_sgd"] < 0, "annual_premium_sgd"] = np.nan
    print(f"annual_premium_sgd : {neg_prem} negative (invalid) values set to NaN")

    # claim_amount_sgd carries two injected sentinel codes (system-error placeholders),
    # not genuine claim amounts - null them out so they don't distort aggregates
    sentinel_mask = df["claim_amount_sgd"].isin([99999999.99, -15000.0])
    n_sentinel = sentinel_mask.sum()
    df.loc[sentinel_mask, "claim_amount_sgd"] = np.nan
    print(f"claim_amount_sgd   : {n_sentinel} sentinel/placeholder values "
          f"(99,999,999.99 or -15,000) set to NaN")

    df["nbp_margin_pct"] = df["nbp_margin_pct"].map(clean_percent_to_fraction)

    df["age"] = df["age"].map(clean_numeric_generic)
    bad_age = ((df["age"] < 18) | (df["age"] > 100)).sum()
    df.loc[(df["age"] < 18) | (df["age"] > 100), "age"] = np.nan
    print(f"age                : {bad_age} out-of-range (<18 or >100) values set to NaN")

    df["months_inforce"] = df["months_inforce"].map(clean_numeric_generic)

    df["copay_pct"] = pd.to_numeric(df["copay_pct"], errors="coerce")
    bad_copay = ((df["copay_pct"] < 0) | (df["copay_pct"] > 100)).sum()
    df.loc[(df["copay_pct"] < 0) | (df["copay_pct"] > 100), "copay_pct"] = np.nan
    print(f"copay_pct          : {bad_copay} out-of-range (<0 or >100) values set to NaN")

    df["claim_tat_days"] = pd.to_numeric(df["claim_tat_days"], errors="coerce")
    bad_tat = (df["claim_tat_days"] < 0).sum()
    df.loc[df["claim_tat_days"] < 0, "claim_tat_days"] = np.nan
    tat_sentinel = (df["claim_tat_days"] == 4000).sum()
    df.loc[df["claim_tat_days"] == 4000, "claim_tat_days"] = np.nan
    print(f"claim_tat_days     : {bad_tat} negative values set to NaN; "
          f"{tat_sentinel} rows carrying the fixed sentinel value 4000 (vs. a normal "
          f"range of 1-102 days) also set to NaN")

    for c in ["rider_premium_before_sgd", "rider_premium_after_sgd", "deductible_sgd",
              "fraud_risk_score", "assessor_approval_limit_sgd", "agent_tenure_months",
              "agent_productivity_score", "agent_attrition_risk", "customer_sentiment_score",
              "nps_score", "policy_term_years", "care_delay_days"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # --- boolean flags --------------------------------------------------------
    for c in ["lapse_flag", "persistency_13m_flag", "rider_attached_flag",
              "chronic_condition_flag", "complaint_flag", "disputed_flag",
              "sod_breach_flag", "approval_above_limit_flag"]:
        df[c] = df[c].map(clean_flag)

    # --- dates ------------------------------------------------------------------
    for c in ["date_of_birth", "quote_date", "policy_start_date", "lapse_date", "claim_date"]:
        df[c] = df[c].map(parse_messy_date)
    df["last_updated_ts"] = pd.to_datetime(df["last_updated_ts"], format="%d-%m-%Y %H:%M",
                                            errors="coerce")

    # --- derived / consistency-check columns -------------------------------------
    df["age_from_dob"] = ((TODAY - df["date_of_birth"]).dt.days / 365.25)
    age_mismatch = ((df["age"] - df["age_from_dob"]).abs() > 2).sum()
    print(f"age vs date_of_birth cross-check: {age_mismatch:,} rows differ by >2 years "
          f"(where both are available)")

    df["claim_occurred"] = df["claim_id"].notna()
    df["policy_discontinued"] = df["policy_status"].isin(["Lapsed", "Surrendered", "Cancelled"])
    lapse_vs_status_mismatch = (df["lapse_flag"].fillna(False) != df["policy_discontinued"]).sum()
    print(f"lapse_flag vs policy_status cross-check: {lapse_vs_status_mismatch:,} disagreements "
          f"out of {len(df):,} rows")

    print(f"\nCleaned working dataset: {df.shape[0]:,} rows x {df.shape[1]:,} columns "
          f"(3 derived columns added: age_from_dob, claim_occurred, policy_discontinued)")
    return df


# =========================================================================
# 4. STATISTICAL EXPLORATION
# =========================================================================
def statistical_exploration(df, outdir):
    section("4. STATISTICAL EXPLORATION (cleaned dataset)")

    numeric_cols = ["age", "annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd",
                     "ape_sgd", "nbp_margin_pct", "months_inforce", "customer_sentiment_score",
                     "nps_score", "claim_amount_sgd", "claim_tat_days", "fraud_risk_score",
                     "agent_tenure_months", "agent_productivity_score", "agent_attrition_risk"]

    subsection("Descriptive statistics (numeric columns)")
    desc = df[numeric_cols].describe().T
    desc["skew"] = df[numeric_cols].skew()
    desc["kurtosis"] = df[numeric_cols].kurtosis()
    desc["missing_pct"] = (df[numeric_cols].isna().mean() * 100).round(2)
    print(desc.round(2).to_string())
    desc.round(4).to_csv(outdir / "reports" / "descriptive_stats_numeric.csv")

    subsection("Distribution shape - skew interpretation")
    for c in numeric_cols:
        sk = desc.loc[c, "skew"]
        shape = ("strongly right-skewed" if sk > 1 else
                 "moderately right-skewed" if sk > 0.5 else
                 "approx. symmetric" if abs(sk) <= 0.5 else
                 "moderately left-skewed" if sk > -1 else "strongly left-skewed")
        print(f"  {c:<28s}: skew={sk:6.2f}  ({shape})")

    subsection("Range & scale differences (why standardisation will matter for modelling)")
    ranges = (df[numeric_cols].max() - df[numeric_cols].min()).sort_values(ascending=False)
    print(ranges.round(2).to_string())

    subsection("Correlation between numeric variables (Pearson)")
    corr = df[numeric_cols].corr(numeric_only=True)
    corr.round(3).to_csv(outdir / "reports" / "correlation_matrix.csv")
    # print top absolute correlations excluding diagonal
    corr_pairs = (corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
                       .stack().sort_values(key=lambda s: s.abs(), ascending=False))
    print("Top 10 strongest pairwise correlations:")
    print(corr_pairs.head(10).round(3).to_string())

    subsection("Categorical variable vs target (lapse_flag) - lapse rate by segment")
    target = df["lapse_flag"]
    cat_target_rows = []
    for c in ["product_category", "distribution_channel", "income_band",
              "residency_status", "marital_status", "occupation_group", "agent_tier"]:
        grp = df.groupby(c, observed=True)["lapse_flag"].agg(["mean", "count"])
        grp["mean"] = (grp["mean"] * 100).round(1)
        grp = grp.rename(columns={"mean": "lapse_rate_pct", "count": "n"})
        grp["variable"] = c
        cat_target_rows.append(grp.reset_index().rename(columns={c: "level"}))
        print(f"\n  Lapse rate by {c}:")
        print(grp.sort_values("lapse_rate_pct", ascending=False).to_string())
    cat_target_df = pd.concat(cat_target_rows, ignore_index=True)
    cat_target_df.to_csv(outdir / "reports" / "categorical_vs_target_lapse.csv", index=False)

    subsection("Value counts - key categorical fields (cleaned)")
    for c in ["product_category", "distribution_channel", "policy_status", "claim_status"]:
        print(f"\n  {c}:")
        print(df[c].value_counts(dropna=False).to_string())

    return desc, corr, cat_target_df


# =========================================================================
# 5. VISUALISATIONS
# =========================================================================
def save_fig(fig, outdir, subfolder, name):
    path = outdir / "figures" / subfolder / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    print(f"  saved -> {path.relative_to(outdir.parent)}")


def visualisations(df, outdir):
    section("5. VISUALISATIONS")

    # ---------------------------------------------------------------- A. UNIVARIATE
    subsection("A. Univariate")

    # A1. Histogram + KDE grid for key numeric variables
    fig, axes = plt.subplots(2, 3, figsize=(16, 9))
    num_vars = [("age", "Age (years)"), ("annual_income_sgd", "Annual income (SGD)"),
                ("annual_premium_sgd", "Annual premium (SGD)"),
                ("sum_assured_sgd", "Sum assured (SGD)"),
                ("customer_sentiment_score", "Customer sentiment score"),
                ("nps_score", "NPS score")]
    for ax, (col, label) in zip(axes.flat, num_vars):
        sns.histplot(df[col].dropna(), kde=True, ax=ax, color=PALETTE[0])
        ax.set_title(label)
        ax.set_xlabel("")
        if col in ("annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd"):
            ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1000:.0f}k"))
    fig.suptitle("Distribution of key numeric variables (histogram + KDE)", fontsize=14)
    fig.tight_layout()
    save_fig(fig, outdir, "univariate", "01_histograms_kde")

    # A2. Box plots (outlier visibility)
    fig, axes = plt.subplots(1, 4, figsize=(16, 5))
    box_vars = [("annual_premium_sgd", "Annual premium (SGD)"),
                ("claim_amount_sgd", "Claim amount (SGD)"),
                ("age", "Age (years)"),
                ("sum_assured_sgd", "Sum assured (SGD)")]
    for ax, (col, label) in zip(axes.flat, box_vars):
        sns.boxplot(y=df[col].dropna(), ax=ax, color=PALETTE[1])
        ax.set_title(label)
        ax.set_ylabel("")
    fig.suptitle("Box plots - spread & outliers", fontsize=14)
    fig.tight_layout()
    save_fig(fig, outdir, "univariate", "02_boxplots")

    # A3. Bar chart - product category / distribution channel volumes
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    df["product_category"].value_counts(dropna=False).plot(kind="bar", ax=axes[0], color=PALETTE[0])
    axes[0].set_title("Policy count by product category")
    axes[0].set_xlabel("")
    axes[0].tick_params(axis="x", rotation=40)
    df["distribution_channel"].value_counts(dropna=False).plot(kind="bar", ax=axes[1], color=PALETTE[2])
    axes[1].set_title("Policy count by distribution channel")
    axes[1].set_xlabel("")
    axes[1].tick_params(axis="x", rotation=40)
    fig.tight_layout()
    save_fig(fig, outdir, "univariate", "03_bar_product_channel")

    # A4. Count plots - policy status, marital status, gender, income band
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    sns.countplot(y=df["policy_status"], order=df["policy_status"].value_counts().index,
                  ax=axes[0, 0], color=PALETTE[0])
    axes[0, 0].set_title("Policy status")
    sns.countplot(x=df["marital_status"], ax=axes[0, 1], color=PALETTE[1])
    axes[0, 1].set_title("Marital status")
    sns.countplot(x=df["gender"], ax=axes[1, 0], color=PALETTE[2])
    axes[1, 0].set_title("Gender (cleaned)")
    sns.countplot(y=df["income_band"], order=df["income_band"].value_counts().index,
                  ax=axes[1, 1], color=PALETTE[4])
    axes[1, 1].set_title("Income band")
    fig.tight_layout()
    save_fig(fig, outdir, "univariate", "04_countplots")

    # ---------------------------------------------------------------- B. BIVARIATE
    subsection("B. Bivariate")

    # B1. Scatter plots
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    samp = df.sample(min(4000, len(df)), random_state=42)
    sns.scatterplot(data=samp, x="annual_income_sgd", y="annual_premium_sgd",
                     alpha=0.35, s=15, ax=axes[0], color=PALETTE[0])
    axes[0].set_title("Income vs. Annual premium")
    sns.scatterplot(data=samp, x="age", y="annual_premium_sgd",
                     alpha=0.35, s=15, ax=axes[1], color=PALETTE[1])
    axes[1].set_title("Age vs. Annual premium")
    sns.scatterplot(data=samp, x="sum_assured_sgd", y="ape_sgd",
                     alpha=0.35, s=15, ax=axes[2], color=PALETTE[2])
    axes[2].set_title("Sum assured vs. APE")
    fig.tight_layout()
    save_fig(fig, outdir, "bivariate", "01_scatterplots")

    # B2. Box plots by category
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.5))
    order1 = df.groupby("product_category", observed=True)["annual_premium_sgd"].median().sort_values(ascending=False).index
    sns.boxplot(data=df, x="product_category", y="annual_premium_sgd", order=order1,
                ax=axes[0], showfliers=False, color=PALETTE[0])
    axes[0].set_title("Annual premium by product category")
    axes[0].tick_params(axis="x", rotation=30)
    order2 = ["Approved", "Partially Approved", "Pending", "Declined", "Withdrawn"]
    order2 = [o for o in order2 if o in df["claim_status"].unique()]
    sns.boxplot(data=df, x="claim_status", y="claim_amount_sgd", order=order2,
                ax=axes[1], showfliers=False, color=PALETTE[1])
    axes[1].set_title("Claim amount by claim status")
    axes[1].tick_params(axis="x", rotation=30)
    sns.boxplot(data=df, x="lapse_flag", y="customer_sentiment_score",
                ax=axes[2], color=PALETTE[2])
    axes[2].set_title("Customer sentiment by lapse outcome")
    axes[2].set_xticklabels(["Retained", "Lapsed"])
    fig.tight_layout()
    save_fig(fig, outdir, "bivariate", "02_boxplots_by_category")

    # B3. Line plot - policies quoted per month + lapse rate over time
    ts = df.dropna(subset=["quote_date"]).copy()
    ts["quote_month"] = ts["quote_date"].dt.to_period("M").dt.to_timestamp()
    monthly = ts.groupby("quote_month").agg(n_policies=("record_id", "count"),
                                             lapse_rate=("lapse_flag", "mean"))
    fig, ax1 = plt.subplots(figsize=(14, 5.5))
    ax1.plot(monthly.index, monthly["n_policies"], color=PALETTE[0], marker="o", markersize=3)
    ax1.set_ylabel("Policies quoted", color=PALETTE[0])
    ax1.set_title("Monthly quote volume vs. lapse rate over time")
    ax2 = ax1.twinx()
    ax2.plot(monthly.index, monthly["lapse_rate"] * 100, color=PALETTE[2], marker="s", markersize=3)
    ax2.set_ylabel("Lapse rate (%)", color=PALETTE[2])
    ax2.grid(False)
    fig.tight_layout()
    save_fig(fig, outdir, "bivariate", "03_line_volume_lapse_over_time")

    # B4. Grouped bar chart - lapse rate by product category x channel
    pivot = df.pivot_table(index="product_category", columns="distribution_channel",
                            values="lapse_flag", aggfunc="mean") * 100
    fig, ax = plt.subplots(figsize=(14, 6))
    pivot.plot(kind="bar", ax=ax)
    ax.set_title("Lapse rate (%) by product category and distribution channel")
    ax.set_ylabel("Lapse rate (%)")
    ax.set_xlabel("")
    ax.tick_params(axis="x", rotation=30)
    ax.legend(title="Channel", bbox_to_anchor=(1.02, 1), loc="upper left")
    fig.tight_layout()
    save_fig(fig, outdir, "bivariate", "04_grouped_bar_lapse_by_product_channel")

    # ---------------------------------------------------------------- C. MULTIVARIATE
    subsection("C. Multivariate")

    numeric_cols = ["age", "annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd",
                     "ape_sgd", "nbp_margin_pct", "months_inforce", "customer_sentiment_score",
                     "nps_score", "claim_amount_sgd", "claim_tat_days", "fraud_risk_score"]
    corr = df[numeric_cols].corr(numeric_only=True)
    fig, ax = plt.subplots(figsize=(11, 9))
    sns.heatmap(corr, annot=True, fmt=".2f", cmap="RdBu_r", center=0, ax=ax,
                cbar_kws={"shrink": 0.8}, annot_kws={"size": 8})
    ax.set_title("Correlation heatmap - numeric variables")
    fig.tight_layout()
    save_fig(fig, outdir, "multivariate", "01_correlation_heatmap")

    # C2. Pair plot / scatter matrix (subset, coloured by lapse outcome)
    pair_cols = ["age", "annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd"]
    pp_df = df[pair_cols + ["lapse_flag"]].dropna().sample(
        min(3000, df[pair_cols].dropna().shape[0]), random_state=42)
    pp_df["Outcome"] = pp_df["lapse_flag"].map({True: "Lapsed", False: "Retained"})
    g = sns.pairplot(pp_df, vars=pair_cols, hue="Outcome",
                      palette={"Retained": PALETTE[2], "Lapsed": PALETTE[1]},
                      plot_kws={"alpha": 0.4, "s": 12}, diag_kind="kde", height=2.3)
    g.fig.suptitle("Pairwise relationships - core numeric variables by lapse outcome", y=1.02)
    g.fig.savefig(outdir / "figures" / "multivariate" / "02_pairplot.png", bbox_inches="tight")
    plt.close(g.fig)
    print(f"  saved -> figures/multivariate/02_pairplot.png")

    # C3. Pivot table heatmap - avg annual premium by product category x income band
    piv2 = df.pivot_table(index="income_band", columns="product_category",
                           values="annual_premium_sgd", aggfunc="mean")
    band_order = ["Below 50k", "50k-100k", "100k-200k", "200k-400k", "Above 400k"]
    piv2 = piv2.reindex([b for b in band_order if b in piv2.index])
    fig, ax = plt.subplots(figsize=(11, 6))
    sns.heatmap(piv2, annot=True, fmt=".0f", cmap="rocket_r", ax=ax, cbar_kws={"label": "Avg annual premium (SGD)"})
    ax.set_title("Average annual premium (SGD): income band x product category")
    fig.tight_layout()
    save_fig(fig, outdir, "multivariate", "03_pivot_heatmap_premium")

    # ---------------------------------------------------------------- D. MISSING DATA
    subsection("D. Missing data")

    miss_cols = df.columns[df.isna().mean() > 0].tolist()
    miss_pct = df[miss_cols].isna().mean().sort_values(ascending=False) * 100

    fig, ax = plt.subplots(figsize=(9, max(6, len(miss_pct) * 0.28)))
    miss_pct.plot(kind="barh", ax=ax, color=PALETTE[0])
    ax.invert_yaxis()
    ax.set_xlabel("% missing")
    ax.set_title("Missingness by column (cleaned dataset)")
    fig.tight_layout()
    save_fig(fig, outdir, "missing_data", "01_missingness_bar")

    sample_cols = miss_pct.index.tolist()
    sample_rows = df[sample_cols].sample(min(600, len(df)), random_state=42)
    fig, ax = plt.subplots(figsize=(12, 8))
    sns.heatmap(sample_rows.isna(), cbar=False, cmap=["#EDEDED", PALETTE[1]], ax=ax)
    ax.set_title("Missingness matrix - sample of 600 rows (grey=present, red=missing)")
    ax.set_xlabel("")
    ax.set_ylabel("Row sample")
    ax.set_yticks([])
    fig.tight_layout()
    save_fig(fig, outdir, "missing_data", "02_missingness_matrix")


# =========================================================================
# MAIN
# =========================================================================
def main():
    parser = argparse.ArgumentParser(description="Prudential SG raw extract - EDA")
    parser.add_argument("csv_path", nargs="?", default="prudential_sg_raw.csv")
    parser.add_argument("--outdir", default="eda_outputs")
    args = parser.parse_args()

    from pathlib import Path
    outdir = Path(args.outdir)
    for sub in ["figures/univariate", "figures/bivariate", "figures/multivariate",
                "figures/missing_data", "reports"]:
        (outdir / sub).mkdir(parents=True, exist_ok=True)

    log_path = outdir / "reports" / "eda_console_log.txt"
    log_file = open(log_path, "w")
    real_stdout = sys.stdout
    sys.stdout = Tee(real_stdout, log_file)

    try:
        print(f"Run timestamp: {datetime.now().isoformat(timespec='seconds')}")
        print(f"Input file   : {args.csv_path}")

        df_raw = pd.read_csv(args.csv_path, low_memory=False)

        structural_exploration(df_raw)
        data_quality_exploration(df_raw)
        df_clean = clean_pipeline(df_raw)
        desc, corr, cat_target_df = statistical_exploration(df_clean, outdir)
        visualisations(df_clean, outdir)

        df_clean.to_csv(outdir / "cleaned_dataset.csv", index=False)
        print(f"\nCleaned dataset saved -> {outdir/'cleaned_dataset.csv'} "
              f"({df_clean.shape[0]:,} rows x {df_clean.shape[1]:,} cols)")

        # ---- key metrics json (for downstream reporting) ---------------------
        key_metrics = {
            "n_rows_raw": int(len(df_raw)),
            "n_cols_raw": int(df_raw.shape[1]),
            "n_exact_duplicates": int(df_raw.duplicated(keep="first").sum()),
            "n_fuzzy_duplicates": int(len(df_raw) - int(df_raw.duplicated(keep="first").sum())
                                       - len(df_clean)),
            "n_rows_clean": int(len(df_clean)),
            "lapse_rate_pct": float(df_clean["lapse_flag"].mean() * 100),
            "claim_rate_pct": float(df_clean["claim_occurred"].mean() * 100),
            "claim_decline_rate_pct": float(
                (df_clean["claim_status"] == "Declined").sum()
                / df_clean["claim_status"].notna().sum() * 100),
            "median_annual_premium_sgd": float(df_clean["annual_premium_sgd"].median()),
            "median_annual_income_sgd": float(df_clean["annual_income_sgd"].median()),
            "avg_customer_sentiment": float(df_clean["customer_sentiment_score"].mean()),
            "avg_nps_score": float(df_clean["nps_score"].mean()),
        }
        with open(outdir / "reports" / "key_metrics.json", "w") as f:
            json.dump(key_metrics, f, indent=2)

        section("DONE")
        print(f"All figures written under {outdir/'figures'}")
        print(f"All summary tables written under {outdir/'reports'}")
        print("Key metrics:")
        print(json.dumps(key_metrics, indent=2))

    finally:
        sys.stdout = real_stdout
        log_file.close()
        print(f"\nFull run log saved to: {log_path}")


if __name__ == "__main__":
    main()
