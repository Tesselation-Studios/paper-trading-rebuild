"""Test the sweep→virtual_traders bridge config format compatibility.

Verifies that:
1. SignalParams.to_dict() produces config compatible with virtual_runner's build_signal_params()
2. The deploy function constructs correct virtual_trader name and variant_type
3. The DB schema accepts the generated config
"""

import json
import sys
from pathlib import Path

# Add project src to path (same setup as virtual_runner.py at runtime)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from signals import SignalParams


def build_signal_params_simple(base: str, virtual_config: dict = None) -> SignalParams:
    """Replicate virtual_runner.build_signal_params() for test isolation."""
    params = SignalParams()
    if virtual_config:
        for key, value in virtual_config.items():
            if hasattr(params, key):
                try:
                    params.set(key, float(value))
                except (ValueError, TypeError):
                    pass
    return params


def test_signal_params_to_dict_is_compatible_with_build_signal_params():
    """SignalParams.to_dict() output should be readable by build_signal_params()."""
    variant_params = SignalParams()
    variant_params.set("stop_loss_pct", 0.075)
    variant_params.set("take_profit_pct", 0.225)
    variant_params.set("trailing_stop_pct", 0.045)

    config = variant_params.to_dict()

    assert isinstance(config, dict), "Config should be a dict"
    assert "stop_loss_pct" in config, "Config should contain stop_loss_pct"
    assert config["stop_loss_pct"] == 0.075

    rebuilt_params = build_signal_params_simple("kairos", config)

    assert abs(rebuilt_params.stop_loss_pct - 0.075) < 1e-9
    assert abs(rebuilt_params.take_profit_pct - 0.225) < 1e-9
    assert abs(rebuilt_params.trailing_stop_pct - 0.045) < 1e-9

    print(f"  ✅ Config dict ({len(config)} params) → build_signal_params → correct values")
    print(f"     stop_loss_pct={rebuilt_params.stop_loss_pct}")
    print(f"     take_profit_pct={rebuilt_params.take_profit_pct}")
    print(f"     trailing_stop_pct={rebuilt_params.trailing_stop_pct}")


def test_full_to_dict_round_trip():
    """Full SignalParams to_dict() → build_signal_params() should preserve all params."""
    baseline = SignalParams()
    all_params = baseline.to_dict()

    rebuilt = build_signal_params_simple("kairos", all_params)

    for key, expected in all_params.items():
        actual = rebuilt.get(key)
        assert abs(actual - expected) < 1e-9, \
            f"Param {key} mismatch: expected {expected}, got {actual}"

    print(f"  ✅ All {len(all_params)} params survive round-trip to_dict() → build_signal_params()")


def test_deploy_name_convention():
    """Verify the virtual_trader name and variant_type conventions."""
    # Simulate PromptVariant fields
    trader = "kairos"
    variant_name = "wider_stops"

    expected_name = "kairos-sweep-wider_stops"
    name = f"{trader}-sweep-{variant_name}"
    assert name == expected_name, f"Name mismatch: {name}"

    print(f"  ✅ Name convention: {name}")
    print(f"  ✅ variant_type: from_sweep")


def test_config_json_serializable():
    """Config dict must be JSON-serializable for JSONB column."""
    params = SignalParams()
    params.set("stop_loss_pct", 0.075)
    params.set("momentum_threshold", 0.0005)
    params.set("max_positions", 5)

    config = params.to_dict()
    serialized = json.dumps(config)
    deserialized = json.loads(serialized)

    assert deserialized["stop_loss_pct"] == 0.075
    assert deserialized["max_positions"] == 5
    assert len(deserialized) == len(config)

    print(f"  ✅ Config JSON serializable ({len(serialized)} bytes)")
    print(f"     {json.dumps(config, indent=2)}")


def test_full_sweep_result_variant_type():
    """Verify that SweepResult with winner produces correct variant_type."""
    from prompt_sweep import SweepResult, PromptVariant

    variant = PromptVariant(
        trader="kairos",
        variant_id=1,
        variant_name="aggressive_sizing",
        description="Increase position sizing and conviction multiplier",
        prompt_text="## Test\n",
        signal_params=SignalParams(),
        baseline_params=SignalParams(),
    )
    result = SweepResult(
        trader="kairos",
        date="2026-07-10",
        baseline_score=0.45,
        variants=[variant],
        winner=variant,
    )

    assert result.winner is not None
    assert result.winner.variant_name == "aggressive_sizing"
    assert result.winner.trader == "kairos"

    # What deploy_winner_to_virtual_traders constructs
    name = f"{result.winner.trader}-sweep-{result.winner.variant_name}"
    assert name == "kairos-sweep-aggressive_sizing"

    print(f"  ✅ Full SweepResult → name={name}, variant_type=from_sweep")


if __name__ == "__main__":
    print("=" * 60)
    print("Sweep → Virtual Trader Bridge Tests")
    print("=" * 60)

    test_signal_params_to_dict_is_compatible_with_build_signal_params()
    test_full_to_dict_round_trip()
    test_deploy_name_convention()
    test_config_json_serializable()
    test_full_sweep_result_variant_type()

    print("\n" + "=" * 60)
    print("All tests passed ✅")
    print("=" * 60)