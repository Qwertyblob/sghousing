# Model card: HDB resale price model 2026-10

**Indicative only.** These are statistical estimates from past HDB resale transactions. They
are not valuations, and must not be used as the basis for buying, selling or financing a
flat.

## What it predicts

The resale price of an HDB flat, as three quantiles: p10, p50 (the point estimate) and p90.
The p10-p90 range is meant to contain 80% of actual prices. Each is a LightGBM model on
`log(resale_price)`, exported to ONNX (`model_p10.onnx`, `model_p50.onnx` and
`model_p90.onnx`, with inputs described in `features.json`).

## Data

- Source: HDB resale flat prices on data.gov.sg (collection 189), with locations from
  OneMap and amenities from LTA, MOE, NEA, NParks and URA datasets.
- Training: 2015-01 to 2023-12 (206,303 sales).
- Validation (tuning, early stopping, interval calibration): to 2024-12
  (27,832 sales).
- Final models: refit on training + validation, 2015-01 to
  2024-12 (234,135 sales).
- Test: after 2024-12 to 2026-10 (46,518 sales).
- Trained on 2026-10-26 with LightGBM 4.7.0.

## Features

- Categorical (fixed integer codes, LightGBM categorical splits): town, flat_type, flat_model, planning_area.
- Numeric: floor_area_sqm, storey_mid, rel_storey, remaining_lease_months, flat_age_years, month_index, lat, lon, dist_mrt_m, n_mrt_exits_500m, n_primary_schools_1km, dist_nearest_primary_m, dist_hawker_m, dist_park_m, dist_cbd_m, block_max_floor, block_year_completed, town_psm_3m, town_psm_6m, town_psm_12m, block_psm_24m, block_sales_24m.
- `town_psm_*`, `block_psm_24m` and `block_sales_24m` are rolling medians and counts over
  months strictly before the sale (`pipeline/sql/205_marts_rolling_features.sql`).

## Training

Random search over 30 parameter sets for the median model, scored by
validation MAE in dollars, with early stopping on validation. Best: learning rate
0.021, 62 leaves, at least
113 sales per leaf. The p10 and p90 models use the same parameters.

Out of sample the raw intervals drift with the market, in whichever direction it moves: fit
to 2023, the p10-p90 range held 68.1% of 2024 prices. The interval
is therefore widened with conformalized quantile regression on a yearly backtest: each year
from 2019 to 2024 is predicted by p10/p90 models fit only on earlier years, and
both bounds move out by 0.0207 in log price (about 2.1%)
so that 80% of those out-of-sample prices fall inside. Raw coverage by year:

| Year | Sales | Inside p10-p90 | Below p10 | Above p90 |
|---|---|---|---|---|
| 2019 | 22,186 | 69.7% | 17.7% | 12.6% |
| 2020 | 23,333 | 68.6% | 10.8% | 20.6% |
| 2021 | 29,087 | 53.7% | 3.6% | 42.6% |
| 2022 | 26,720 | 65.1% | 6.9% | 28.0% |
| 2023 | 25,754 | 73.2% | 13.1% | 13.6% |
| 2024 | 27,832 | 67.9% | 5.9% | 26.2% |

The three models were then refit on training + validation with the same parameters and
numbers of trees, and the widening applied. Quantiles that cross are sorted so that
p10 <= p50 <= p90.

## Metrics

Validation (models fit on the training split only, before refitting):

| Model | MAE (S$) | MAPE | RMSE (S$) | R² |
|---|---|---|---|---|
| LightGBM (median) | 27,579 | 4.39% | 38,785 | 0.958 |
| Town median baseline | 73,732 | 11.09% | 111,935 | 0.648 |
| Ridge baseline | 43,239 | 6.61% | 74,115 | 0.846 |

Test (final models; baselines refit on training + validation too):

| Model | MAE (S$) | MAPE | RMSE (S$) | R² |
|---|---|---|---|---|
| LightGBM (median) | 29,176 | 4.43% | 42,177 | 0.960 |
| Town median baseline | 83,928 | 12.02% | 126,408 | 0.640 |
| Ridge baseline | 58,694 | 8.21% | 99,367 | 0.778 |

80% interval (p10-p90) coverage on test: 79.6%
(146 sales where the models crossed and were sorted).

Test by flat type (LightGBM median):

| Group | MAE (S$) | MAPE | RMSE (S$) | R² |
|---|---|---|---|---|
| 2 ROOM | 16,152 | 4.41% | 21,365 | 0.785 |
| 3 ROOM | 22,294 | 4.81% | 30,774 | 0.927 |
| 4 ROOM | 28,265 | 4.19% | 39,345 | 0.946 |
| 5 ROOM | 35,872 | 4.46% | 52,256 | 0.920 |
| EXECUTIVE | 42,632 | 4.44% | 59,031 | 0.832 |

Test by region (LightGBM median):

| Group | MAE (S$) | MAPE | RMSE (S$) | R² |
|---|---|---|---|---|
| CENTRAL REGION | 41,793 | 5.60% | 59,642 | 0.963 |
| EAST REGION | 29,437 | 4.27% | 44,822 | 0.941 |
| NORTH REGION | 23,341 | 4.01% | 31,413 | 0.949 |
| NORTH-EAST REGION | 26,985 | 4.13% | 36,739 | 0.953 |
| WEST REGION | 25,150 | 4.19% | 34,365 | 0.960 |

Monthly test metrics are in `metrics.json` (`test.by_month`).

## Earlier test scores

The test split was scored before these results. The changes since were chosen
on data from before the test period only, but knowing the earlier results is a
form of test-set reuse.

- 2026-10-25: trained to 2023-12, uncalibrated intervals: MAE S$40,058, 80% interval coverage 64.1%.
- 2026-10-26: refit to 2024-12, interval offsets calibrated on 2024 only: MAE S$29,147, 80% interval coverage 62.6%.

## Known limitations

- No unit-level information: condition, renovation, facing, view, noise and exact floor
  (only a 3-storey range) are unknown.
- Prices are nominal Singapore dollars, with no inflation adjustment.
- Amenity locations are today's, so new stations or hawker centres also count for older
  sales.
- Trained on sales up to 2024-12. The rolling price features carry recent price
  levels forward, but the model cannot anticipate faster price growth or sudden shocks
  (policy changes, a pandemic); accuracy and interval coverage fall the further a sale is
  from the training data.
- Rare flats (1-room, multi-generation, terrace houses) have little data and wider errors.
