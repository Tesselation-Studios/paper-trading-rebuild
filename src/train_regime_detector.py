import logging

log = logging.getLogger("train_regime_detector")

"""
Train K-Means regime detector from Postgres market_data.bars_1d.

Usage:
    python -m src.train_regime_detector
    python -m src.train_regime_detector --k 4 --days 730
    python -m src.train_regime_detector --k 4 --days 730 --compare

Requires: sklearn, numpy, psycopg2 (already in project venv)
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.regime_detector import RegimeDetector

DB_URL = os.getenv("TRAIN_DB_URL", "postgresql://trader:@trading-db:5432/trading")


def fetch_spy_history(days: int = 730) -> list:
    """Fetch SPY OHLCV from Postgres market_data.bars_1d."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DB_URL)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT symbol, date, open, high, low, close, volume
            FROM market_data.bars_1d
            WHERE symbol = 'SPY'
            ORDER BY date ASC
            """
        )
        rows = cur.fetchall()
        cur.close()

        # Filter to last N days
        if len(rows) > days:
            rows = rows[-days:]

        result = []
        for row in rows:
            result.append({
                "symbol": row["symbol"],
                "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            })
        return result
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Train K-Means regime detector")
    parser.add_argument("--k", type=int, default=4, help="Number of clusters (default: 4)")
    parser.add_argument("--days", type=int, default=506, help="Days of history (default: max available)")
    parser.add_argument("--output", type=str,
                        default="/home/openclaw/data/regime_kmeans.pkl",
                        help="Model output path")
    parser.add_argument("--compare", action="store_true",
                        help="Compare against rule-based detector after training")
    args = parser.parse_args()

    print(f"Connecting to Postgres: trading-db:5432/trading")
    print(f"Fetching up to {args.days} days of SPY history from market_data.bars_1d...")
    data = fetch_spy_history(args.days)

    if not data:
        print("ERROR: No SPY data found in market_data.bars_1d")
        sys.exit(1)

    print(f"Got {len(data)} bars ({data[0]['date']} → {data[-1]['date']}).")
    print(f"Training K-Means (k={args.k})...")

    detector = RegimeDetector(k=args.k, model_path=args.output)
    detector.fit(data, symbols=["SPY"])

    # Compute class distribution
    from collections import Counter
    labels = []
    for i in range(len(data)):
        closes = [d["close"] for d in data[:i+1]]
        if i < 50:
            continue
        features = _compute_snapshot(data[:i+1], ["SPY"])
        try:
            result = detector.predict(features)
            labels.append(result.label)
        except Exception as e:
            log.warning("train_regime_detector: %s", e)

    dist = Counter(labels)
    total = sum(dist.values())

    print(f"\n{'='*60}")
    print(f"Model trained successfully!")
    print(f"  Samples: {len(data)}")
    print(f"  Features: {len(detector._feature_names)}")
    print(f"  Clusters (k={args.k}):")
    for cluster_id, label_name in detector._centroid_labels.items():
        count = dist.get(label_name, 0)
        pct = (count / total * 100) if total > 0 else 0
        print(f"    [{cluster_id}] {label_name:20s} → {count:4d} days ({pct:5.1f}%)")
    print(f"  Saved to: {args.output}")
    print(f"{'='*60}")

    # Print centroids for inspection
    print(f"\nCluster Centroids:")
    centroids = detector._get_centroids()
    for cluster_id, centroid in centroids.items():
        label = detector._centroid_labels.get(cluster_id, f"cluster_{cluster_id}")
        mom = centroid.get("SPY_mom_20d", 0)
        rsi = centroid.get("SPY_rsi_14", 0)
        atr = centroid.get("SPY_atr_pct", 0)
        vol = centroid.get("SPY_vol_trend", 0)
        print(f"  [{cluster_id}] {label:20s} | mom_20d={mom:+.4f}  rsi_14={rsi:.1f}  atr={atr:.4f}  vol_trend={vol:+.4f}")

    # Compare with rule-based if requested
    if args.compare:
        print(f"\n{'='*60}")
        print(f"Comparison with Rule-Based Detector")
        print(f"{'='*60}")
        try:
            compare_with_rule_based(data, detector)
        except Exception as e:
            print(f"  Comparison skipped: {e}")


def _compute_snapshot(data: list, symbols: list) -> dict:
    """Compute feature snapshot for the most recent bar. Mirrors RegimeDetector._extract_features."""
    detector = RegimeDetector(k=4)  # temp, just for feature extraction
    features, names = detector._extract_features(data, symbols)
    if not features:
        return {}
    return dict(zip(names, features[-1]))


def compare_with_rule_based(data: list, kmeans_detector: RegimeDetector):
    """Compare K-Means labels with a simple rule-based regime classifier."""
    closes = [d["close"] for d in data]
    labels_kmeans = []
    labels_rule = []

    for i in range(50, len(data)):
        # K-Means prediction
        features = _compute_snapshot(data[:i+1], ["SPY"])
        try:
            km = kmeans_detector.predict(features)
            labels_kmeans.append(km.label)
        except Exception:
            labels_kmeans.append("error")

        # Rule-based: simple trend/volatility heuristic
        window = closes[max(0, i-20):i+1]
        ret_20d = (window[-1] - window[0]) / window[0] if window[0] > 0 else 0
        returns = [window[j]/window[j-1]-1 for j in range(1, len(window))]
        vol = __import__("statistics").stdev(returns) if len(returns) > 1 else 0
        vol_annual = vol * (252 ** 0.5)

        ret_5d = (window[-1] - window[-5]) / window[-5] if len(window) >= 5 and window[-5] > 0 else 0

        if ret_20d > 0.03 and vol_annual < 0.25:
            labels_rule.append("momentum_bull")
        elif ret_20d < -0.03 and vol_annual < 0.25:
            labels_rule.append("momentum_bear")
        elif vol_annual > 0.30:
            labels_rule.append("volatility_spike")
        elif abs(ret_5d) < 0.005 and vol_annual < 0.12:
            labels_rule.append("low_vol_drift")
        else:
            labels_rule.append("mean_reversion")

    from collections import Counter
    km_dist = Counter(labels_kmeans)
    rule_dist = Counter(labels_rule)

    print(f"\n  K-Means distribution:")
    for label, count in km_dist.most_common():
        pct = count / len(labels_kmeans) * 100
        print(f"    {label:20s} → {count:4d} days ({pct:5.1f}%)")

    print(f"\n  Rule-based distribution:")
    for label, count in rule_dist.most_common():
        pct = count / len(labels_rule) * 100
        print(f"    {label:20s} → {count:4d} days ({pct:5.1f}%)")

    # Agreement rate
    matches = sum(1 for a, b in zip(labels_kmeans, labels_rule) if a == b)
    print(f"\n  Agreement: {matches}/{len(labels_kmeans)} ({matches/len(labels_kmeans)*100:.1f}%)")


if __name__ == "__main__":
    main()
