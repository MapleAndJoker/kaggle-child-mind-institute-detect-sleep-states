import sys
sys.path.append("/root/aicom/kagglep/kernels/yinhaojie/cmi-714/kaggle-child-mind-institute-detect-sleep-states")

import shutil
from pathlib import Path

import hydra
import numpy as np
import polars as pl
from omegaconf import DictConfig
from tqdm import tqdm

from src.utils.common import trace

SERIES_SCHEMA = {
    "series_id": pl.Utf8,
    "step": pl.UInt32,
    "anglez": pl.Float32,
    "enmo": pl.Float32,
}


FEATURE_NAMES = [
    "anglez",
    "enmo",
    "hour_sin",
    "hour_cos",
    "month_sin",
    "month_cos",
    "minute_sin",
    "minute_cos",
]

ANGLEZ_MEAN = -8.810476
ANGLEZ_STD = 35.521877
ENMO_MEAN = 0.041315
ENMO_STD = 0.101829


'''
处理周期性数据 时序数据
时间特征具有明显的周期性，例如一天有24小时，一周有7天，一年有12个月等
如果我们直接使用这些数值作为特征，模型可能会难以捕捉到这种周期性，因为它们的数值是线性的。
例如，对于小时特征，23点和0点在数值上相差很大，但实际上它们在时间上是连续的。
为了解决这个问题，我们可以使用三角函数将这些特征映射到一个圆上，这样周期的开始和结束就可以无缝连接了。
'''
def to_coord(x: pl.Expr, max_: int, name: str) -> list[pl.Expr]:
    rad = 2 * np.pi * (x % max_) / max_
    # 正弦和余弦函数是在弧度制下定义的，需要将这个比例转换成弧度
    x_sin = rad.sin()
    x_cos = rad.cos()

    return [x_sin.alias(f"{name}_sin"), x_cos.alias(f"{name}_cos")]


def add_feature(series_df: pl.DataFrame) -> pl.DataFrame:
    #with_columns: 添加新列
    series_df = series_df.with_columns(
        *to_coord(pl.col("timestamp").dt.hour(), 24, "hour"),
        *to_coord(pl.col("timestamp").dt.month(), 12, "month"),
        *to_coord(pl.col("timestamp").dt.minute(), 60, "minute"),
    ).select("series_id", *FEATURE_NAMES)
    return series_df


def save_each_series(this_series_df: pl.DataFrame, columns: list[str], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    for col_name in columns:
        x = this_series_df.get_column(col_name).to_numpy(zero_copy_only=True)
        np.save(output_dir / f"{col_name}.npy", x)


@hydra.main(config_path="conf", config_name="prepare_data", version_base="1.2")
def main(cfg: DictConfig):
    processed_dir: Path = Path(cfg.dir.processed_dir) / cfg.phase

    # ディレクトリが存在する場合は削除
    if processed_dir.exists():
        shutil.rmtree(processed_dir)
        print(f"Removed {cfg.phase} dir: {processed_dir}")

    with trace("Load series"):
        # scan parquet
        if cfg.phase in ["train", "test"]:
            series_lf = pl.scan_parquet(
                Path(cfg.dir.data_dir) / f"{cfg.phase}_series.parquet",
                low_memory=True,
            )
            # 延迟读取的数据框架
        elif cfg.phase == "dev":
            series_lf = pl.scan_parquet(
                Path(cfg.dir.processed_dir) / f"{cfg.phase}_series.parquet",
                low_memory=True,
            )
        else:
            raise ValueError(f"Invalid phase: {cfg.phase}")

        # preprocess
        series_df = (
            series_lf.with_columns(
                pl.col("timestamp").str.to_datetime("%Y-%m-%dT%H:%M:%S%z"),
                (pl.col("anglez") - ANGLEZ_MEAN) / ANGLEZ_STD,
                (pl.col("enmo") - ENMO_MEAN) / ENMO_STD,
            )
            .select([pl.col("series_id"), pl.col("anglez"), pl.col("enmo"), pl.col("timestamp")])
            .collect(streaming=True)
            .sort(by=["series_id", "timestamp"])
        )
        n_unique = series_df.get_column("series_id").n_unique()
    # 按序列标识符分组数据，对每个组添加特征，然后将这些特征保存
    with trace("Save features"):
        for series_id, this_series_df in tqdm(series_df.group_by("series_id"), total=n_unique):
            # 特徴量を追加
            this_series_df = add_feature(this_series_df)

            # 特徴量をそれぞれnpyで保存
            series_dir = processed_dir / series_id  # type: ignore
            save_each_series(this_series_df, FEATURE_NAMES, series_dir)


if __name__ == "__main__":
    main()
