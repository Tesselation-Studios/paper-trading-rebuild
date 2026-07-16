#!/usr/bin/env python3
"""
regime_detector.py — K-Means market regime detection per SPEC #149.

Replaces the legacy rule-based classifier (TRENDING_UP/DOWN/HIGH_VOL/
MEAN_REVERTING heuristic) with a statistically grounded K-Means
clustering model trained on 6 months of 5-min SPY bars.

Architecture:
  1. Fetch 6 months of 5-min bars for SPY (market proxy)
  2. Engineer 10 features capturing trend, volatility, volume, and momentum
  3. Fit K-Means (k=6) to cluster observations into market regimes
  4. Label clusters by their feature profiles (trending, ranging, volatile, etc.)
  5. Save model to disk; load for inference at tick time
  6. Output regime label + confidence score compatible with tick_prompt.py

Usage:
    # Train and save model
    python src/regime_detector.py --train

    # Classify latest regime (online inference)
    python src/regime_detector.py --classify

    # Compare rule-based vs K-Means on historical data
    python src/regime_detector.py --compare

    # From code:
    from src.regime_detector import RegimeDetector
    detector = RegimeDetector()
    detector.train()  # or detector.load()
    result = detector.classify_latest()
    print(result["regime"], result["confidence"])
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yfinance as yf
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

# Suppress yfinance FutureWarnings
warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
MODEL_PATH = STATE_DIR / "kmeans_regime_model.pkl"
SCALER_PATH = STATE_DIR / "kmeans_regime_scaler.pkl"
METADATA_PATH = STATE_DIR / "kmeans_regime_metadata.json"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MARKET_PROXY = "SPY"
DEFAULT_LOOKBACK_MONTHS = 6
DEFAULT_INTERVAL = "5m"
N_CLUSTERS = 6
MIN_TRAIN_SAMPLES = 500

# Cluster label mapping — derived from feature profiles post-training
# These are assigned after clustering by analyzing cluster centroids
REGIME_LABELS: dict[int, str] = {
    0: "TRENDING_UP",
    1: "TRENDING_DOWN",
    2: "HIGH_VOLATILITY",
    3: "CALM",
    4: "MEAN_REVERTING",
    5: "CRASH",
}

# Legacy rule-based regime names (for comparison)
LEGACY_REGIMES = {"TRENDING_UP", "TRENDING_DOWN", "HIGH_VOL", "MEAN_REVERTING"}


@dataclass
class RegimeResult:
    """Result of regime classification."""
    regime: str
    confidence: float
    cluster_id: int
    timestamp: str
    features: dict[str, float]
    feature_values: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer 10 features for K-Means regime detection.

    Args:
        df: DataFrame with columns: Open, High, Low, Close, Volume
            (single-level columns, not MultiIndex)

    Returns:
        DataFrame with 10 engineered features (rows with NaNs dropped).

    Features:
        1.  return_5      — 5-period log return
        2.  volatility_20 — 20-period std of returns (annualized proxy)
        3.  rsi_14        — 14-period RSI
        4.  volume_ratio  — Volume / 20-period avg volume
        5.  sma20_dist    — (Close - SMA20) / SMA20
        6.  sma50_dist    — (Close - SMA50) / SMA50
        7.  trend_sma     — SMA20 / SMA50 - 1 (trend direction & strength)
        8.  bb_position   — (Close - BB_lower) / (BB_upper - BB_lower)
        9.  atr_ratio     — ATR(14) / Close
        10. momentum_10   — 10-period log return
    """
    close = df["Close"].astype(float).squeeze()
    high = df["High"].astype(float).squeeze()
    low = df["Low"].astype(float).squeeze()
    volume = df["Volume"].astype(float).squeeze()

    features = pd.DataFrame(index=df.index)

    # 1. 5-period return
    features["return_5"] = np.log(close / close.shift(5))

    # 2. 20-period volatility (annualized daily, scaled for 5-min)
    log_ret = np.log(close / close.shift(1))
    features["volatility_20"] = log_ret.rolling(20).std()

    # 3. 14-period RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14, min_periods=14).mean()
    avg_loss = loss.rolling(14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    features["rsi_14"] = 100.0 - (100.0 / (1.0 + rs))

    # 4. Volume ratio
    features["volume_ratio"] = volume / volume.rolling(20).mean()

    # 5. Price distance from SMA20
    sma20 = close.rolling(20).mean()
    features["sma20_dist"] = (close - sma20) / sma20

    # 6. Price distance from SMA50
    sma50 = close.rolling(50).mean()
    features["sma50_dist"] = (close - sma50) / sma50

    # 7. SMA trend indicator
    features["trend_sma"] = sma20 / sma50 - 1.0

    # 8. Bollinger Band position
    bb_std = close.rolling(20).std()
    bb_upper = sma20 + 2 * bb_std
    bb_lower = sma20 - 2 * bb_std
    bb_range = bb_upper - bb_lower
    features["bb_position"] = np.where(
        bb_range > 0, (close - bb_lower) / bb_range, 0.5
    )

    # 9. ATR ratio (14-period)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = true_range.rolling(14).mean()
    features["atr_ratio"] = atr14 / close

    # 10. 10-period momentum
    features["momentum_10"] = np.log(close / close.shift(10))

    # Drop rows with NaN from rolling calculations
    return features.dropna()


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_market_data(
    ticker: str = MARKET_PROXY,
    months: int = DEFAULT_LOOKBACK_MONTHS,
    interval: str = DEFAULT_INTERVAL,
) -> pd.DataFrame:
    """Fetch historical market data via yfinance.

    Args:
        ticker: Market proxy ticker (default: SPY).
        months: Lookback period in months.
        interval: Bar interval (default: 5m).

    Returns:
        DataFrame with single-level columns: Open, High, Low, Close, Volume.

    Raises:
        RuntimeError: If data download fails or returns insufficient bars.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=months * 31)

    try:
        data = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        raise RuntimeError(f"yfinance download failed for {ticker}: {e}") from e

    if data is None or (hasattr(data, "empty") and data.empty):
        raise RuntimeError(f"No data returned for {ticker} ({months}mo, {interval})")

    # Flatten MultiIndex columns if present
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    # Ensure required columns exist
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(data.columns)
    if missing:
        raise RuntimeError(f"Missing columns in data: {missing}")

    if len(data) < MIN_TRAIN_SAMPLES:
        raise RuntimeError(
            f"Insufficient data: {len(data)} rows (need >= {MIN_TRAIN_SAMPLES})"
        )

    return data


# ---------------------------------------------------------------------------
# Cluster labeling — map cluster IDs to human-readable regimes
# ---------------------------------------------------------------------------

def label_clusters(
    kmeans: KMeans,
    feature_names: list[str],
) -> dict[int, str]:
    """Analyze cluster centroids and assign human-readable regime labels.

    Rules (applied to normalized centroids):
      - High volatility_20 + high atr_ratio + extreme return_5 → CRASH
      - High volatility_20 + high atr_ratio → HIGH_VOLATILITY
      - High momentum_10 + positive trend_sma + high rsi_14 → TRENDING_UP
      - Low momentum_10 + negative trend_sma + low rsi_14 → TRENDING_DOWN
      - Low volatility_20 + near-zero trend_sma → CALM
      - Near-zero momentum + bb_position ≈ 0.5 → MEAN_REVERTING

    Args:
        kmeans: Fitted KMeans model.
        feature_names: List of feature names in order.

    Returns:
        Dict mapping cluster_id → regime label string.
    """
    centers = kmeans.cluster_centers_  # shape: (n_clusters, n_features)
    n_clusters = centers.shape[0]

    # Build feature index map
    idx = {name: i for i, name in enumerate(feature_names)}

    labels: dict[int, str] = {}
    used_regimes: set[str] = set()

    for cid in range(n_clusters):
        center = centers[cid]

        vol = center[idx["volatility_20"]]
        atr = center[idx["atr_ratio"]]
        mom = center[idx["momentum_10"]]
        trend = center[idx["trend_sma"]]
        rsi = center[idx["rsi_14"]]
        ret5 = center[idx["return_5"]]
        bb = center[idx["bb_position"]]

        # Classification rules (ordered by priority)
        if vol > 0.8 and atr > 0.8 and ret5 < -0.5:
            label = "CRASH"
        elif vol > 0.6 and atr > 0.5:
            label = "HIGH_VOLATILITY"
        elif mom > 0.3 and trend > 0.2 and rsi > 0.3:
            label = "TRENDING_UP"
        elif mom < -0.3 and trend < -0.2 and rsi < -0.5:
            label = "TRENDING_DOWN"
        elif abs(vol) < 0.3 and abs(trend) < 0.15:
            label = "CALM"
        elif abs(mom) < 0.3 and 0.3 < bb < 0.7 and abs(trend) < 0.2:
            label = "MEAN_REVERTING"
        else:
            # Fallback: assign based on dominant feature
            dominant = np.argmax(np.abs(center))
            if dominant == idx["momentum_10"]:
                label = "TRENDING_UP" if mom > 0 else "TRENDING_DOWN"
            elif dominant == idx["volatility_20"]:
                label = "HIGH_VOLATILITY"
            elif dominant == idx["trend_sma"]:
                label = "TRENDING_UP" if trend > 0 else "TRENDING_DOWN"
            else:
                label = "CALM"

        # Ensure uniqueness — use alternative labels for duplicates
        base_label = label
        duplicates: dict[str, list[str]] = {
            "TRENDING_UP": ["BULLISH", "MOMENTUM", "STRONG_TREND"],
            "TRENDING_DOWN": ["BEARISH", "WEAKNESS", "DECLINE"],
            "HIGH_VOLATILITY": ["VOLATILE", "CHOPPY", "TURBULENT"],
            "CALM": ["QUIET", "LOW_VOL", "STABLE"],
            "MEAN_REVERTING": ["OSCILLATING", "RANGE_BOUND", "SIDEWAYS"],
            "CRASH": ["PANIC", "SELLOFF", "FEAR"],
        }
        if label in used_regimes:
            alts = duplicates.get(base_label, [])
            for alt in alts:
                if alt not in used_regimes:
                    label = alt
                    break
            else:
                suffix = 2
                while f"{base_label}_{suffix}" in used_regimes:
                    suffix += 1
                label = f"{base_label}_{suffix}"
        used_regimes.add(label)
        labels[cid] = label

    return labels


# ---------------------------------------------------------------------------
# Rule-based classifier (for comparison)
# ---------------------------------------------------------------------------

def classify_rule_based(row: pd.Series) -> str:
    """Legacy rule-based regime classifier.

    Uses simple thresholds on a few features to assign regimes.
    This is the heuristic being replaced by K-Means per issue #149.

    Returns one of: TRENDING_UP, TRENDING_DOWN, HIGH_VOL, MEAN_REVERTING
    """
    rsi = row.get("rsi_14", 50)
    trend = row.get("trend_sma", 0)
    vol_ratio = row.get("volume_ratio", 1)
    atr = row.get("atr_ratio", 0.01)

    if atr > 0.03 or vol_ratio > 2.0:
        return "HIGH_VOL"
    elif trend > 0.003 and rsi > 55:
        return "TRENDING_UP"
    elif trend < -0.003 and rsi < 45:
        return "TRENDING_DOWN"
    else:
        return "MEAN_REVERTING"


# ---------------------------------------------------------------------------
# RegimeDetector — main class
# ---------------------------------------------------------------------------

class RegimeDetector:
    """K-Means market regime detector.

    Trains on SPY 5-min bars, classifies current market state into
    one of 6 regimes: TRENDING_UP, TRENDING_DOWN, HIGH_VOLATILITY,
    CALM, MEAN_REVERTING, or CRASH.

    Model is persisted to disk for fast inference without retraining.
    """

    def __init__(
        self,
        ticker: str = MARKET_PROXY,
        n_clusters: int = N_CLUSTERS,
        model_dir: Path = STATE_DIR,
        random_state: int = 42,
    ):
        self.ticker = ticker
        self.n_clusters = n_clusters
        self.model_dir = model_dir
        self.model_path = model_dir / "kmeans_regime_model.pkl"
        self.scaler_path = model_dir / "kmeans_regime_scaler.pkl"
        self.metadata_path = model_dir / "kmeans_regime_metadata.json"
        self.random_state = random_state

        self.kmeans: Optional[KMeans] = None
        self.scaler: Optional[StandardScaler] = None
        self.feature_names: list[str] = []
        self.cluster_labels: dict[int, str] = {}
        self.is_trained: bool = False
        self._training_data: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        months: int = DEFAULT_LOOKBACK_MONTHS,
        interval: str = DEFAULT_INTERVAL,
        force: bool = False,
    ) -> dict[str, Any]:
        """Train the K-Means model on historical market data.

        Args:
            months: Lookback period in months.
            interval: Bar interval (e.g., '5m').
            force: If True, retrain even if saved model exists.

        Returns:
            Dict with training metadata (n_samples, inertia, cluster_sizes, etc.).
        """
        # 1. Fetch data
        print(f"[regime_detector] Fetching {months}mo of {interval} bars for {self.ticker}...")
        data = fetch_market_data(self.ticker, months=months, interval=interval)
        print(f"[regime_detector] Downloaded {len(data)} bars from {data.index[0]} to {data.index[-1]}")

        # 2. Engineer features
        features_df = engineer_features(data)
        if len(features_df) < MIN_TRAIN_SAMPLES:
            raise RuntimeError(
                f"Insufficient feature rows after NaN drop: {len(features_df)} "
                f"(need >= {MIN_TRAIN_SAMPLES})"
            )
        self.feature_names = list(features_df.columns)
        print(f"[regime_detector] Engineered {len(self.feature_names)} features: {self.feature_names}")
        print(f"[regime_detector] Training samples: {len(features_df)}")

        # 3. Scale features
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(features_df.values)

        # 4. Fit K-Means
        self.kmeans = KMeans(
            n_clusters=self.n_clusters,
            random_state=self.random_state,
            n_init=10,
            max_iter=300,
        )
        self.kmeans.fit(X_scaled)

        # 5. Label clusters
        self.cluster_labels = label_clusters(self.kmeans, self.feature_names)
        self.is_trained = True
        self._training_data = features_df

        # 6. Persist model
        self.save()

        # 7. Build metadata
        cluster_sizes = np.bincount(self.kmeans.labels_)
        metadata = {
            "ticker": self.ticker,
            "n_clusters": self.n_clusters,
            "n_samples": len(features_df),
            "features": self.feature_names,
            "inertia": float(self.kmeans.inertia_),
            "cluster_labels": self.cluster_labels,
            "cluster_sizes": {int(i): int(s) for i, s in enumerate(cluster_sizes)},
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "data_start": str(data.index[0]),
            "data_end": str(data.index[-1]),
        }

        # Print cluster summary
        print(f"\n[regime_detector] Training complete. Clusters:")
        for cid, label in sorted(self.cluster_labels.items()):
            size = cluster_sizes[cid] if cid < len(cluster_sizes) else 0
            pct = size / len(features_df) * 100
            print(f"  Cluster {cid}: {label:20s} ({size:6d} samples, {pct:.1f}%)")
        print(f"  Inertia: {self.kmeans.inertia_:.2f}")

        return metadata

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Save model, scaler, and metadata to disk."""
        self.model_dir.mkdir(parents=True, exist_ok=True)

        with open(self.model_path, "wb") as f:
            pickle.dump(self.kmeans, f)

        with open(self.scaler_path, "wb") as f:
            pickle.dump(self.scaler, f)

        metadata = {
            "ticker": self.ticker,
            "n_clusters": self.n_clusters,
            "feature_names": self.feature_names,
            "cluster_labels": self.cluster_labels,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(self.metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        print(f"[regime_detector] Model saved to {self.model_dir}")

    def load(self) -> bool:
        """Load pre-trained model from disk.

        Returns:
            True if model loaded successfully, False otherwise.
        """
        if not self.model_path.exists() or not self.scaler_path.exists():
            return False

        try:
            with open(self.model_path, "rb") as f:
                self.kmeans = pickle.load(f)
            with open(self.scaler_path, "rb") as f:
                self.scaler = pickle.load(f)
            if self.metadata_path.exists():
                with open(self.metadata_path) as f:
                    meta = json.load(f)
                    self.feature_names = meta.get("feature_names", [])
                    self.cluster_labels = {
                        int(k): v for k, v in meta.get("cluster_labels", {}).items()
                    }
            self.is_trained = True
            return True
        except (pickle.PickleError, json.JSONDecodeError, OSError) as e:
            print(f"[regime_detector] Failed to load model: {e}", file=sys.stderr)
            return False

    def is_model_saved(self) -> bool:
        """Check if a saved model exists on disk."""
        return self.model_path.exists() and self.scaler_path.exists()

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def classify(
        self,
        features: np.ndarray | pd.DataFrame | dict[str, float],
    ) -> RegimeResult:
        """Classify a single observation into a market regime.

        Args:
            features: One row of the 10 engineered features.
                      Can be a numpy array (same order as feature_names),
                      a DataFrame row, or a dict of feature_name → value.

        Returns:
            RegimeResult with regime label, confidence, and cluster info.

        Raises:
            RuntimeError: If model is not trained/loaded.
            ValueError: If features don't match expected feature_names.
        """
        if not self.is_trained or self.kmeans is None or self.scaler is None:
            raise RuntimeError("Model not trained. Call train() or load() first.")

        # Convert features to numpy array in correct order
        if isinstance(features, dict):
            try:
                feature_vec = np.array(
                    [[features[name] for name in self.feature_names]]
                )
                feature_dict = features
            except KeyError as e:
                raise ValueError(f"Missing feature: {e}") from e
        elif isinstance(features, pd.DataFrame):
            feature_vec = features[self.feature_names].values.reshape(1, -1)
            feature_dict = features[self.feature_names].iloc[0].to_dict()
        else:
            feature_vec = np.array(features).reshape(1, -1)
            if feature_vec.shape[1] != len(self.feature_names):
                raise ValueError(
                    f"Expected {len(self.feature_names)} features, got {feature_vec.shape[1]}"
                )
            feature_dict = {
                name: float(feature_vec[0, i])
                for i, name in enumerate(self.feature_names)
            }

        # Scale and predict
        X_scaled = self.scaler.transform(feature_vec)
        cluster_id = int(self.kmeans.predict(X_scaled)[0])

        # Compute confidence as inverse distance to cluster center
        # Lower distance → higher confidence
        center = self.kmeans.cluster_centers_[cluster_id]
        distance = np.linalg.norm(X_scaled[0] - center)

        # Normalize confidence: distance to this center vs distances to all centers
        all_distances = np.linalg.norm(X_scaled[0] - self.kmeans.cluster_centers_, axis=1)
        min_dist = np.min(all_distances)
        second_min = np.partition(all_distances, 1)[1] if len(all_distances) > 1 else min_dist + 1

        # Confidence: how much closer to assigned center vs second-closest
        if second_min > 0 and min_dist < second_min:
            raw_conf = 1.0 - (min_dist / second_min)
            # Scale to 0.5-1.0 range (never below 0.5 — always some confidence)
            confidence = 0.5 + raw_conf * 0.5
        else:
            confidence = 0.5

        confidence = float(np.clip(confidence, 0.0, 1.0))

        regime = self.cluster_labels.get(cluster_id, f"REGIME_{cluster_id}")

        return RegimeResult(
            regime=regime,
            confidence=round(confidence, 4),
            cluster_id=cluster_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            features=feature_dict,
            feature_values=feature_dict,
        )

    def classify_latest(
        self,
        ticker: Optional[str] = None,
        lookback_bars: int = 60,
        interval: str = DEFAULT_INTERVAL,
    ) -> RegimeResult:
        """Classify current market regime using most recent bars.

        Fetches the latest bars, engineers features, and classifies
        the most recent complete observation.

        Args:
            ticker: Ticker to classify (defaults to self.ticker).
            lookback_bars: Number of recent bars to fetch (must be >= 50
                           for SMA50 calculation).
            interval: Bar interval.

        Returns:
            RegimeResult for the latest observation.
        """
        ticker = ticker or self.ticker

        # Fetch recent data (need enough for rolling windows)
        days_needed = max(10, lookback_bars // 78 + 2)  # ~78 5-min bars/day
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_needed)

        data = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=interval,
            progress=False,
            auto_adjust=True,
        )

        if data is None or (hasattr(data, "empty") and data.empty):
            raise RuntimeError(f"No recent data for {ticker}")

        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        features_df = engineer_features(data)

        if len(features_df) == 0:
            raise RuntimeError("No feature rows after engineering (need >= 50 bars)")

        # Classify the most recent row
        latest_features = features_df.iloc[-1:]
        return self.classify(latest_features)

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def compare_with_rule_based(
        self,
        months: int = DEFAULT_LOOKBACK_MONTHS,
        interval: str = DEFAULT_INTERVAL,
    ) -> pd.DataFrame:
        """Compare K-Means vs rule-based classifications over historical data.

        Returns:
            DataFrame with columns: timestamp, kmeans_regime, rule_based_regime,
            agreement (bool), and all 10 features.
        """
        if not self.is_trained or self.kmeans is None or self.scaler is None:
            raise RuntimeError("Model not trained. Call train() first.")

        data = fetch_market_data(self.ticker, months=months, interval=interval)
        features_df = engineer_features(data)

        results = []
        for idx in range(len(features_df)):
            row = features_df.iloc[idx : idx + 1]
            row_series = features_df.iloc[idx]

            # K-Means classification
            km_result = self.classify(row)

            # Rule-based classification
            rb_regime = classify_rule_based(row_series)

            results.append({
                "timestamp": str(features_df.index[idx]),
                "kmeans_regime": km_result.regime,
                "rule_based_regime": rb_regime,
                "agreement": km_result.regime == rb_regime or
                              rb_regime in km_result.regime or
                              km_result.regime in rb_regime,
                "kmeans_confidence": km_result.confidence,
                **{f"f_{name}": row_series[name] for name in self.feature_names},
            })

        return pd.DataFrame(results)

    def print_comparison_summary(self, comparison_df: pd.DataFrame) -> None:
        """Print a summary of K-Means vs rule-based comparison."""
        total = len(comparison_df)
        agreement = comparison_df["agreement"].sum()
        agreement_pct = (agreement / total * 100) if total > 0 else 0

        print(f"\n{'='*70}")
        print(f"  K-Means vs Rule-Based Regime Comparison")
        print(f"  Ticker: {self.ticker} | Samples: {total}")
        print(f"{'='*70}")
        print(f"\n  Agreement rate: {agreement}/{total} ({agreement_pct:.1f}%)")

        # Regime distribution
        print(f"\n  K-Means regime distribution:")
        km_counts = comparison_df["kmeans_regime"].value_counts()
        for regime, count in km_counts.items():
            print(f"    {regime:20s}: {count:6d} ({count/total*100:5.1f}%)")

        print(f"\n  Rule-based regime distribution:")
        rb_counts = comparison_df["rule_based_regime"].value_counts()
        for regime, count in rb_counts.items():
            print(f"    {regime:20s}: {count:6d} ({count/total*100:5.1f}%)")

        # Disagreement breakdown
        disagreements = comparison_df[~comparison_df["agreement"]]
        if len(disagreements) > 0:
            print(f"\n  Top disagreements (K-Means → Rule-Based):")
            pairs = (
                disagreements.groupby(["kmeans_regime", "rule_based_regime"])
                .size()
                .sort_values(ascending=False)
                .head(10)
            )
            for (km, rb), count in pairs.items():
                print(f"    {km:20s} → {rb:20s}: {count:5d}")

        # Average confidence
        avg_conf = comparison_df["kmeans_confidence"].mean()
        print(f"\n  Average K-Means confidence: {avg_conf:.3f}")

        print(f"\n{'='*70}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_train(args: argparse.Namespace) -> int:
    """Train and save the K-Means regime detector."""
    detector = RegimeDetector(
        ticker=args.ticker,
        n_clusters=args.clusters,
    )

    if detector.is_model_saved() and not args.force:
        print(
            f"[regime_detector] Model already exists at {detector.model_path}. "
            f"Use --force to retrain."
        )
        return 0

    try:
        metadata = detector.train(
            months=args.months,
            interval=args.interval,
            force=args.force,
        )
        print(f"\n[regime_detector] Training complete.")
        if args.verbose:
            print(json.dumps(metadata, indent=2, default=str))
        return 0
    except Exception as e:
        print(f"[regime_detector] Training failed: {e}", file=sys.stderr)
        return 1


def cmd_classify(args: argparse.Namespace) -> int:
    """Classify the current market regime."""
    detector = RegimeDetector(ticker=args.ticker, n_clusters=args.clusters)

    if not detector.load():
        print(
            "[regime_detector] No trained model found. Run --train first.",
            file=sys.stderr,
        )
        return 1

    try:
        result = detector.classify_latest(
            ticker=args.ticker,
            interval=args.interval,
        )
        output = {
            "regime": result.regime,
            "confidence": result.confidence,
            "cluster_id": result.cluster_id,
            "timestamp": result.timestamp,
        }
        print(json.dumps(output, indent=2))
        return 0
    except Exception as e:
        print(f"[regime_detector] Classification failed: {e}", file=sys.stderr)
        return 1


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare K-Means vs rule-based regime classification."""
    detector = RegimeDetector(ticker=args.ticker, n_clusters=args.clusters)

    if not detector.load():
        if detector.is_model_saved():
            print(
                "[regime_detector] Failed to load model. Run --train first.",
                file=sys.stderr,
            )
            return 1
        # Auto-train if no model exists
        print("[regime_detector] No model found. Training first...")
        detector.train(months=args.months, interval=args.interval)

    try:
        comparison_df = detector.compare_with_rule_based(
            months=args.months,
            interval=args.interval,
        )
        detector.print_comparison_summary(comparison_df)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            comparison_df.to_csv(output_path, index=False)
            print(f"  Comparison data saved to {output_path}")

        return 0
    except Exception as e:
        print(f"[regime_detector] Comparison failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="regime_detector",
        description="K-Means market regime detection for paper trading.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-command")

    # --train
    p_train = subparsers.add_parser("train", help="Train K-Means regime detector")
    p_train.add_argument("--ticker", default=MARKET_PROXY, help="Market proxy ticker")
    p_train.add_argument("--months", type=int, default=DEFAULT_LOOKBACK_MONTHS,
                         help="Lookback months for training")
    p_train.add_argument("--interval", default=DEFAULT_INTERVAL, help="Bar interval")
    p_train.add_argument("--clusters", type=int, default=N_CLUSTERS,
                         help="Number of clusters")
    p_train.add_argument("--force", action="store_true", help="Force retrain")
    p_train.add_argument("--verbose", action="store_true", help="Verbose output")

    # --classify
    p_classify = subparsers.add_parser("classify", help="Classify current regime")
    p_classify.add_argument("--ticker", default=MARKET_PROXY, help="Ticker to classify")
    p_classify.add_argument("--clusters", type=int, default=N_CLUSTERS,
                            help="Number of clusters (must match trained model)")
    p_classify.add_argument("--interval", default=DEFAULT_INTERVAL, help="Bar interval")

    # --compare
    p_compare = subparsers.add_parser("compare", help="Compare K-Means vs rule-based")
    p_compare.add_argument("--ticker", default=MARKET_PROXY, help="Market proxy ticker")
    p_compare.add_argument("--months", type=int, default=DEFAULT_LOOKBACK_MONTHS,
                           help="Lookback months")
    p_compare.add_argument("--interval", default=DEFAULT_INTERVAL, help="Bar interval")
    p_compare.add_argument("--clusters", type=int, default=N_CLUSTERS,
                           help="Number of clusters")
    p_compare.add_argument("--output", help="Save comparison CSV to path")

    args = parser.parse_args(argv)

    if args.command == "train":
        return cmd_train(args)
    elif args.command == "classify":
        return cmd_classify(args)
    elif args.command == "compare":
        return cmd_compare(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
