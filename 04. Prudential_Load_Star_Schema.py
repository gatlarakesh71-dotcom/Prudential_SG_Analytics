"""Load the cleaned CSV into the prudential SQL schema."""

import calendar
import os
import sys
from datetime import datetime

import pandas as pd
import pyodbc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(
    BASE_DIR,
    "02. prudential_sg_cleaned",
    "prudential_sg_cleaned_imputed.csv",
)
SCHEMA = "prudential"
DB_SERVER = os.getenv("PRUDENTIAL_DB_SERVER", r"localhost\RAKESHSQLEXPRESS")
DB_NAME = os.getenv("PRUDENTIAL_DB_NAME", "PrudentialDW")
DB_USER = os.getenv("PRUDENTIAL_DB_USER", "")
DB_PASSWORD = os.getenv("PRUDENTIAL_DB_PASSWORD", "")
ODBC_DRIVER = os.getenv("PRUDENTIAL_ODBC_DRIVER",
                        "ODBC Driver 18 for SQL Server")
USE_TRUSTED_CONNECTION = os.getenv(
    "PRUDENTIAL_TRUSTED_CONNECTION", "yes"
).lower() in {"yes", "true", "1"}
NULL_VALUES = {"", "Not Applicable", "NA", "N/A", "None", "null"}


def get_connection():
    if USE_TRUSTED_CONNECTION:
        connection_string = (
            f"DRIVER={{{ODBC_DRIVER}}};SERVER={DB_SERVER};DATABASE={DB_NAME};"
            "Trusted_Connection=yes;TrustServerCertificate=yes;"
        )
    else:
        if not DB_USER or not DB_PASSWORD:
            raise RuntimeError(
                "SQL authentication requires user and password.")
        connection_string = (
            f"DRIVER={{{ODBC_DRIVER}}};SERVER={DB_SERVER};DATABASE={DB_NAME};"
            f"UID={DB_USER};PWD={DB_PASSWORD};TrustServerCertificate=yes;"
        )
    return pyodbc.connect(connection_string, autocommit=False)


def text(value):
    if pd.isna(value) or str(value).strip() in NULL_VALUES:
        return None
    return str(value).strip()


def lookup_text(value):
    value = text(value)
    return None if value is None else value.casefold()


def usable_dimension_key(value):
    value = text(value)
    if value is None or value.casefold() in {"unknown", "not applicable", "-", "?"}:
        return None
    return value


def policy_business_key(record_id, policy_id):
    policy_id = usable_dimension_key(policy_id)
    if policy_id is not None:
        return policy_id
    record_id = text(record_id)
    if record_id is None:
        raise ValueError(
            "A policy row has neither a usable policy_id nor record_id.")
    return f"SYN-{record_id}"


def number(value):
    value = text(value)
    return None if value is None else float(value)


def integer(value):
    value = number(value)
    return None if value is None else int(value)


def bit(value):
    value = text(value)
    if value is None:
        return None
    if value.lower() in {"yes", "true", "1"}:
        return 1
    if value.lower() in {"no", "false", "0"}:
        return 0
    raise ValueError(f"Unsupported bit value: {value}")


def parsed_date(value):
    value = text(value)
    return None if value is None else datetime.strptime(value, "%d-%m-%Y").date()


def key(value):
    value = parsed_date(value)
    return None if value is None else int(value.strftime("%Y%m%d"))


def insert_many(cursor, sql, rows, chunk_size=5000):
    rows = list(rows)
    cursor.fast_executemany = True
    for start in range(0, len(rows), chunk_size):
        cursor.executemany(sql, rows[start:start + chunk_size])
    return len(rows)


def reset_load_tables(cursor):
    cursor.execute(f"DELETE FROM {SCHEMA}.fact_claim")
    cursor.execute(f"DELETE FROM {SCHEMA}.fact_policy_new_business")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_date")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_customer")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_agent")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_assessor")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_claim_type")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_product WHERE product_key <> 1")
    cursor.execute(f"DELETE FROM {SCHEMA}.dim_channel WHERE channel_key <> 1")


def load_dimensions(cursor, data):
    dates = set()
    for column in ("quote_date", "policy_start_date", "claim_date"):
        dates.update(parsed_date(value)
                     for value in data[column] if text(value))
    date_rows = []
    for value in sorted(dates):
        quarter = (value.month - 1) // 3 + 1
        date_rows.append(
            (
                int(value.strftime("%Y%m%d")), value, value.day,
                value.strftime("%A"), value.isoweekday(),
                value.isocalendar().week, value.month, value.strftime("%B"),
                quarter, f"Q{quarter}", value.year, value.strftime("%Y-%m"),
                int(value.day == calendar.monthrange(
                    value.year, value.month)[1]),
                int(value.month in {3, 6, 9, 12}
                    and value.day == calendar.monthrange(value.year, value.month)[1]),
                int(value.month == 12 and value.day == 31),
            )
        )
    insert_many(
        cursor,
        f"""INSERT INTO {SCHEMA}.dim_date
        (date_key, full_date, day_of_month, day_name, day_of_week, week_of_year,
         month_number, month_name, quarter_number, quarter_name, year_number,
         year_month, is_month_end, is_quarter_end, is_year_end)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        date_rows,
    )

    customers = []
    customer_data = data.assign(
        _customer_lookup=data.customer_id.map(lookup_text)
    ).drop_duplicates("_customer_lookup", keep="first")
    for _, row in customer_data.iterrows():
        customers.append(
            (
                text(row.customer_id), text(
                    row.customer_name), text(row.gender),
                parsed_date(row.date_of_birth), integer(row.age),
                text(row.marital_status), text(row.residency_status),
                text(row.occupation_group), number(row.annual_income_sgd),
                text(row.income_band),
            )
        )
    insert_many(
        cursor,
        f"""INSERT INTO {SCHEMA}.dim_customer
        (customer_id, customer_name, gender, date_of_birth, age, marital_status,
         residency_status, occupation_group, annual_income_sgd, income_band)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        customers,
    )

    products = []
    for (name, category), group in data.groupby(
        ["product_name", "product_category"], dropna=False
    ):
        products.append((text(name), text(category), int(
            group.nbp_margin_pct.astype(float).mean() < 0.10)))
    insert_many(
        cursor,
        f"INSERT INTO {SCHEMA}.dim_product (product_name, product_category, is_low_margin) VALUES (?, ?, ?)",
        products,
    )

    channels = [(text(value),) for value in data.distribution_channel.unique()]
    insert_many(
        cursor, f"INSERT INTO {SCHEMA}.dim_channel (channel_name) VALUES (?)", channels)

    agents = []
    agent_data = data.assign(
        _agent_key=data.agent_id.map(usable_dimension_key)
    ).dropna(subset=["_agent_key"])
    agent_data = agent_data.assign(
        _agent_lookup=agent_data._agent_key.map(lookup_text)
    ).drop_duplicates("_agent_lookup", keep="first")
    for agent_id, group in agent_data.groupby("_agent_key"):
        first = group.iloc[0]
        agents.append(
            (
                text(agent_id), text(first.agent_name), text(first.agent_tier),
                integer(first.agent_tenure_months), text(first.agency_unit),
                text(first.agent_status), number(
                    first.agent_productivity_score),
                number(first.agent_attrition_risk),
                min(parsed_date(value) for value in group.policy_start_date),
            )
        )
    insert_many(
        cursor,
        f"""INSERT INTO {SCHEMA}.dim_agent
        (agent_id, agent_name, agent_tier, agent_tenure_months, agency_unit,
         agent_status, agent_productivity_score, agent_attrition_risk, valid_from)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        agents,
    )

    assessors = []
    assessor_data = data.assign(
        _assessor_key=data.assessor_id.map(usable_dimension_key)
    ).dropna(subset=["_assessor_key"])
    assessor_data = assessor_data.assign(
        _assessor_lookup=assessor_data._assessor_key.map(lookup_text)
    ).drop_duplicates("_assessor_lookup", keep="first")
    for assessor_id, group in assessor_data.groupby("_assessor_key"):
        assessors.append((text(assessor_id), number(
            group.iloc[0].assessor_approval_limit_sgd)))
    insert_many(
        cursor,
        f"""INSERT INTO {SCHEMA}.dim_assessor
        (assessor_id, assessor_approval_limit_sgd) VALUES (?, ?)""",
        assessors,
    )

    claim_types = sorted({text(value)
                         for value in data.claim_type if text(value)})
    insert_many(
        cursor,
        f"INSERT INTO {SCHEMA}.dim_claim_type (claim_type) VALUES (?)",
        [(value,) for value in claim_types],
    )


def lookup(cursor, table, key_column, value_column):
    cursor.execute(
        f"SELECT {key_column}, {value_column} FROM {SCHEMA}.{table}")
    return {row[0]: row[1] for row in cursor.fetchall()}


def load_facts(cursor, data):
    customers = {
        lookup_text(customer_id): customer_key
        for customer_id, customer_key in lookup(
            cursor, "dim_customer", "customer_id", "customer_key"
        ).items()
    }
    products = lookup(cursor, "dim_product",
                      "product_name + '|' + product_category", "product_key")
    channels = lookup(cursor, "dim_channel", "channel_name", "channel_key")
    agents = {
        lookup_text(agent_id): agent_key
        for agent_id, agent_key in lookup(
            cursor, "dim_agent", "agent_id", "agent_key"
        ).items()
    }
    assessors = {
        lookup_text(assessor_id): assessor_key
        for assessor_id, assessor_key in lookup(
            cursor, "dim_assessor", "assessor_id", "assessor_key"
        ).items()
    }
    claim_types = lookup(cursor, "dim_claim_type",
                         "claim_type", "claim_type_key")

    policies = data.assign(
        _policy_key=data.apply(
            lambda row: policy_business_key(row.record_id, row.policy_id), axis=1
        )
    ).assign(
        _policy_lookup=lambda frame: frame._policy_key.map(lookup_text)
    ).drop_duplicates("_policy_lookup", keep="first")
    policy_rows = []
    for row in policies.itertuples(index=False):
        policy_rows.append(
            (
                text(row.record_id), policy_business_key(
                    row.record_id, row.policy_id
                ), customers.get(
                    lookup_text(row.customer_id)),
                products.get(
                    f"{text(row.product_name)}|{text(row.product_category)}", 1),
                channels.get(usable_dimension_key(row.distribution_channel),
                             1), agents.get(lookup_text(usable_dimension_key(
                                 row.agent_id))),
                key(row.policy_start_date), parsed_date(
                    row.quote_date), parsed_date(row.policy_start_date),
                integer(row.policy_term_years), text(
                    row.payment_frequency), text(row.policy_status),
                number(row.annual_premium_sgd), number(
                    row.sum_assured_sgd), number(row.ape_sgd),
                number(row.nbp_margin_pct), integer(
                    row.months_inforce), bit(row.lapse_flag),
                bit(row.persistency_13m_flag), bit(
                    row.chronic_condition_flag), text(row.ip_plan_tier),
                bit(row.rider_attached_flag), number(
                    row.rider_premium_before_sgd),
                number(row.rider_premium_after_sgd), number(
                    row.copay_pct), number(row.deductible_sgd),
                integer(row.care_delay_days),
            )
        )
    insert_many(
        cursor,
        f"""INSERT INTO {SCHEMA}.fact_policy_new_business
        (record_id, policy_id, customer_key, product_key, channel_key, agent_key,
         start_date_key, quote_date, policy_start_date, policy_term_years,
         payment_frequency, policy_status, annual_premium_sgd, sum_assured_sgd,
         ape_sgd, nbp_margin_pct, months_inforce, lapse_flag, persistency_13m,
         chronic_condition_flag, ip_plan_tier, rider_attached_flag,
         rider_premium_before_sgd, rider_premium_after_sgd, copay_pct,
         deductible_sgd, care_delay_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        policy_rows,
    )

    policy_keys = {
        lookup_text(policy_id): policy_key
        for policy_id, policy_key in lookup(
            cursor, "fact_policy_new_business", "policy_id", "policy_key"
        ).items()
    }
    claims = data[data.claim_id.map(text).notna()].drop_duplicates("claim_id")
    claim_rows = []
    for row in claims.itertuples(index=False):
        claim_rows.append(
            (
                text(row.claim_id), policy_keys[lookup_text(
                    policy_business_key(row.record_id, row.policy_id)
                )], key(row.claim_date),
                claim_types.get(text(row.claim_type)), assessors.get(
                    lookup_text(usable_dimension_key(row.assessor_id))),
                parsed_date(row.claim_date), number(
                    row.claim_amount_sgd), text(row.claim_status),
                text(row.claim_decline_reason), integer(
                    row.claim_tat_days), bit(row.disputed_flag),
                bit(row.complaint_flag), number(
                    row.customer_sentiment_score), integer(row.nps_score),
                bit(row.approval_above_limit_flag), bit(
                    row.sod_breach_flag), number(row.fraud_risk_score),
                number(row.assessor_approval_limit_sgd),
            )
        )
    insert_many(
        cursor,
        f"""INSERT INTO {SCHEMA}.fact_claim
        (claim_id, policy_key, claim_date_key, claim_type_key, assessor_key,
         claim_date, claim_amount_sgd, claim_status, claim_decline_reason,
         claim_tat_days, disputed_flag, complaint_flag, customer_sentiment_score,
         nps_score, approval_above_limit, sod_breach, fraud_risk_score,
         assessor_approval_limit_sgd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        claim_rows,
    )
    return len(policy_rows), len(claim_rows), len(data) - len(policies)


def validate(cursor):
    print("\n=== WAREHOUSE LOAD VALIDATION ===")
    for table in (
        "dim_date", "dim_customer", "dim_product", "dim_channel", "dim_agent",
        "dim_assessor", "dim_claim_type", "fact_policy_new_business", "fact_claim",
    ):
        count = cursor.execute(
            f"SELECT COUNT(*) FROM {SCHEMA}.{table}").fetchone()[0]
        print(f"{SCHEMA}.{table}: {count:,}")


def main():
    print("PRUDENTIAL SINGAPORE - STAR SCHEMA LOAD")
    print(f"Source   : {CSV_PATH}")
    print(f"Server   : {DB_SERVER}")
    print(f"Database : {DB_NAME}")
    data = pd.read_csv(CSV_PATH)
    print(f"Source rows: {len(data):,}")
    connection = None
    try:
        connection = get_connection()
        cursor = connection.cursor()
        reset_load_tables(cursor)
        load_dimensions(cursor, data)
        policy_count, claim_count, duplicate_count = load_facts(cursor, data)
        connection.commit()
        print(
            f"[COMMIT] Loaded {policy_count:,} policies and {claim_count:,} claims.")
        print(f"Duplicate policy rows skipped: {duplicate_count:,}")
        validate(cursor)
        return 0
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        print(f"[ROLLBACK] Warehouse load failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
