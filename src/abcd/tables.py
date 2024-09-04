from itertools import product
import polars as pl
import pandas as pd
import numpy as np
from tqdm import tqdm

from abcd.config import Config, get_config


def quartile_counts():
    data = pl.read_csv("data/raw/metadata.csv")
    data = data.to_pandas()
    data["Next quartile"] = data["Next quartile"] + 1
    quartile = data["Next quartile"]
    variables = ["Sex", "Age", "Race", "Follow-up event", "Event year", "ADI quartile"]
    dfs = []
    for i, name in enumerate(variables):
        variable = data[name]
        df1 = pd.crosstab(
            [variable],
            quartile,
            rownames=["Group"],
            colnames=["Next quartile"],
            margins=True,
        ).astype(str)
        df2 = " (" + (
            pd.crosstab(
                [variable],
                quartile,
                rownames=["Group"],
                colnames=["Next quartile"],
                margins=True,
                normalize="all",
            )
            .mul(100)
            .round(1)
            .astype(str)
            + "%)"
        )
        df = df1 + df2
        df = df.reset_index()
        df["Group"] = [int(i) if isinstance(i, float) else i for i in df["Group"]]
        df.insert(0, "Variable", name)
        if i == 0:
            df = df.reindex(np.roll(df.index, shift=1))
            df.iloc[0, 0] = "All"
        else:
            df = df.iloc[:-1]
        dfs.append(df)
    df = pd.concat(dfs).rename(
        columns={
            1: "Quartile 1",
            2: "Quartile 2",
            3: "Quartile 3",
            4: "Quartile 4",
            "All": "Total",
        }
    )
    print(df)
    df.to_csv("data/tables/table_1.csv", index=False)


def demographic_counts():
    columns = ["Subject ID", "Sex", "Race", "ADI quartile"]
    df = pl.read_csv("data/raw/metadata.csv").select(columns).unique(columns)
    n = df["Subject ID"].n_unique()
    df = (
        df.melt(id_vars="Subject ID")
        .group_by("variable")
        .agg(pl.col("value").value_counts())
        .explode("value")
        .unnest("value")
        .with_columns(
            pl.col("count")
            .truediv(n)
            .mul(100)
            .round(1)
            .cast(pl.String)
            .add("%")
            .alias("percentage")
        )
        .sort("variable", "value")
        .write_csv("data/tables/supplemental_table_1.csv")
    )


def make_metric_table(df: pl.DataFrame, groups: list[str]):
    df = (
        df.group_by(groups + ["Quartile at t+1"], maintain_order=True)
        .agg(
            pl.col("Prevalence").first(),
            pl.col("value").mean().round(2).cast(pl.String)
            + " ± "
            + pl.col("value").std(2).round(2).cast(pl.String),
        )
        .with_columns(pl.col("Prevalence").round(2))
        .with_columns(
            pl.when(pl.col("Metric").eq("AP"))
            .then(
                pl.col("value").add(" (" + pl.col("Prevalence").cast(pl.String)) + ")"
            )
            .otherwise(pl.col("value"))
        )
        .sort("Quartile at t+1")
        .pivot(on="Quartile at t+1", values="value", index=groups)
        .rename(
            {"1": "No risk", "2": "Low risk", "3": "Moderate risk", "4": "High risk"}
        )
        .with_columns(pl.col("Metric").cast(pl.Enum(["AUROC", "AP"])))
    )
    group_order = [
        pl.col("Group").cast(pl.Int32, strict=False) if group == "Group" else group
        for group in groups
    ]
    return df.sort(group_order)


def shap_table():
    df = pl.read_csv("data/results/shap_coefs.csv")
    variables = pl.read_csv("data/variables.csv").rename({"column": "variable"})
    df = df.group_by("variable").agg(
        pl.col("value").mean().alias("shap_coef_mean"),
        pl.col("value").std().alias("shap_coef_std"),
    )
    df = (
        df.join(variables, on="variable", how="left")
        .select(
            [
                "dataset",
                "table",
                "respondent",
                "variable",
                "shap_coef_mean",
                "shap_coef_std",
                "question",
                "response",
            ]
        )
        .sort(["dataset", "table", "respondent", "variable"])
    )
    df.write_csv("data/tables/shap_coefs.csv")


def aggregate_metrics(analyses: list[str], factor_models: list[str]):
    for metric_type in ("curves", "metrics", "sensitivity_specificity"):
        metrics = []
        progress_bar = tqdm(
            product(analyses, factor_models), total=len(analyses) * len(factor_models)
        )
        for analysis, factor_model in progress_bar:
            path = f"data/analyses/{factor_model}/{analysis}/results/metrics/{metric_type}.csv"
            metric = pl.scan_csv(path).with_columns(
                pl.lit(factor_model).alias("Factor model"),
                pl.lit(analysis).alias("Predictor set"),
            )
            metrics.append(metric)
        pl.concat(metrics).with_columns(
            pl.col("Predictor set").replace(
                {
                    "questions": "{Questions}",
                    "symptoms": "{CBCL scales}",
                    "questions_symptoms": "{Questions, CBCL scales}",
                    "questions_mri": "{Questions, MRI}",
                    "questions_mri_symptoms": "{Questions, MRI, CBCL scales}",
                    "autoregressive": "{Previous p-factors}",
                }
            ),
            pl.col("Factor model").replace(
                {"within_event": "Within-event", "across_event": "Across-event"}
            ),
        ).sink_parquet(f"data/results/metrics/{metric_type}.parquet")


def quartile_metric_table(df: pl.DataFrame):
    return df.filter(
        pl.col("Variable").eq("Quartile subset")
        & pl.col("Predictor set").is_in(["{CBCL scales}", "{Questions}"])
        & pl.col("Factor model").eq("Within-event")
    ).drop("Factor model", "Variable")


def demographic_metric_table(df: pl.DataFrame):
    return df.filter(
        pl.col("Variable").ne("Quartile subset")
        & pl.col("Factor model").eq("Within-event")
    ).drop("Factor model")


def make_tables(config: Config):
    # quartile_counts()
    # demographic_counts()
    aggregate_metrics(analyses=config.analyses, factor_models=config.factor_models)
    df = pl.read_parquet("data/results/metrics/metrics.parquet")
    groups = ["Factor model", "Predictor set", "Metric", "Variable", "Group"]
    metric_table = make_metric_table(df=df, groups=groups)
    quartile_metrics = quartile_metric_table(df=metric_table)
    print(quartile_metrics)
    quartile_metrics.write_csv("data/tables/table_2.csv")
    demographic_metrics = demographic_metric_table(df=metric_table)
    print(demographic_metrics)
    demographic_metrics.write_csv("data/supplement/tables/supplemental_table_2.csv")
    # shap_table()


if __name__ == "__main__":
    config = get_config()
    make_tables(config)
