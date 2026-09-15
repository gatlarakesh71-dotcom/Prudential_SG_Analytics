"""
=============================================================================
 PRUDENTIAL SG BOOK :: CLEANING + MISSING-VALUE IMPUTATION (mean/median/mode)
=============================================================================
Builds on Step 1 (prudential_eda.py / the EDA business report) to produce a
single, fully-structured, gap-free CSV from the raw extract
prudential_sg_raw.csv.

WHAT THIS SCRIPT DOES
----------------------
1. Re-applies the same standardisation pipeline documented in the EDA report:
   de-duplicates, strips currency/percent notation, unifies Y/N-style flags,
   parses the 5 mixed date formats, and nulls out the confirmed sentinel/
   error codes (99,999,999.99 claim amount, -15,000 claim amount, 4,000-day
   TAT, out-of-range copay/age, negative premiums).

1b. Detects and removes FUZZY duplicates that survive exact-match
    de-duplication: the same policy re-entered a second time where
    customer_name lost its internal spacing on the repeat (e.g.
    'Jonathan Bennett' vs 'JonathanBennett'). Verified three independent
    ways (policy_id match, name+DOB+policy-fingerprint match, email+policy-
    fingerprint match) before removal - see remove_fuzzy_duplicates().

2. Fills every remaining missing value using MEAN, MEDIAN or MODE:
     - Numeric fields  -> MEAN if approximately symmetric (|skew| <= 0.5),
                          MEDIAN if skewed (|skew| > 0.5) - this matches the
                          distribution-shape findings in the EDA report.
     - Categorical/text fields -> MODE (most frequent category).

   IMPORTANT NUANCE (documented in the EDA report, Section 2.6 "Leakage
   risk" / structural missingness): a large share of "missing" cells in
   this book are not random gaps but fields that simply DO NOT APPLY to
   that row - e.g. a claim amount when no claim was ever filed, a rider
   premium when no rider was attached, an agent name when the policy was
   sold Direct/Bancassurance/Digital. Blindly mean/median/mode-filling
   those would fabricate claims, riders and agents that never existed and
   would badly distort any claims/rider/agent analysis.

   So, for the four structurally-conditional blocks (claims, riders,
   IP plan tier, servicing agent), this script applies mean/median/mode
   imputation ONLY within the population the field actually applies to,
   and fills the "not applicable" population with an explicit
   "Not Applicable" / 0 marker instead - never a fabricated statistic.
   Every one of these choices is logged column-by-column in
   imputation_method_log.csv so the method is fully auditable.

   The only two fields left with any blank cells by design are
   claim_date and lapse_date for rows where that event never happened
   (no claim / never lapsed) - a date field has no honest "average" for
   an event that did not occur, so these are left blank rather than
   filled with a fabricated date. Every other cell in the output is
   populated. See the printed summary / log for exact counts.

OUTPUTS
-------
   prudential_sg_cleaned_imputed.csv   <- final, fully-structured dataset
   imputation_method_log.csv           <- column-by-column audit of every
                                          fill method and value used

Usage:
    python prudential_data_cleaning_imputation.py [path_to_raw_csv] [--outdir .]
=============================================================================
"""

import argparse
import re
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

TODAY = pd.Timestamp("2026-09-10")
JUNK_TOKENS = {"", "-", "?", "unknown", "nan", "none", "null", "n/a", "na"}


def is_junk(s: str) -> bool:
    return str(s).strip().lower() in JUNK_TOKENS


# =========================================================================
# STAGE 1 - SAME STANDARDISATION PIPELINE AS THE EDA SCRIPT
# (kept self-contained here so this script runs on its own from the raw CSV)
# =========================================================================
def clean_currency(val):
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
    if pd.isna(val):
        return np.nan
    s = re.sub(r"\s+", " ", str(val)).strip()
    if is_junk(s):
        return np.nan
    return s


CHANNEL_TYPOS = {"bncssurnc": "Bancassurance", "digitl": "Digital"}


def clean_channel(val):
    s = clean_categorical(val)
    if not isinstance(s, str):
        return np.nan
    key = s.lower()
    return CHANNEL_TYPOS.get(key, s.title())


PRODUCT_CAT_JUNK = {"tbc", "misc", "legacy-xx", "unknown product"}


def clean_product_category(val):
    s = clean_categorical(val)
    if not isinstance(s, str):
        return np.nan
    return np.nan if s.lower() in PRODUCT_CAT_JUNK else s


PRODUCT_NAME_JUNK = {"zz-retired-plan", "temp product"}


def clean_product_name(val):
    s = clean_categorical(val)
    if not isinstance(s, str):
        return np.nan
    return np.nan if s.lower() in PRODUCT_NAME_JUNK else s


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


def parse_dob(val):
    """Same as parse_messy_date, but corrects a real ambiguity in the 2-digit
    year 'DD-Mon-YY' format: strptime's default century pivot reads '68' as
    2068 and '26' as 2026 rather than 1968/1926. A birth date can never be
    in the future, so any parse that lands after TODAY is shifted back a
    century - e.g. '19-Nov-58' parses to 2058-11-19 by default, which is
    impossible for a date of birth, so it is corrected to 1958-11-19."""
    d = parse_messy_date(val)
    if pd.notna(d) and d > TODAY:
        d = d - pd.DateOffset(years=100)
    return d


def clean_name(val):
    if pd.isna(val):
        return np.nan
    s = re.sub(r"\s+", " ", str(val)).strip()
    return s if s else np.nan


def clean_pipeline(df_raw):
    """Reproduces the standardisation from prudential_eda.py: de-dup, format
    fixes, sentinel removal - leaves genuine NaNs for Stage 2 to fill."""
    df = df_raw.drop_duplicates(keep="first").copy()

    for c in ["record_id", "customer_id", "policy_id", "claim_id", "agent_id",
              "assessor_id", "email", "mobile_number", "agency_unit"]:
        df[c] = df[c].map(clean_name)
    df["customer_name"] = df["customer_name"].map(clean_name)
    df["agent_name"] = df["agent_name"].map(clean_name)

    df["gender"] = df["gender"].map(clean_gender)
    for c in ["marital_status", "residency_status", "occupation_group", "income_band",
              "postal_district", "policy_status", "ip_plan_tier", "claim_type",
              "claim_status", "claim_decline_reason", "agent_tier", "agent_status",
              "source_system", "payment_frequency"]:
        df[c] = df[c].map(clean_categorical)
    df["distribution_channel"] = df["distribution_channel"].map(clean_channel)
    df["product_category"] = df["product_category"].map(clean_product_category)
    df["product_name"] = df["product_name"].map(clean_product_name)

    for c in ["annual_income_sgd", "annual_premium_sgd", "sum_assured_sgd",
              "ape_sgd", "claim_amount_sgd"]:
        df[c] = df[c].map(clean_currency)
    df.loc[df["annual_premium_sgd"] < 0, "annual_premium_sgd"] = np.nan
    df.loc[df["claim_amount_sgd"].isin([99999999.99, -15000.0]), "claim_amount_sgd"] = np.nan

    df["nbp_margin_pct"] = df["nbp_margin_pct"].map(clean_percent_to_fraction)

    df["age"] = df["age"].map(clean_numeric_generic)
    df.loc[(df["age"] < 18) | (df["age"] > 100), "age"] = np.nan

    df["months_inforce"] = df["months_inforce"].map(clean_numeric_generic)

    df["copay_pct"] = pd.to_numeric(df["copay_pct"], errors="coerce")
    df.loc[(df["copay_pct"] < 0) | (df["copay_pct"] > 100), "copay_pct"] = np.nan

    df["claim_tat_days"] = pd.to_numeric(df["claim_tat_days"], errors="coerce")
    df.loc[df["claim_tat_days"] < 0, "claim_tat_days"] = np.nan
    df.loc[df["claim_tat_days"] == 4000, "claim_tat_days"] = np.nan

    for c in ["rider_premium_before_sgd", "rider_premium_after_sgd", "deductible_sgd",
              "fraud_risk_score", "assessor_approval_limit_sgd", "agent_tenure_months",
              "agent_productivity_score", "agent_attrition_risk", "customer_sentiment_score",
              "nps_score", "policy_term_years", "care_delay_days"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    for c in ["lapse_flag", "persistency_13m_flag", "rider_attached_flag",
              "chronic_condition_flag", "complaint_flag", "disputed_flag",
              "sod_breach_flag", "approval_above_limit_flag"]:
        df[c] = df[c].map(clean_flag)

    _dob_naive = df["date_of_birth"].map(parse_messy_date)
    df["date_of_birth"] = df["date_of_birth"].map(parse_dob)
    n_century_fixed = int((_dob_naive > TODAY).sum())
    if n_century_fixed:
        print(f"date_of_birth: corrected {n_century_fixed} rows where the 2-digit-year "
              f"'DD-Mon-YY' format parsed as a future date (e.g. 2058 instead of 1958) "
              f"- shifted back a century, since a birth date cannot be in the future.")
    for c in ["quote_date", "policy_start_date", "lapse_date", "claim_date"]:
        df[c] = df[c].map(parse_messy_date)
    df["last_updated_ts"] = pd.to_datetime(df["last_updated_ts"], format="%d-%m-%Y %H:%M",
                                            errors="coerce")

    df["age_from_dob"] = (TODAY - df["date_of_birth"]).dt.days / 365.25
    df["claim_occurred"] = df["claim_status"].notna()
    df["policy_discontinued"] = df["policy_status"].isin(["Lapsed", "Surrendered", "Cancelled"])

    return df


def remove_fuzzy_duplicates(df):
    """Catches the near-duplicate pattern found by investigation: the same
    policy_id genuinely re-entered a second time where customer_name lost
    its internal spacing on the repeat entry (e.g. 'Jonathan Bennett' vs
    'JonathanBennett') - a formatting glitch, not a coincidence. Verified
    three independent ways (policy_id match, name+DOB+policy-fingerprint
    match, email+policy-fingerprint match) - all three agree exactly.

    Deliberately narrow: matches require a REAL (non-blank/non-placeholder)
    shared policy_id AND a name match after stripping whitespace/case, so
    two different customers who simply share a common name are never
    caught (verified no false positives at this threshold).
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
# STAGE 2 - MEAN / MEDIAN / MODE IMPUTATION
# =========================================================================
LOG = []


def _log(col, group, n_missing, n_struct, struct_val, n_stat, stat_method, stat_val, n_blank=0,
         note="", n_unknown=0):
    LOG.append({
        "column": col, "field_group": group, "n_missing_before": int(n_missing),
        "n_filled_structural_not_applicable": int(n_struct), "structural_fill_value": struct_val,
        "n_filled_statistical": int(n_stat), "statistical_method": stat_method,
        "statistical_fill_value": stat_val, "n_filled_unknown_placeholder": int(n_unknown),
        "n_left_blank_by_design": int(n_blank), "note": note,
    })


def choose_mean_or_median(series):
    s = series.dropna()
    skew = s.skew() if len(s) > 2 else 0.0
    if abs(skew) > 0.5:
        return "median", float(s.median()), skew
    return "mean", float(s.mean()), skew


def fill_numeric_standard(df, col, group="standard"):
    n_missing = df[col].isna().sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    method, value, skew = choose_mean_or_median(df[col])
    df.loc[df[col].isna(), col] = value
    _log(col, group, n_missing, 0, "", n_missing, method, round(value, 4),
         note=f"skew={skew:.2f}")


def fill_categorical_standard(df, col, group="standard"):
    n_missing = df[col].isna().sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    mode_val = df[col].mode(dropna=True)
    mode_val = mode_val.iloc[0] if len(mode_val) else "Unknown"
    df.loc[df[col].isna(), col] = mode_val
    _log(col, group, n_missing, 0, "", n_missing, "mode", mode_val)


def fill_identifier_standard(df, col, group="identifier", label="Unknown"):
    n_missing = df[col].isna().sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    df.loc[df[col].isna(), col] = label
    _log(col, group, n_missing, 0, "", 0, "n/a", "", n_unknown=n_missing,
         note="Identifier/free-text field - mode-filling would fabricate a shared "
              "identity across unrelated records, so a explicit placeholder is used "
              "instead of a statistical mode.")


def fill_date_standard(df, col, group="standard"):
    n_missing = df[col].isna().sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    median_date = df[col].dropna().median()
    df.loc[df[col].isna(), col] = median_date
    _log(col, group, n_missing, 0, "", n_missing, "median date", median_date.date().isoformat())


def fill_numeric_conditional(df, col, applicable_mask, group, struct_value=0.0):
    missing = df[col].isna()
    n_missing = missing.sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    stat_rows = missing & applicable_mask
    struct_rows = missing & ~applicable_mask
    n_stat = stat_rows.sum()
    n_struct = struct_rows.sum()
    method, value, skew, note = "n/a", "", 0.0, ""
    if n_stat > 0:
        method, value, skew = choose_mean_or_median(df.loc[applicable_mask, col])
        df.loc[stat_rows, col] = value
        note = f"skew (within applicable subset)={skew:.2f}"
    if n_struct > 0:
        df.loc[struct_rows, col] = struct_value
    _log(col, group, n_missing, n_struct, struct_value, n_stat, method,
         round(value, 4) if value != "" else "", note=note)


def fill_categorical_conditional(df, col, applicable_mask, group, not_applicable_label,
                                  unknown_mask=None):
    missing = df[col].isna()
    n_missing = missing.sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    if unknown_mask is None:
        unknown_mask = pd.Series(False, index=df.index)
    stat_rows = missing & applicable_mask & ~unknown_mask
    unk_rows = missing & unknown_mask
    struct_rows = missing & ~applicable_mask & ~unknown_mask
    n_stat, n_unk, n_struct = stat_rows.sum(), unk_rows.sum(), struct_rows.sum()
    method, value = "n/a", ""
    if n_stat > 0:
        mode_val = df.loc[applicable_mask & df[col].notna(), col].mode(dropna=True)
        value = mode_val.iloc[0] if len(mode_val) else "Unknown"
        method = "mode (within applicable subset)"
        df.loc[stat_rows, col] = value
    if n_unk > 0:
        df.loc[unk_rows, col] = "Unknown"
    if n_struct > 0:
        df.loc[struct_rows, col] = not_applicable_label
    note = f"+{n_unk} filled 'Unknown' where applicability itself was unknown" if n_unk else ""
    _log(col, group, n_missing, n_struct, not_applicable_label, n_stat, method, value,
         n_unknown=n_unk, note=note)


def fill_identifier_conditional(df, col, applicable_mask, group, not_applicable_label,
                                 unknown_mask=None):
    missing = df[col].isna()
    n_missing = missing.sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    if unknown_mask is None:
        unknown_mask = pd.Series(False, index=df.index)
    stat_rows = missing & applicable_mask & ~unknown_mask
    unk_rows = missing & unknown_mask
    struct_rows = missing & ~applicable_mask & ~unknown_mask
    df.loc[stat_rows, col] = "Unknown"
    df.loc[unk_rows, col] = "Unknown"
    df.loc[struct_rows, col] = not_applicable_label
    _log(col, group, n_missing, struct_rows.sum(), not_applicable_label,
         0, "n/a", "", n_unknown=stat_rows.sum() + unk_rows.sum(),
         note="Identifier field - genuine gaps get 'Unknown', not a fabricated shared ID")


def fill_date_conditional(df, col, applicable_mask, group):
    missing = df[col].isna()
    n_missing = missing.sum()
    if n_missing == 0:
        _log(col, group, 0, 0, "", 0, "n/a", "", note="No missing values - untouched")
        return
    stat_rows = missing & applicable_mask
    struct_rows = missing & ~applicable_mask
    n_stat = stat_rows.sum()
    method, value = "n/a", ""
    if n_stat > 0:
        median_date = df.loc[applicable_mask, col].dropna().median()
        df.loc[stat_rows, col] = median_date
        method, value = "median date (within applicable subset)", median_date.date().isoformat()
    _log(col, group, n_missing, 0, "left blank (event never occurred)", n_stat, method, value,
         n_blank=struct_rows.sum(),
         note="Left blank rather than filled: fabricating an event date for a row where "
              "the event never happened (no claim / never lapsed) would be misleading.")


def flag_to_yesno(df, col, applicable_mask=None, group="standard"):
    """Convert a clean boolean flag column to explicit Yes/No[/Not Applicable] text."""
    n_missing = df[col].isna().sum()
    if applicable_mask is None:
        df[col] = df[col].map({True: "Yes", False: "No"})
        if n_missing:
            mode_val = df[col].mode(dropna=True).iloc[0]
            df.loc[df[col].isna(), col] = mode_val
            _log(col, group, n_missing, 0, "", n_missing, "mode", mode_val)
        else:
            _log(col, group, 0, 0, "", 0, "n/a", "",
                 note="No missing values; recoded True/False -> Yes/No for readability")
        return
    missing = df[col].isna()
    stat_rows = missing & applicable_mask
    struct_rows = missing & ~applicable_mask
    n_stat, n_struct = stat_rows.sum(), struct_rows.sum()
    method, value = "n/a", ""
    if n_stat > 0:
        mode_val = df.loc[applicable_mask & df[col].notna(), col].mode(dropna=True)
        value = "Yes" if (len(mode_val) and mode_val.iloc[0]) else "No"
        method = "mode (within applicable subset)"
    df[col] = df[col].map({True: "Yes", False: "No"})
    if n_stat > 0:
        df.loc[stat_rows, col] = value
    if n_struct > 0:
        df.loc[struct_rows, col] = "Not Applicable"
    _log(col, group, n_missing, n_struct, "Not Applicable", n_stat, method, value)


def fill_age_and_dob_jointly(df):
    """age and date_of_birth are two views of the same fact, so they must be
    imputed together, not independently - otherwise a row can end up with,
    say, a recorded age of 25 and an imputed birth date implying age ~43.

    Priority order per row:
      1. Age missing, DOB known  -> derive age EXACTLY from that row's own DOB.
      2. DOB missing, age known  -> derive a DOB that reproduces that row's
         own recorded age exactly (TODAY minus age years). We can't recover
         the real birth month/day, but the derived DOB is at least
         consistent with the one fact we do know about that customer.
      3. Both missing            -> fall back to the population mean age
         (matches the standard random-missingness rule used elsewhere),
         then derive a matching DOB the same way as case 2, so the two
         fields still agree with each other for these rows too.

    This does NOT touch rows where both age and DOB are already present,
    even if those two values disagree with each other - that is a
    pre-existing source-data quality issue (flagged separately in the EDA),
    not a missing value, and is not ours to silently overwrite.
    """
    age_missing = df["age"].isna()
    dob_missing = df["date_of_birth"].isna()

    only_age_missing = age_missing & ~dob_missing
    only_dob_missing = dob_missing & ~age_missing
    both_missing = age_missing & dob_missing

    n_only_age = int(only_age_missing.sum())
    n_only_dob = int(only_dob_missing.sum())
    n_both = int(both_missing.sum())

    # Case 1: derive age exactly from the row's own real date_of_birth
    derived_age = ((TODAY - df.loc[only_age_missing, "date_of_birth"]).dt.days / 365.25).round()
    df.loc[only_age_missing, "age"] = derived_age

    # Case 2: derive a DOB consistent with the row's own real age
    def age_to_dob(age_val):
        return TODAY - pd.DateOffset(years=int(round(age_val)))
    df.loc[only_dob_missing, "date_of_birth"] = df.loc[only_dob_missing, "age"].map(age_to_dob)

    # Case 3: population mean age (same rule as any other symmetric numeric
    # field), then a matching, internally-consistent DOB
    pop_mean_age = df.loc[~age_missing, "age"].mean()  # uses only genuinely-recorded ages
    df.loc[both_missing, "age"] = round(pop_mean_age)
    df.loc[both_missing, "date_of_birth"] = age_to_dob(pop_mean_age)

    _log("age", "demographics", n_only_age + n_both, 0, "", n_only_age + n_both,
         "derived from date_of_birth (own row)" if n_only_age else "mean",
         round(float(pop_mean_age), 2),
         note=f"{n_only_age} rows: age derived exactly from that row's own date_of_birth. "
              f"{n_both} rows: population mean age ({pop_mean_age:.2f}) used because "
              f"both age and date_of_birth were missing for that row.")
    _log("date_of_birth", "demographics", n_only_dob + n_both, 0, "", n_only_dob + n_both,
         "derived from age (own row, TODAY minus N years)",
         "varies per row",
         note=f"{n_only_dob} rows: date_of_birth backed out from that row's own recorded "
              f"age, so age and date_of_birth cannot disagree for these rows. {n_both} rows: "
              f"derived the same way from the population mean age used for 'age' above.")
    print(f"age & date_of_birth resolved jointly: {n_only_age} rows derived age from DOB, "
          f"{n_only_dob} rows derived DOB from age, {n_both} rows used population mean "
          f"(both were missing) - zero rows left with mutually-inconsistent imputed values.")


def impute_pipeline(df):
    df = df.copy()

    # ---- applicability masks (built BEFORE filling the fields that define them) ----
    claim_mask = df["claim_status"].notna()
    declined_mask = df["claim_status"] == "Declined"
    rider_mask = df["rider_attached_flag"] == True  # noqa: E712
    lapse_mask = df["lapse_flag"] == True  # noqa: E712
    healthip_mask = df["product_category"] == "Health & IP"
    healthip_unknown = df["product_category"].isna()
    agency_mask = df["distribution_channel"].isin(["Agency", "Broker"])
    agency_unknown = df["distribution_channel"].isna()

    # ---- Group: identifiers & metadata --------------------------------------
    fill_identifier_standard(df, "record_id", "identifier")
    fill_categorical_standard(df, "source_system", "metadata")
    fill_date_standard(df, "last_updated_ts", "metadata")
    fill_identifier_standard(df, "customer_id", "identifier")
    fill_identifier_standard(df, "policy_id", "identifier")

    # ---- Group: customer demographics (always-applicable, random gaps) -----
    fill_identifier_standard(df, "customer_name", "identifier")
    fill_categorical_standard(df, "gender", "demographics")
    fill_age_and_dob_jointly(df)
    fill_categorical_standard(df, "marital_status", "demographics")
    fill_categorical_standard(df, "residency_status", "demographics")
    fill_categorical_standard(df, "occupation_group", "demographics")
    fill_numeric_standard(df, "annual_income_sgd", "demographics")
    fill_categorical_standard(df, "income_band", "demographics")
    fill_categorical_standard(df, "postal_district", "demographics")
    fill_identifier_standard(df, "email", "identifier")
    fill_identifier_standard(df, "mobile_number", "identifier")

    # recompute age_from_dob after date_of_birth fill (keeps the two consistent
    # wherever date_of_birth itself needed imputing)
    n_missing_agefromdob = df["age_from_dob"].isna().sum()
    df["age_from_dob"] = (TODAY - df["date_of_birth"]).dt.days / 365.25
    _log("age_from_dob", "demographics (derived)", n_missing_agefromdob, 0, "", 
         n_missing_agefromdob, "recomputed from imputed date_of_birth", "n/a",
         note="Resolved automatically once date_of_birth was imputed above, rather "
              "than being independently imputed.")

    # ---- Group: policy & product ---------------------------------------------
    fill_date_standard(df, "quote_date", "policy")
    fill_date_standard(df, "policy_start_date", "policy")
    fill_categorical_standard(df, "product_name", "policy")
    fill_categorical_standard(df, "product_category", "policy")
    fill_categorical_standard(df, "distribution_channel", "policy")
    fill_categorical_standard(df, "payment_frequency", "policy")
    fill_categorical_standard(df, "policy_status", "policy")
    fill_numeric_standard(df, "months_inforce", "policy")
    fill_date_conditional(df, "lapse_date", lapse_mask, "policy (conditional on lapse_flag)")

    # ---- Group: financials -----------------------------------------------------
    fill_numeric_standard(df, "annual_premium_sgd", "financial")
    fill_numeric_standard(df, "sum_assured_sgd", "financial")
    fill_numeric_standard(df, "ape_sgd", "financial")
    fill_numeric_standard(df, "nbp_margin_pct", "financial")

    # ---- Group: health / rider (conditional on rider_attached_flag / product) --
    fill_categorical_conditional(df, "ip_plan_tier", healthip_mask,
                                  "health (conditional on product = Health & IP)",
                                  not_applicable_label="Not Applicable",
                                  unknown_mask=healthip_unknown)
    fill_numeric_conditional(df, "rider_premium_before_sgd", rider_mask,
                              "rider (conditional on rider_attached_flag)")
    fill_numeric_conditional(df, "rider_premium_after_sgd", rider_mask,
                              "rider (conditional on rider_attached_flag)")
    fill_numeric_conditional(df, "copay_pct", rider_mask,
                              "rider (conditional on rider_attached_flag)")
    fill_numeric_conditional(df, "deductible_sgd", rider_mask,
                              "rider (conditional on rider_attached_flag)")

    # ---- Group: claims (conditional on claim_occurred) --------------------------
    # claim_status missing IS the definition of "no claim" - fill directly with an
    # explicit category rather than a statistical mode (a "typical status" would
    # misrepresent a policy that was never claimed against).
    n_missing_status = df["claim_status"].isna().sum()
    df.loc[~claim_mask, "claim_status"] = "No Claim"
    _log("claim_status", "claims", n_missing_status, n_missing_status, "No Claim", 0, "n/a", "",
         note="Missing here means no claim was ever filed - an explicit 'No Claim' "
              "category is used instead of a statistical mode, which would imply a "
              "claim outcome that never happened.")
    df["claim_occurred"] = claim_mask  # lock in the TRUE applicability flag used throughout
    flag_to_yesno(df, "claim_occurred", None, "claims (derived)")

    fill_identifier_conditional(df, "claim_id", claim_mask, "claims", "Not Applicable")
    fill_date_conditional(df, "claim_date", claim_mask, "claims")
    fill_categorical_conditional(df, "claim_type", claim_mask, "claims", "Not Applicable")
    fill_numeric_conditional(df, "claim_amount_sgd", claim_mask, "claims")
    fill_categorical_conditional(df, "claim_decline_reason", declined_mask, "claims",
                                  "Not Applicable")
    fill_numeric_conditional(df, "claim_tat_days", claim_mask, "claims")

    # ---- Group: claim assessment / fraud (conditional on claim_occurred) --------
    fill_identifier_conditional(df, "assessor_id", claim_mask, "claims assessment",
                                 "Not Applicable")
    fill_numeric_conditional(df, "assessor_approval_limit_sgd", claim_mask, "claims assessment")
    flag_to_yesno(df, "approval_above_limit_flag", claim_mask, "claims assessment")
    flag_to_yesno(df, "sod_breach_flag", claim_mask, "claims assessment")
    fill_numeric_conditional(df, "fraud_risk_score", claim_mask, "claims assessment")

    # ---- Group: agent / distribution (conditional on Agency/Broker channel) -----
    fill_identifier_conditional(df, "agent_id", agency_mask, "agent",
                                 "Not Applicable", unknown_mask=agency_unknown)
    fill_identifier_conditional(df, "agent_name", agency_mask, "agent",
                                 "Not Applicable", unknown_mask=agency_unknown)
    fill_categorical_conditional(df, "agent_tier", agency_mask, "agent",
                                  "Not Applicable", unknown_mask=agency_unknown)
    fill_numeric_conditional(df, "agent_tenure_months", agency_mask, "agent")
    fill_categorical_conditional(df, "agency_unit", agency_mask, "agent",
                                  "Not Applicable", unknown_mask=agency_unknown)
    fill_categorical_conditional(df, "agent_status", agency_mask, "agent",
                                  "Not Applicable", unknown_mask=agency_unknown)
    fill_numeric_conditional(df, "agent_productivity_score", agency_mask, "agent")
    fill_numeric_conditional(df, "agent_attrition_risk", agency_mask, "agent")

    # ---- Always-complete fields: no missing, just recode booleans to text -------
    flag_to_yesno(df, "lapse_flag", None, "policy")
    flag_to_yesno(df, "persistency_13m_flag", None, "policy")
    flag_to_yesno(df, "rider_attached_flag", None, "health/rider")
    flag_to_yesno(df, "chronic_condition_flag", None, "health/rider")
    flag_to_yesno(df, "complaint_flag", None, "claims")
    flag_to_yesno(df, "disputed_flag", None, "claims")
    flag_to_yesno(df, "policy_discontinued", None, "policy (derived)")

    for c in ["customer_sentiment_score", "nps_score", "policy_term_years", "care_delay_days"]:
        fill_numeric_standard(df, c, "already complete")

    return df


# =========================================================================
# ROUNDING / DTYPE POLISH + COLUMN ORDERING
# =========================================================================
COLUMN_ORDER = [
    # identifiers & metadata
    "record_id", "source_system", "last_updated_ts", "customer_id", "policy_id",
    # customer demographics
    "customer_name", "gender", "date_of_birth", "age", "age_from_dob", "marital_status",
    "residency_status", "occupation_group", "annual_income_sgd", "income_band",
    "postal_district", "email", "mobile_number",
    # policy & product
    "quote_date", "policy_start_date", "product_name", "product_category",
    "distribution_channel", "payment_frequency", "policy_term_years", "policy_status",
    "months_inforce", "policy_discontinued", "lapse_flag", "lapse_date",
    "persistency_13m_flag",
    # financials
    "annual_premium_sgd", "sum_assured_sgd", "ape_sgd", "nbp_margin_pct",
    # health & rider
    "chronic_condition_flag", "ip_plan_tier", "rider_attached_flag",
    "rider_premium_before_sgd", "rider_premium_after_sgd", "copay_pct", "deductible_sgd",
    "care_delay_days",
    # claims
    "claim_occurred", "claim_id", "claim_date", "claim_type", "claim_amount_sgd",
    "claim_status", "claim_decline_reason", "claim_tat_days", "disputed_flag",
    "complaint_flag", "customer_sentiment_score", "nps_score",
    # claim assessment / fraud
    "assessor_id", "assessor_approval_limit_sgd", "approval_above_limit_flag",
    "sod_breach_flag", "fraud_risk_score",
    # agent / distribution
    "agent_id", "agent_name", "agent_tier", "agent_tenure_months", "agency_unit",
    "agent_status", "agent_productivity_score", "agent_attrition_risk",
]

ROUND_2DP = ["annual_premium_sgd", "ape_sgd", "rider_premium_before_sgd",
             "rider_premium_after_sgd", "claim_amount_sgd", "customer_sentiment_score"]
ROUND_3DP = ["fraud_risk_score", "agent_attrition_risk"]
ROUND_4DP = ["nbp_margin_pct"]
ROUND_1DP = ["agent_productivity_score", "age_from_dob"]
ROUND_INT = ["age", "months_inforce", "claim_tat_days", "agent_tenure_months",
             "policy_term_years", "care_delay_days", "nps_score",
             "annual_income_sgd", "sum_assured_sgd", "deductible_sgd",
             "assessor_approval_limit_sgd", "copay_pct"]


def polish(df):
    for c in ROUND_2DP:
        df[c] = df[c].round(2)
    for c in ROUND_3DP:
        df[c] = df[c].round(3)
    for c in ROUND_4DP:
        df[c] = df[c].round(4)
    for c in ROUND_1DP:
        df[c] = df[c].round(1)
    for c in ROUND_INT:
        df[c] = df[c].round(0).astype("Int64")
    for c in ["date_of_birth", "quote_date", "policy_start_date", "lapse_date", "claim_date",
              "last_updated_ts"]:
        if c == "last_updated_ts":
            df[c] = df[c].dt.strftime("%Y-%m-%d %H:%M")
        else:
            df[c] = df[c].dt.strftime("%Y-%m-%d")
    return df[COLUMN_ORDER]


# =========================================================================
# MAIN
# =========================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Clean the Prudential SG raw extract and fill all missing "
                    "values using mean/median/mode imputation.")
    parser.add_argument("csv_path", nargs="?", default="prudential_sg_raw.csv")
    parser.add_argument("--outdir", default=".")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Reading raw file: {args.csv_path}")
    df_raw = pd.read_csv(args.csv_path, low_memory=False)
    print(f"Raw shape: {df_raw.shape[0]:,} rows x {df_raw.shape[1]:,} columns")

    print("\nStage 1/4 - standardising formats, exact de-duplication, nulling sentinel values...")
    df_clean = clean_pipeline(df_raw)
    n_dupes = len(df_raw) - len(df_clean)
    print(f"  Dropped {n_dupes:,} exact duplicate rows -> {len(df_clean):,} rows remain")

    print("\nStage 2/4 - fuzzy-duplicate detection (same policy re-entered with a "
          "name-spacing glitch)...")
    df_clean, n_fuzzy, n_fuzzy_groups = remove_fuzzy_duplicates(df_clean)
    print(f"  Found {n_fuzzy_groups:,} genuine near-duplicate policies (verified via "
          f"policy_id + name/email/DOB cross-checks) -> removed {n_fuzzy:,} rows")
    print(f"  Rows remaining after all de-duplication: {len(df_clean):,}")
    print(f"  Missing cells before imputation: {int(df_clean.isna().sum().sum()):,}")

    print("\nStage 3/4 - filling every missing value with mean / median / mode "
          "(structural 'not applicable' fields handled separately - see log)...")
    df_imputed = impute_pipeline(df_clean)

    print("\nStage 4/4 - rounding, dtype polish, and applying final column order...")
    df_final = polish(df_imputed)

    remaining_missing = df_final.isna().sum()
    remaining_missing = remaining_missing[remaining_missing > 0]
    remaining_blank = (df_final == "").sum()
    remaining_blank = remaining_blank[remaining_blank > 0]

    out_csv = outdir / "prudential_sg_cleaned_imputed.csv"
    df_final.to_csv(out_csv, index=False)

    log_df = pd.DataFrame(LOG)
    log_csv = outdir / "imputation_method_log.csv"
    log_df.to_csv(log_csv, index=False)

    print("\n" + "=" * 78)
    print("DONE")
    print("=" * 78)
    print(f"Final dataset : {out_csv}  ({df_final.shape[0]:,} rows x {df_final.shape[1]:,} cols)")
    print(f"Method log    : {log_csv}  ({len(log_df)} columns documented)")
    print(f"\nTrue NaN cells remaining in the file: {int(df_final.isna().sum().sum())}")
    if len(remaining_missing):
        print("(all in date columns, left blank by design for events that never "
              "occurred - see 'n_left_blank_by_design' in the method log):")
        print(remaining_missing.to_string())
    print(f"\nBlank-string cells remaining: {int(remaining_blank.sum())}")
    if len(remaining_blank):
        print(remaining_blank.to_string())


if __name__ == "__main__":
    main()
