"""Run and optionally persist Scenario S-01 for the Prudential warehouse."""

import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyodbc


SCHEMA = "prudential"
DEFAULT_SCENARIO = "S-01"
SEED = 42
ITERATIONS = 10_000
SHIFT = 0.10
SOURCE_SHARES = {
    "Protection": 0.60,
    "Health & IP": 0.40,
}
OUTPUT_DIR = Path(__file__).resolve().parent / "Output" / "Simulation_Results"

SCENARIO_REGISTRY = {}


@dataclass(frozen=True)
class SimulationResult:
    baseline_margin: float
    p05: float
    p50: float
    p95: float
    total_ape: float
    central_impact_bps: float
    nbp_at_risk_sgd: float


def get_connection():
    """Create a SQL Server connection from the project environment settings."""
    server = os.getenv("PRUDENTIAL_DB_SERVER", r"localhost\RAKESHSQLEXPRESS")
    database = os.getenv("PRUDENTIAL_DB_NAME", "PrudentialDW")
    driver = os.getenv(
        "PRUDENTIAL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"
    )
    trusted = os.getenv("PRUDENTIAL_TRUSTED_CONNECTION", "yes").lower()

    if trusted in {"yes", "true", "1"}:
        connection_string = (
            f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
            "Trusted_Connection=yes;TrustServerCertificate=yes;"
        )
    else:
        user = os.getenv("PRUDENTIAL_DB_USER", "")
        password = os.getenv("PRUDENTIAL_DB_PASSWORD", "")
        if not user or not password:
            raise RuntimeError(
                "SQL authentication requires PRUDENTIAL_DB_USER and "
                "PRUDENTIAL_DB_PASSWORD."
            )
        connection_string = (
            f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
            f"UID={user};PWD={password};TrustServerCertificate=yes;"
        )
    return pyodbc.connect(connection_string, autocommit=False)


def load_baseline(connection):
    """Load clean APE and margin statistics by product category."""
    query = f"""
        SELECT p.product_category,
               SUM(f.ape_sgd) AS ape_sgd,
               AVG(f.nbp_margin_pct) AS mean_margin,
               STDEV(f.nbp_margin_pct) AS sd_margin
        FROM {SCHEMA}.fact_policy_new_business AS f
        INNER JOIN {SCHEMA}.dim_product AS p
            ON p.product_key = f.product_key
        WHERE f.dq_row_status = 'OK'
          AND f.ape_sgd IS NOT NULL
          AND f.nbp_margin_pct IS NOT NULL
        GROUP BY p.product_category;
    """
    cursor = connection.cursor()
    cursor.execute(query)
    rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(
            "The warehouse returned no clean simulation baseline.")

    categories = [row[0] for row in rows]
    ape = np.array([float(row[1]) for row in rows], dtype=float)
    mean_margin = np.array([float(row[2]) for row in rows], dtype=float)
    sd_margin = np.array(
        [0.0 if row[3] is None else float(row[3]) for row in rows],
        dtype=float,
    )
    if np.any(ape < 0) or ape.sum() <= 0:
        raise ValueError(
            "Baseline APE must be non-negative and greater than zero.")
    if np.any((mean_margin < 0) | (mean_margin > 1)):
        raise ValueError(
            "Baseline margins must be within the 0-1 fraction scale.")
    return categories, ape, mean_margin, sd_margin


def simulate_s01(categories, ape, mean_margin, sd_margin):
    """Simulate a 10-point mix shift into Savings & Wealth."""
    category_index = {category: index for index,
                      category in enumerate(categories)}
    required = ["Savings & Wealth", *SOURCE_SHARES]
    missing = [category for category in required if category not in category_index]
    if missing:
        raise ValueError(
            "S-01 requires these product categories in the baseline: "
            + ", ".join(missing)
        )

    total_ape = float(ape.sum())
    baseline_mix = ape / total_ape
    new_mix = baseline_mix.copy()
    target_index = category_index["Savings & Wealth"]
    new_mix[target_index] += SHIFT
    for category, proportion in SOURCE_SHARES.items():
        source_index = category_index[category]
        reduction = SHIFT * proportion
        if new_mix[source_index] < reduction:
            raise ValueError(
                f"S-01 would make {category} have a negative mix share.")
        new_mix[source_index] -= reduction
    if not np.isclose(new_mix.sum(), 1.0):
        raise ValueError("The simulated product mix does not sum to 100%.")

    rng = np.random.default_rng(SEED)
    draws = rng.normal(
        loc=mean_margin,
        scale=sd_margin,
        size=(ITERATIONS, len(categories)),
    )
    draws = np.clip(draws, 0.01, 0.45)
    simulated_margins = draws @ new_mix
    baseline_margin = float(baseline_mix @ mean_margin)
    p05, p50, p95 = np.percentile(simulated_margins, [5, 50, 95])
    return SimulationResult(
        baseline_margin=baseline_margin,
        p05=float(p05),
        p50=float(p50),
        p95=float(p95),
        total_ape=total_ape,
        central_impact_bps=float((p50 - baseline_margin) * 10_000),
        nbp_at_risk_sgd=float((baseline_margin - p50) * total_ape),
    )


def persist_result(connection, result, scenario_code=DEFAULT_SCENARIO, scenario_name="Product mix +10% to Savings & Wealth", iterations=ITERATIONS, seed=SEED):
    """Persist one clean scenario result with standardized KPI and sensitivity columns."""
    cursor = connection.cursor()
    cursor.execute(
        f"""
        INSERT INTO {SCHEMA}.scenario_run
            (scenario_code, scenario_name, run_by, iterations, random_seed)
        OUTPUT INSERTED.run_id
        VALUES (?, ?, ?, ?, ?);
        """,
        scenario_code,
        scenario_name,
        os.getenv("USERNAME", "unknown"),
        iterations,
        seed,
    )
    run_id = cursor.fetchone()[0]
    cursor.execute(
        f"""
        INSERT INTO {SCHEMA}.scenario_result
            (run_id, metric_name, p05, p50, p95, baseline, delta_vs_base, result_unit, central_impact_bps, nbp_at_risk_sgd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        run_id,
        "blended_nbp_margin",
        result.p05 * 100,
        result.p50 * 100,
        result.p95 * 100,
        result.baseline_margin * 100,
        (result.p50 - result.baseline_margin) * 100,
        "%",
        result.central_impact_bps,
        result.nbp_at_risk_sgd,
    )
    connection.commit()
    return run_id


def print_result(result, run_id=None, scenario_code=DEFAULT_SCENARIO):
    """Print the decision-useful scenario outputs."""
    print(f"Scenario               : {scenario_code}")
    print(f"Baseline blended margin : {result.baseline_margin:.3%}")
    print(
        f"Simulated p05/p50/p95   : {result.p05:.3%} / {result.p50:.3%} / {result.p95:.3%}")
    print(f"Central impact          : {result.central_impact_bps:,.0f} bps")
    print(f"NBP at risk             : S${result.nbp_at_risk_sgd:,.2f}")
    print(f"Total clean APE         : S${result.total_ape:,.2f}")
    print(f"Seed / iterations       : {SEED} / {ITERATIONS:,}")
    if run_id is not None:
        print(f"Persisted scenario run  : {run_id}")


def save_output(result, run_id, scenario_code=DEFAULT_SCENARIO, scenario_name="Product mix +10% to Savings & Wealth", iterations=ITERATIONS, seed=SEED):
    """Write reproducible scenario result artifacts for review and handover."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    values = {
        "scenario_code": scenario_code,
        "scenario_name": scenario_name,
        "generated_at_utc": generated_at,
        "sql_run_id": run_id,
        "seed": seed,
        "iterations": iterations,
        "shift_percentage_points": SHIFT * 100,
        "source_protection_share": SOURCE_SHARES["Protection"],
        "source_health_ip_share": SOURCE_SHARES["Health & IP"],
        "baseline_margin_pct": result.baseline_margin * 100,
        "p05_margin_pct": result.p05 * 100,
        "p50_margin_pct": result.p50 * 100,
        "p95_margin_pct": result.p95 * 100,
        "delta_vs_base_pct": (result.p50 - result.baseline_margin) * 100,
        "result_unit": "%",
        "central_impact_bps": result.central_impact_bps,
        "nbp_at_risk_sgd": result.nbp_at_risk_sgd,
        "total_clean_ape_sgd": result.total_ape,
    }
    file_suffix = f"scenario_{scenario_code.lower().replace('-', '')}_result"
    json_path = OUTPUT_DIR / f"{file_suffix}.json"
    csv_path = OUTPUT_DIR / f"{file_suffix}.csv"
    json_path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=values.keys())
        writer.writeheader()
        writer.writerow(values)
    print(f"Output JSON              : {json_path}")
    print(f"Output CSV               : {csv_path}")


def simulate_s02(categories, ape, mean_margin, sd_margin):
    """Simulate Scenario S-02: IP rider repricing by 55% with a co-pay increase.

    The scenario assumes the IP rider price is cut materially while the policyholder
    absorbs part of the cost through higher co-pay. This compresses rider economics,
    moderates retention, and lowers blended new-business margin for rider-attached
    policies.
    """
    category_index = {category: index for index,
                      category in enumerate(categories)}
    required = ["Health & IP", "Protection", "Savings & Wealth"]
    missing = [category for category in required if category not in category_index]
    if missing:
        raise ValueError(
            "S-02 requires these categories in the baseline: " +
            ", ".join(missing)
        )

    total_ape = float(ape.sum())
    baseline_mix = ape / total_ape
    new_mix = baseline_mix.copy()

    price_cut = 0.55
    copay_rise = 0.10
    net_drag = price_cut * (1.0 - copay_rise) * 0.65
    adjusted_mean_margin = np.clip(mean_margin * (1.0 - net_drag), 0.01, 0.45)

    rng = np.random.default_rng(SEED + 2)
    draws = rng.normal(
        loc=adjusted_mean_margin,
        scale=np.clip(sd_margin * 0.85, 0.01, 0.30),
        size=(ITERATIONS, len(categories)),
    )
    draws = np.clip(draws, 0.01, 0.45)
    simulated_margins = draws @ new_mix

    baseline_margin = float(baseline_mix @ mean_margin)
    p05, p50, p95 = np.percentile(simulated_margins, [5, 50, 95])

    return SimulationResult(
        baseline_margin=baseline_margin,
        p05=float(p05),
        p50=float(p50),
        p95=float(p95),
        total_ape=total_ape,
        central_impact_bps=float((p50 - baseline_margin) * 10_000),
        nbp_at_risk_sgd=float((baseline_margin - p50) * total_ape),
    )


def simulate_generic(
    categories,
    ape,
    mean_margin,
    sd_margin,
    *,
    margin_multiplier=1.0,
    scale_multiplier=1.0,
    mix_shift=None,
    seed_offset=0,
    min_margin=0.01,
    max_margin=0.45,
):
    """Run a generic multi-scenario Monte Carlo margin shock using the same baseline."""
    category_index = {category: index for index,
                      category in enumerate(categories)}
    total_ape = float(ape.sum())
    baseline_mix = ape / total_ape
    new_mix = baseline_mix.copy()

    if mix_shift:
        for category, adjustment in mix_shift.items():
            if category not in category_index:
                raise ValueError(f"Unknown category in mix shift: {category}")
            target_index = category_index[category]
            new_mix[target_index] += adjustment
        if not np.isclose(new_mix.sum(), 1.0, atol=1e-6):
            raise ValueError(
                "The scenario mix shift does not preserve the full APE mix.")

    adjusted_loc = np.clip(
        mean_margin * margin_multiplier, min_margin, max_margin)
    adjusted_scale = np.clip(sd_margin * scale_multiplier, 0.01, 0.30)
    rng = np.random.default_rng(SEED + seed_offset)
    draws = rng.normal(
        loc=adjusted_loc,
        scale=adjusted_scale,
        size=(ITERATIONS, len(categories)),
    )
    draws = np.clip(draws, min_margin, max_margin)
    simulated_margins = draws @ new_mix

    baseline_margin = float(baseline_mix @ mean_margin)
    p05, p50, p95 = np.percentile(simulated_margins, [5, 50, 95])
    return SimulationResult(
        baseline_margin=baseline_margin,
        p05=float(p05),
        p50=float(p50),
        p95=float(p95),
        total_ape=total_ape,
        central_impact_bps=float((p50 - baseline_margin) * 10_000),
        nbp_at_risk_sgd=float((baseline_margin - p50) * total_ape),
    )


def simulate_s03(categories, ape, mean_margin, sd_margin):
    """S-03: care delay increases 6 months, increasing claim friction and compressing margin."""
    return simulate_generic(
        categories,
        ape,
        mean_margin,
        sd_margin,
        margin_multiplier=0.82,
        scale_multiplier=1.18,
        seed_offset=3,
    )


def simulate_s04(categories, ape, mean_margin, sd_margin):
    """S-04: dispute escalation creates a wider claim friction shock."""
    return simulate_generic(
        categories,
        ape,
        mean_margin,
        sd_margin,
        margin_multiplier=0.72,
        scale_multiplier=1.30,
        seed_offset=4,
    )


def simulate_s05(categories, ape, mean_margin, sd_margin):
    """S-05: assessor approvals sit just below the threshold, causing more claims to be funded."""
    return simulate_generic(
        categories,
        ape,
        mean_margin,
        sd_margin,
        margin_multiplier=0.76,
        scale_multiplier=1.20,
        seed_offset=5,
    )


def simulate_s06(categories, ape, mean_margin, sd_margin):
    """S-06: top 5% of agents resign, reducing sales momentum and high-margin distribution capacity."""
    return simulate_generic(
        categories,
        ape,
        mean_margin,
        sd_margin,
        margin_multiplier=0.86,
        scale_multiplier=1.12,
        mix_shift={"Savings & Wealth": -0.04,
                   "Protection": 0.01, "Health & IP": 0.03},
        seed_offset=6,
    )


def simulate_s07(categories, ape, mean_margin, sd_margin):
    """S-07: competitor undercuts by 15%, reducing pricing power and compressing margin."""
    return simulate_generic(
        categories,
        ape,
        mean_margin,
        sd_margin,
        margin_multiplier=0.70,
        scale_multiplier=1.25,
        seed_offset=7,
    )


def simulate_s08(categories, ape, mean_margin, sd_margin):
    """S-08: claims volume spikes 3x, triggering an extreme operating loss shock."""
    return simulate_generic(
        categories,
        ape,
        mean_margin,
        sd_margin,
        margin_multiplier=0.58,
        scale_multiplier=1.55,
        seed_offset=8,
    )


SCENARIO_REGISTRY["S-01"] = {
    "scenario_name": "Product mix +10% to Savings & Wealth",
    "simulator": simulate_s01,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-02"] = {
    "scenario_name": "IP rider repriced -55% with co-pay",
    "simulator": simulate_s02,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-03"] = {
    "scenario_name": "Care delay increases 6 months",
    "simulator": simulate_s03,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-04"] = {
    "scenario_name": "Dispute goes viral",
    "simulator": simulate_s04,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-05"] = {
    "scenario_name": "Assessor approves just under threshold",
    "simulator": simulate_s05,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-06"] = {
    "scenario_name": "Top 5% of agents resign",
    "simulator": simulate_s06,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-07"] = {
    "scenario_name": "Competitor undercuts by 15%",
    "simulator": simulate_s07,
    "seed": 42,
    "iterations": 10_000,
}
SCENARIO_REGISTRY["S-08"] = {
    "scenario_name": "Claims volume spikes 3x",
    "simulator": simulate_s08,
    "seed": 42,
    "iterations": 10_000,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIO_REGISTRY.keys()),
        default=DEFAULT_SCENARIO,
        help="Scenario code to run. All future scenarios must live in this file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the simulation without inserting scenario results.",
    )
    args = parser.parse_args()

    scenario_code = args.scenario
    scenario_spec = SCENARIO_REGISTRY[scenario_code]
    scenario_name = scenario_spec["scenario_name"]
    scenario_seed = scenario_spec["seed"]
    scenario_iterations = scenario_spec["iterations"]

    connection = get_connection()
    try:
        baseline = load_baseline(connection)
        result = scenario_spec["simulator"](*baseline)
        run_id = None if args.dry_run else persist_result(
            connection,
            result,
            scenario_code=scenario_code,
            scenario_name=scenario_name,
            iterations=scenario_iterations,
            seed=scenario_seed,
        )
        print_result(result, run_id, scenario_code=scenario_code)
        if run_id is not None:
            save_output(
                result,
                run_id,
                scenario_code=scenario_code,
                scenario_name=scenario_name,
                iterations=scenario_iterations,
                seed=scenario_seed,
            )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
