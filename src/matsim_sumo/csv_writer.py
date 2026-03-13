from pathlib import Path

import pandas as pd


class CsvWriter:
    @staticmethod
    def write_df(
        df: pd.DataFrame,
        out_path: Path | str,
        sep: str = ";",
        index: bool = False,
        compression: str = "infer",
    ) -> None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, sep=sep, index=index, compression=compression)

    @staticmethod
    def write_series(
        series: pd.Series,
        out_path: Path | str,
        index: bool = False,
        header: bool = False,
    ) -> None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        series.to_csv(out, index=index, header=header)
