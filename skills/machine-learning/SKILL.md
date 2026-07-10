---
name: machine-learning
description: ML engineering on the offline stack - scikit-learn/XGBoost/LightGBM/PyTorch(MPS), temporal cross-validation, Optuna tuning, MLflow tracking, calibration and interpretation. Use when building, tuning, or evaluating predictive models.
---

# Machine Learning (offline, finance-aware)

Stack: scikit-learn, XGBoost, LightGBM, PyTorch (Apple **MPS**), Optuna, SHAP,
MLflow (local file backend: `mlflow.set_tracking_uri("file:artifacts/mlruns")`).

## Non-negotiables for financial ML

1. **Temporal splits only.** Random K-fold on time series is leakage. Use walk-forward
   or purged K-fold with an embargo (gap ≥ label horizon) between train and test:

```python
def purged_walk_forward(dates, n_folds=5, embargo="30D"):
    """Yield (train_idx, test_idx); train strictly precedes test minus embargo."""
    bounds = pd.date_range(dates.min(), dates.max(), periods=n_folds + 1)
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        test = (dates >= lo) & (dates < hi)
        train = dates < (lo - pd.Timedelta(embargo))
        if train.sum() and test.sum():
            yield np.where(train)[0], np.where(test)[0]
```

2. **Fit preprocessing inside the fold.** Scalers, imputers, target encoders, feature
   selection — all inside a `Pipeline`, fit on train only. A scaler fit on all data is leakage.
3. **Labels:** define the horizon explicitly; consider triple-barrier labels for
   trading; check class balance per fold, not globally.
4. **Baselines first:** always report naive persistence / historical mean / logistic-on-
   one-feature next to the fancy model. If the GBM cannot beat persistence, say so.
5. **Metrics that match the decision:** ranking → Spearman IC per period (report mean
   and t-stat across periods); classification for sizing → calibrated probabilities
   (`CalibratedClassifierCV`, Brier score, reliability curve); never accuracy alone.

## Gradient boosting defaults (tabular finance)

- LightGBM: `num_leaves=31..127`, `min_data_in_leaf≥100` (noisy targets need big leaves),
  `feature_fraction=0.7`, `bagging_fraction=0.8`, early stopping on the *time-ordered*
  validation slice. Monotone constraints when economics dictate sign.
- XGBoost: `max_depth 4–8`, `eta 0.03–0.1`, `min_child_weight` high for noise.
- Feature importance: use permutation importance or SHAP on the held-out fold;
  gain-based importance on correlated features misleads.

## Optuna (offline)

```python
study = optuna.create_study(direction="maximize",
        storage="sqlite:///artifacts/optuna.db", study_name=name, load_if_exists=True)
```
Objective = mean walk-forward score minus 1 std (penalize instability). Budget trials;
log every trial to MLflow with the git sha and data snapshot hash.

## PyTorch on Apple Silicon

- `device = "mps" if torch.backends.mps.is_available() else "cpu"`; float32 (fp64 is
  unsupported on MPS, fp16 autocast support is partial — verify numerics).
- Determinism: seed `torch`, `numpy`, `random`; note MPS kernels are not all
  deterministic — record seeds AND torch version in MLflow.
- Small tabular/sequence models train fine on MPS; pin `num_workers=0` (fork issues).

## Experiment hygiene

Every run logs: git sha, data snapshot hash, feature list, split spec, seeds, metrics
per fold, artifact paths. An experiment that cannot be reproduced from its MLflow
record did not happen. Promote a model only with: out-of-sample metric + calibration
plot + SHAP summary + a ledger entry documenting the decision.
