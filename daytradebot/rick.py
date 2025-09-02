def position_size(entry: float, stop: float, equity: float, r_pct: float) -> int:
    risk_per_share = max(0.01, entry - stop)
    risk_dollars = max(0.0, equity * r_pct)
    return int(risk_dollars / risk_per_share) if risk_per_share > 0 else 0

def immediate_stop_hit(price: float, entry: float, max_loss_pct: float) -> bool:
    return price <= entry * (1 - max_loss_pct + 1e-12)
