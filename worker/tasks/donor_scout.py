"""
Blueprint 48 — donor scout Celery tasks: harvest → score → digest.

harvest_wallet_sightings (60s): passive record-only revival of the Model-A
whale feed — every large taker BUY inside OUR fast-market universe lands in
wallet_sightings. No copies, no notifications: just tape.

score_donor_candidates (nightly): wallets with enough sightings get an
activity-feed anti-MM fingerprint + resolution WR on their sighted markets,
land in donor_candidates, and the best fill free probation seats
(tracked_wallets.mode='candidate' — signals recorded, never copied).

donor_scout_digest (weekly): probation results in real would-be dollars,
pushed to admins with promote/dismiss buttons. Live donors are never
auto-removed — demotion is BP42's pause or a human tap here.
"""

import time
from datetime import datetime, timedelta, timezone

import structlog

from core.config import settings
from worker.celery_app import celery_app

log = structlog.get_logger(__name__)

# In-memory tx dedup to keep the 60s harvest from hammering the unique index;
# the DB constraint is the durable guard across restarts.
_seen_tx: set[str] = set()
_SEEN_TX_CAP = 50_000


@celery_app.task(name="worker.tasks.harvest_wallet_sightings", queue="periodic")
def harvest_wallet_sightings() -> dict:
    from core.db import get_supabase, list_tracked_wallets
    from core.polymarket import fetch_whale_trades, get_fast_markets

    fast = get_fast_markets()
    if not fast:
        return {"skipped": "no_fast_markets"}

    trades = fetch_whale_trades(min_usdc=settings.scout_min_trade_usdc)
    if not trades:
        return {"sightings": 0}

    tracked = {(w.get("address") or "").lower()
               for w in list_tracked_wallets(active_only=False)}

    rows: list[dict] = []
    for t in trades:
        tx = t.get("tx_hash") or ""
        wallet = (t.get("whale_wallet") or "").lower()
        cond = t.get("condition_id") or ""
        if not tx or tx in _seen_tx:
            continue
        _seen_tx.add(tx)
        if (t.get("side") != "BUY" or not wallet or wallet in tracked
                or cond not in fast):
            continue
        ts = int(t.get("timestamp") or 0)
        rows.append({
            "wallet": wallet,
            "tx_hash": tx,
            "condition_id": cond,
            "token_id": t.get("token_id") or None,
            "outcome": t.get("outcome") or None,
            "price": float(t.get("price") or 0),
            "size_usdc": float(t.get("size_usdc") or 0),
            "title": (t.get("title") or "")[:200] or None,
            "traded_at": (
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                if ts else None
            ),
        })

    if len(_seen_tx) > _SEEN_TX_CAP:
        _seen_tx.clear()

    if rows:
        try:
            get_supabase().table("wallet_sightings").upsert(
                rows, on_conflict="tx_hash", ignore_duplicates=True
            ).execute()
            log.info("sightings_recorded", count=len(rows))
        except Exception:
            log.exception("sightings_insert_failed", count=len(rows))
    return {"sightings": len(rows)}


def _fetch_sightings_14d(sb) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    rows: list[dict] = []
    off = 0
    while True:
        chunk = (
            sb.table("wallet_sightings")
            .select("wallet,condition_id,outcome,size_usdc")
            .gte("created_at", since)
            .order("id")
            .range(off, off + 999)
            .execute()
            .data or []
        )
        rows += chunk
        if len(chunk) < 1000:
            break
        off += 1000
    return rows


@celery_app.task(name="worker.tasks.score_donor_candidates", queue="periodic")
def score_donor_candidates() -> dict:
    from core.db import add_tracked_wallet, get_supabase, list_tracked_wallets
    from core.donor_scout import (
        candidate_qualifies,
        laplace_score,
        resolve_winning_outcomes,
        tally_outcomes,
    )
    from core.wallet_discovery import _activity_profile

    sb = get_supabase()

    # Retention: the scout only ever looks 14d back; 30d keeps audit headroom.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        sb.table("wallet_sightings").delete().lt("created_at", cutoff).execute()
    except Exception:
        log.warning("sightings_prune_failed")

    sightings = _fetch_sightings_14d(sb)
    by_wallet: dict[str, list[dict]] = {}
    for s in sightings:
        by_wallet.setdefault(s["wallet"], []).append(s)

    tracked = {(w.get("address") or "").lower()
               for w in list_tracked_wallets(active_only=False)}
    pool = {
        w: ss for w, ss in by_wallet.items()
        if len(ss) >= settings.scout_min_sightings and w not in tracked
    }
    if not pool:
        log.info("scout_score_empty_pool", sightings=len(sightings),
                 wallets_seen=len(by_wallet))
        return {"scored": 0}

    all_conds = [s["condition_id"] for ss in pool.values() for s in ss]
    winners = resolve_winning_outcomes(all_conds)

    scored = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for wallet, ss in pool.items():
        profile = _activity_profile(wallet)
        time.sleep(0.05)  # Data-API politeness between per-wallet pulls
        ok, reason = candidate_qualifies(
            profile,
            min_directionality=settings.discovery_min_directionality,
            max_trades_per_day=settings.discovery_max_trades_per_day,
            min_avg_trade_size=settings.discovery_min_avg_trade_size,
            max_event_outcomes=settings.discovery_max_event_outcomes,
        )
        resolved, wins = tally_outcomes(ss, winners)
        volume = sum(float(s.get("size_usdc") or 0) for s in ss)
        score = laplace_score(wins, resolved) if ok else 0.0
        payload = {
            "wallet": wallet,
            "sightings_14d": len(ss),
            "volume_14d": round(volume, 2),
            "avg_trade_usdc": round(volume / len(ss), 2),
            "resolved_count": resolved,
            "resolved_wins": wins,
            "directionality": profile.get("directionality"),
            "trades_per_day": profile.get("trades_per_day"),
            "is_mm": not ok,
            "score": round(score, 4),
            "updated_at": now_iso,
        }
        try:
            # No 'status' column in the payload: upsert must never clobber a
            # promoted/dismissed/candidate status set by a human or enrollment.
            sb.table("donor_candidates").upsert(
                payload, on_conflict="wallet"
            ).execute()
            scored += 1
        except Exception:
            log.exception("candidate_upsert_failed", wallet=wallet[:10])
        if not ok:
            log.info("candidate_rejected", wallet=wallet[:10], reason=reason)

    enrolled = _enroll_probation(sb, add_tracked_wallet, list_tracked_wallets)
    log.info("scout_score_done", pool=len(pool), scored=scored,
             enrolled=enrolled, resolved_markets=len(winners))
    return {"scored": scored, "enrolled": enrolled}


def _enroll_probation(sb, add_tracked_wallet, list_tracked_wallets) -> int:
    """Fill free probation seats with the best fresh candidates. Only
    status='new' rows are eligible — a dismissed wallet stays dismissed."""
    seats = settings.scout_probation_slots - sum(
        1 for w in list_tracked_wallets()
        if (w.get("mode") or "default") == "candidate"
    )
    if seats <= 0:
        return 0
    try:
        rows = (
            sb.table("donor_candidates")
            .select("wallet,score,resolved_count")
            .eq("status", "new")
            .eq("is_mm", False)
            .gt("resolved_count", 0)
            .order("score", desc=True)
            .limit(seats)
            .execute()
            .data or []
        )
    except Exception:
        log.exception("probation_enroll_query_failed")
        return 0
    enrolled = 0
    for r in rows:
        try:
            add_tracked_wallet(r["wallet"], f"scout {r['wallet'][:6]}",
                               mode="candidate")
            sb.table("donor_candidates").update(
                {"status": "candidate"}
            ).eq("wallet", r["wallet"]).execute()
            enrolled += 1
            log.info("probation_enrolled", wallet=r["wallet"][:10],
                     score=r["score"])
        except Exception:
            log.exception("probation_enroll_failed", wallet=r["wallet"][:10])
    return enrolled


def _send_admin_digest(text: str, keyboard: list[list[dict]] | None) -> None:
    """Admin-bot push with optional inline keyboard (donor_guard.notify_admins
    can't carry buttons)."""
    import httpx

    from core.db import list_admins

    token = settings.telegram_admin_bot_token or settings.telegram_bot_token
    chat_ids = {settings.admin_telegram_id}
    try:
        chat_ids |= {int(a["telegram_id"]) for a in list_admins()}
    except Exception:
        log.warning("digest_admin_list_failed")
    payload: dict = {"text": text, "parse_mode": "HTML",
                     "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    for chat_id in chat_ids:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={**payload, "chat_id": chat_id},
                timeout=10.0,
            )
        except Exception:
            log.warning("digest_send_failed", chat_id=chat_id)


@celery_app.task(name="worker.tasks.donor_scout_digest", queue="periodic")
def donor_scout_digest() -> dict:
    from core.db import get_supabase, list_tracked_wallets
    from core.donor_scout import probation_pnl, resolve_winning_outcomes

    sb = get_supabase()
    wallets = list_tracked_wallets()
    candidates = [w for w in wallets
                  if (w.get("mode") or "default") == "candidate"]
    live = [w for w in wallets if (w.get("mode") or "default") == "default"]

    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    lines: list[str] = ["🔭 <b>Donor scout — недельный дайджест</b>"]
    keyboard: list[list[dict]] = []

    if candidates:
        lines.append("\n<b>Кандидаты на обкатке</b> (виртуальная ставка "
                     f"${settings.scout_probation_stake_usdc:.0f}):")
        for w in candidates:
            addr = (w.get("address") or "").lower()
            label = w.get("label") or f"{addr[:6]}…{addr[-4:]}"
            try:
                signals = (
                    sb.table("trade_signals")
                    .select("market_id,outcome,price")
                    .eq("source_wallet", addr)
                    .eq("probation", True)
                    .gte("created_at", since)
                    .limit(500)
                    .execute()
                    .data or []
                )
            except Exception:
                log.warning("digest_signals_fetch_failed", wallet=addr[:10])
                signals = []
            winners = resolve_winning_outcomes(
                [s["market_id"] for s in signals])
            st = probation_pnl(signals, winners,
                               settings.scout_probation_stake_usdc)
            wr = (f"{st['wins']}/{st['resolved']}"
                  if st["resolved"] else "нет резолвов")
            lines.append(
                f"\n🧪 <b>{label}</b>\n<code>{addr}</code>\n"
                f"   сигналов: {st['signals']} | исход: {wr} | "
                f"PnL: <b>${st['pnl']:+.2f}</b>"
            )
            keyboard.append([
                {"text": f"✅ В бой: {label[:16]}", "callback_data": f"dc:p:{addr}"},
                {"text": "🗑", "callback_data": f"dc:x:{addr}"},
            ])
    else:
        lines.append("\nНа обкатке никого нет — скаут ищет кандидатов.")

    # Retirement hints for live donors: signals are the donor's pulse; a
    # 14d-silent donor blocks nothing but deserves a look. Never auto-removed.
    stale: list[str] = []
    for w in live:
        addr = (w.get("address") or "").lower()
        try:
            recent = (
                sb.table("trade_signals").select("id")
                .eq("source_wallet", addr).gte("created_at", since)
                .limit(1).execute().data
            )
        except Exception:
            continue
        if not recent:
            stale.append(w.get("label") or f"{addr[:6]}…{addr[-4:]}")
    if stale:
        lines.append("\n💤 <b>Молчат 14 дней</b> (посмотреть вручную): "
                     + ", ".join(stale))

    _send_admin_digest("\n".join(lines), keyboard or None)
    log.info("scout_digest_sent", candidates=len(candidates),
             stale_live=len(stale))
    return {"candidates": len(candidates), "stale": len(stale)}
