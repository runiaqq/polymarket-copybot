from types import SimpleNamespace
from typing import Any

from worker import shadow_engine


class FakeShadowTradesTable:
    def __init__(self, existing_keys: set[tuple[str, str]]) -> None:
        self.existing_keys = existing_keys
        self.filters: dict[str, Any] = {}
        self.operation = ""
        self.inserted_payloads: list[dict[str, Any]] = []

    def select(self, _columns: str) -> "FakeShadowTradesTable":
        self.operation = "select"
        self.filters = {}
        return self

    def eq(self, column: str, value: Any) -> "FakeShadowTradesTable":
        self.filters[column] = value
        return self

    def limit(self, _count: int) -> "FakeShadowTradesTable":
        return self

    def insert(self, payload: dict[str, Any]) -> "FakeShadowTradesTable":
        self.operation = "insert"
        self.inserted_payloads.append(payload)
        return self

    def execute(self) -> SimpleNamespace:
        if self.operation == "select":
            key = (
                str(self.filters.get("condition_id")),
                str(self.filters.get("variant")),
            )
            return SimpleNamespace(data=[{"id": 1}] if key in self.existing_keys else [])
        return SimpleNamespace(data=[])


class FakeSupabase:
    def __init__(self, table: FakeShadowTradesTable) -> None:
        self.shadow_trades = table

    def table(self, name: str) -> FakeShadowTradesTable:
        assert name == "shadow_trades"
        return self.shadow_trades


def test_insert_trade_deduplicates_exact_condition_variant(monkeypatch) -> None:
    table = FakeShadowTradesTable({("condition-1", "full")})
    monkeypatch.setattr(shadow_engine, "get_supabase", lambda: FakeSupabase(table))

    result = shadow_engine.ShadowEngine._insert_trade(
        {"condition_id": "condition-1", "variant": "full"}
    )

    assert result == "duplicate"
    assert table.inserted_payloads == []


def test_insert_trade_allows_another_variant_for_same_condition(monkeypatch) -> None:
    table = FakeShadowTradesTable({("condition-1", "full")})
    monkeypatch.setattr(shadow_engine, "get_supabase", lambda: FakeSupabase(table))
    payload = {"condition_id": "condition-1", "variant": "t20-30"}

    result = shadow_engine.ShadowEngine._insert_trade(payload)

    assert result == "inserted"
    assert table.inserted_payloads == [payload]


def test_open_trades_selects_every_row_is_signal_column() -> None:
    """BP52 regression: _row_is_signal recomputes calibrated edge from the
    resolution-loop rows. Live bug 08-25..31: model_p was missing from the
    _open_trades select, so every win/loss notice was silently dropped."""
    import inspect

    src = inspect.getsource(shadow_engine.ShadowEngine._open_trades)
    for col in ("model_p", "sim_fill_price", "edge", "spot", "open_price",
                "variant", "asset"):
        assert col in src, f"_open_trades select is missing {col}"


class TestSilentAssets:
    """BP50.1: per-asset spot silence watchdog (btc flowing must not mask
    starved alt subscriptions)."""

    def _engine(self) -> shadow_engine.ShadowEngine:
        return shadow_engine.ShadowEngine()

    def test_all_assets_ticking_reports_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr(
            shadow_engine.settings, "shadow_spot_asset_silence_sec", 600.0
        )
        engine = self._engine()
        for state in engine.spots.values():
            state.last_rx_monotonic = 10_000.0
        assert engine._silent_assets(10_100.0, connected_at=9_000.0) == []

    def test_starved_asset_reported_while_others_tick(self, monkeypatch) -> None:
        monkeypatch.setattr(
            shadow_engine.settings, "shadow_spot_asset_silence_sec", 600.0
        )
        engine = self._engine()
        for asset, state in engine.spots.items():
            state.last_rx_monotonic = 400.0 if asset == "eth" else 10_000.0
        assert engine._silent_assets(10_100.0, connected_at=400.0) == ["eth"]

    def test_fresh_connection_gets_grace_period(self, monkeypatch) -> None:
        # No asset has ever ticked, but the connection is younger than the
        # threshold — must not flap-reconnect before subscriptions warm up.
        monkeypatch.setattr(
            shadow_engine.settings, "shadow_spot_asset_silence_sec", 600.0
        )
        engine = self._engine()
        assert engine._silent_assets(10_100.0, connected_at=10_000.0) == []

    def test_disabled_when_threshold_zero(self, monkeypatch) -> None:
        monkeypatch.setattr(shadow_engine.settings, "shadow_spot_asset_silence_sec", 0.0)
        engine = self._engine()
        assert engine._silent_assets(99_999.0, connected_at=0.0) == []
