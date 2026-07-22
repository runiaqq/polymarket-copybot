"""BP30 shadow performance report with a same-period donor benchmark."""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.db.session import get_supabase
from core.relayer import (
    detect_outcome_index,
    get_payout_numerator,
    is_condition_resolved,
)
from core.shadow_model import build_entry_variants, fee_usdc

DONOR = "0xf7f20c0f7e93a745d0cb064f5f62850d7b30d881"
PAGE_SIZE = 1000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BP30 shadow-mode performance report")
    parser.add_argument("--days", type=float, default=14.0)
    parser.add_argument("--since", help="ISO-8601 UTC lower bound")
    parser.add_argument("--until", help="ISO-8601 UTC upper bound")
    return parser.parse_args()


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc  # noqa: UP017 - production currently runs Python 3.10.
        )
    return parsed.astimezone(
        timezone.utc  # noqa: UP017 - production currently runs Python 3.10.
    )


def _period(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = (
        _as_utc(args.until)
        if args.until
        else datetime.now(
            timezone.utc  # noqa: UP017 - production currently runs Python 3.10.
        )
    )
    start = _as_utc(args.since) if args.since else end - timedelta(days=args.days)
    if start >= end:
        raise ValueError("--since must be earlier than --until")
    return start, end


def _paged(fetch_page: Callable[[int, int], list[dict]]) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        page = fetch_page(offset, offset + PAGE_SIZE - 1)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def _shadow_rows(start: datetime, end: datetime) -> list[dict]:
    sb = get_supabase()

    def fetch(low: int, high: int) -> list[dict]:
        return (
            sb.table("shadow_trades")
            .select(
                "asset,status,sim_shares,stake_usdc,fee_usdc,pnl_usdc,"
                "edge,time_left_sec,entered_at,variant,sim_fill_price,"
                "model_p,spot,open_price"
            )
            .gte("entered_at", start.isoformat())
            .lt("entered_at", end.isoformat())
            .order("entered_at")
            .range(low, high)
            .execute()
            .data
            or []
        )

    return _paged(fetch)


def _donor_rows(start: datetime, end: datetime) -> list[dict]:
    sb = get_supabase()

    def fetch(low: int, high: int) -> list[dict]:
        return (
            sb.table("trade_signals")
            .select("id,market_id,token_id,outcome,price,size_usdc,created_at")
            .eq("source_wallet", DONOR)
            .gte("created_at", start.isoformat())
            .lt("created_at", end.isoformat())
            .order("created_at")
            .range(low, high)
            .execute()
            .data
            or []
        )

    return _paged(fetch)


def _stats(rows: list[dict]) -> dict[str, float]:
    settled = [row for row in rows if row.get("status") in {"win", "loss"}]
    wins = sum(1 for row in settled if row["status"] == "win")
    stake = sum(float(row.get("stake_usdc") or 0) for row in settled)
    fees = sum(float(row.get("fee_usdc") or 0) for row in settled)
    gross = sum(
        (
            float(row.get("sim_shares") or 0) - float(row.get("stake_usdc") or 0)
            if row["status"] == "win"
            else -float(row.get("stake_usdc") or 0)
        )
        for row in settled
    )
    net = sum(float(row.get("pnl_usdc") or 0) for row in settled)
    return {
        "count": len(settled),
        "wins": wins,
        "stake": stake,
        "fees": fees,
        "gross": gross,
        "net": net,
        "winrate": wins / len(settled) if settled else 0.0,
        "gross_roi": gross / stake if stake else 0.0,
        "net_roi": net / stake if stake else 0.0,
    }


def _print_stats(label: str, rows: list[dict]) -> None:
    stats = _stats(rows)
    print(
        f"{label:<18} n={int(stats['count']):4d}  "
        f"WR={stats['winrate']:6.1%}  stake=${stats['stake']:9.2f}  "
        f"gross=${stats['gross']:+9.2f} ({stats['gross_roi']:+6.1%})  "
        f"fees=${stats['fees']:7.2f}  "
        f"net=${stats['net']:+9.2f} ({stats['net_roi']:+6.1%})"
    )


def _print_variant_stats(label: str, rows: list[dict]) -> None:
    settled = [row for row in rows if row.get("status") in {"win", "loss"}]
    stats = _stats(settled)
    average_fill = (
        sum(float(row.get("sim_fill_price") or 0) for row in settled) / len(settled)
        if settled
        else 0.0
    )
    average_model_p = (
        sum(float(row.get("model_p") or 0) for row in settled) / len(settled) if settled else 0.0
    )
    print(
        f"{label:<18} n={int(stats['count']):4d}  "
        f"WR={stats['winrate']:6.1%}  price={average_fill:.3f}  "
        f"model_p={average_model_p:6.1%}  "
        f"net=${stats['net']:+9.2f} ({stats['net_roi']:+6.1%})"
    )


def _bucket_label(value: float, boundaries: list[float], unit: str = "") -> str:
    ordered = sorted(boundaries)
    if not ordered:
        return "all"
    if value < ordered[0]:
        return f"<{ordered[0]:g}{unit}"
    for low, high in zip(ordered, ordered[1:]):
        if value < high:
            return f"{low:g}-{high:g}{unit}"
    return f">={ordered[-1]:g}{unit}"


def _print_breakdown(
    title: str,
    rows: list[dict],
    key: Callable[[dict], str],
) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    print(f"\n{title}")
    for label in sorted(grouped):
        _print_stats(label, grouped[label])


def _strike_distance_bucket(row: dict) -> str:
    spot = float(row.get("spot") or 0)
    open_price = float(row.get("open_price") or 0)
    if spot <= 0 or open_price <= 0:
        return "unknown"
    distance_bps = abs(math.log(spot / open_price)) * 10_000
    return _bucket_label(
        distance_bps,
        settings.shadow_report_strike_bins_bps,
        "bp",
    )


def _print_variant_breakdown(rows: list[dict]) -> None:
    configured = build_entry_variants(
        settings.shadow_entry_min_sec,
        settings.shadow_entry_max_sec,
        settings.shadow_variant_edges_sec,
    )
    configured_names = [variant[0] for variant in configured]
    observed_names = {str(row.get("variant") or "full") for row in rows}
    variant_names = configured_names + sorted(observed_names - set(configured_names))

    print("\nВарианты времени входа")
    for variant_name in variant_names:
        variant_rows = [row for row in rows if str(row.get("variant") or "full") == variant_name]
        _print_variant_stats(variant_name, variant_rows)

    print("\nВарианты по расстоянию до страйка")
    for variant_name in variant_names:
        variant_rows = [
            row
            for row in rows
            if str(row.get("variant") or "full") == variant_name
            and row.get("status") in {"win", "loss"}
        ]
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in variant_rows:
            grouped[_strike_distance_bucket(row)].append(row)
        print(f"  {variant_name}")
        for label in sorted(grouped):
            _print_variant_stats(label, grouped[label])


def _print_divergence_breakdown(rows: list[dict]) -> None:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        divergence = float(row.get("model_p") or 0) - float(row.get("sim_fill_price") or 0)
        grouped[_bucket_label(divergence, settings.shadow_report_divergence_bins)].append(row)

    print("\nПо расхождению model−price")
    for label in sorted(grouped):
        _print_variant_stats(label, grouped[label])


def _donor_benchmark(rows: list[dict]) -> list[dict]:
    benchmark: list[dict] = []
    resolution_cache: dict[tuple[str, str], bool | None] = {}
    for row in rows:
        condition_id = str(row.get("market_id") or "")
        token_id = str(row.get("token_id") or "")
        price = float(row.get("price") or 0)
        stake = float(row.get("size_usdc") or 0)
        if not condition_id or not token_id or not 0 < price < 1 or stake <= 0:
            continue
        cache_key = (condition_id, token_id)
        won = resolution_cache.get(cache_key)
        if cache_key not in resolution_cache:
            if not is_condition_resolved(condition_id):
                resolution_cache[cache_key] = None
                continue
            outcome_index = detect_outcome_index(condition_id, token_id)
            if outcome_index is None:
                resolution_cache[cache_key] = None
                continue
            held_payout = get_payout_numerator(condition_id, outcome_index)
            other_payout = get_payout_numerator(condition_id, 1 - outcome_index)
            if held_payout <= 0 and other_payout <= 0:
                resolution_cache[cache_key] = None
                continue
            won = held_payout > 0
            resolution_cache[cache_key] = won
        if won is None:
            continue
        shares = stake / price
        fee = fee_usdc(
            price,
            shares,
            fee_rate=settings.shadow_fee_rate,
            exponent=settings.shadow_fee_exponent,
        )
        gross = shares - stake if won else -stake
        benchmark.append(
            {
                "status": "win" if won else "loss",
                "sim_shares": shares,
                "stake_usdc": stake,
                "fee_usdc": fee,
                "pnl_usdc": gross - fee,
            }
        )
    return benchmark


def main() -> None:
    args = _parse_args()
    start, end = _period(args)
    rows = _shadow_rows(start, end)
    full_rows = [row for row in rows if str(row.get("variant") or "full") == "full"]
    settled = [row for row in full_rows if row.get("status") in {"win", "loss"}]
    open_count = sum(1 for row in full_rows if row.get("status") == "open")
    void_count = sum(1 for row in full_rows if row.get("status") == "void")

    print(f"Период: {start.isoformat()} — {end.isoformat()}")
    print(f"Shadow full: всего {len(full_rows)}, open {open_count}, void {void_count}")
    _print_stats("ВСЕГО SHADOW", settled)
    _print_breakdown("По активам", settled, lambda row: str(row.get("asset") or "?").upper())
    _print_breakdown(
        "По edge",
        settled,
        lambda row: _bucket_label(
            float(row.get("edge") or 0),
            settings.shadow_report_edge_bins,
        ),
    )
    _print_breakdown(
        "По времени до конца",
        settled,
        lambda row: _bucket_label(
            float(row.get("time_left_sec") or 0),
            settings.shadow_report_tau_bins_sec,
            "s",
        ),
    )
    _print_divergence_breakdown(settled)
    _print_variant_breakdown(rows)

    donor_signals = _donor_rows(start, end)
    donor = _donor_benchmark(donor_signals)
    print(f"\nДонор BTC: сигналов {len(donor_signals)}, рассчитано {len(donor)}")
    _print_stats("ДОНОР", donor)


if __name__ == "__main__":
    main()
