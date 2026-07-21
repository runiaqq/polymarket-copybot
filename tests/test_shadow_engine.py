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
