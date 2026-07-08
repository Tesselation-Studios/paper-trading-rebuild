"""
Risk Management — spec-driven rebuild.

Composable risk gates. Each gate is a pure function:
    (context, action, timestamp=None) -> (granted: bool, reason: str)

Gates:
    CashGate       — can't spend > available cash
    PositionGate   — max N% portfolio in single position
    ExposureGate   — max N% total exposure
    PDTGate        — ≤N day trades in 5-day window
    HoursGate      — only trade 09:30–16:00 ET

RiskManager chains gates in order and evaluates trades.
"""

from src.risk.gates import CashGate, PositionGate, ExposureGate, PDTGate, HoursGate, ConvictionGate, SectorGate
from src.risk.manager import RiskManager
from src.circuit_breaker import AgentCircuitBreaker, get_breaker

__all__ = [
    "CashGate",
    "PositionGate",
    "ExposureGate",
    "PDTGate",
    "HoursGate",
    "ConvictionGate",
    "SectorGate",
    "RiskManager",
    "AgentCircuitBreaker",
    "get_breaker",
]
