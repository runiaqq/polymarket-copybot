from worker.tasks.ai_filter import run_ai_analysis
from worker.tasks.donor_refresh import deactivate_underperforming_donors, refresh_donor_stats
from worker.tasks.execute_copy import execute_copy_trade
from worker.tasks.manage_positions import close_position, sync_positions
from worker.tasks.monitor_deposits import monitor_deposits
from worker.tasks.poll_donors import poll_donor_trades
from worker.tasks.poll_tracked_wallets import poll_tracked_wallets
from worker.tasks.poll_sniper_wallets import poll_sniper_wallets
from worker.tasks.scan_markets import dispatch_signal, scan_whale_trades
from worker.tasks.subscriptions import check_subscription_expiry
from worker.tasks.wallet_ops import withdraw_funds, wrap_collateral

__all__ = [
    "execute_copy_trade",
    "run_ai_analysis",
    "scan_whale_trades",
    "dispatch_signal",
    "sync_positions",
    "close_position",
    "check_subscription_expiry",
    "wrap_collateral",
    "withdraw_funds",
    "poll_donor_trades",
    "poll_tracked_wallets",
    "poll_sniper_wallets",
    "monitor_deposits",
    "refresh_donor_stats",
    "deactivate_underperforming_donors",
]
