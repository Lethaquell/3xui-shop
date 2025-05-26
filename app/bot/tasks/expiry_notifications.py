import logging
import time
from datetime import datetime

from aiogram.utils.i18n import lazy_gettext as __
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.services import NotificationService, VPNService
from app.db.models import User

logger = logging.getLogger(__name__)

# Time intervals for notifications (in milliseconds)
ONE_HOUR_MS = 60 * 60 * 1000  # 1 hour
ONE_DAY_MS = 24 * 60 * 60 * 1000  # 24 hours

# Cache for sent notifications to avoid duplicates
# Format: {user_id: {notification_type: timestamp}}
_notification_cache = {}


def _should_send_notification(user_id: int, notification_type: str) -> bool:
    """
    Check if notification should be sent based on cache and time remaining.
    Returns True if notification should be sent, False otherwise.
    
    Logic: Send only ONE notification per subscription period.
    - If 24h notification was sent, allow 1h notification later
    - If 1h notification was sent, don't send any more notifications
    - Don't send the same notification type twice
    """
    current_time = time.time()
    
    # Clean old cache entries (older than 48 hours)
    cutoff_time = current_time - (48 * 60 * 60)
    for uid in list(_notification_cache.keys()):
        user_cache = _notification_cache[uid]
        for ntype in list(user_cache.keys()):
            if user_cache[ntype] < cutoff_time:
                del user_cache[ntype]
        if not user_cache:
            del _notification_cache[uid]
    
    # Check if we already sent ANY notification for this user
    if user_id in _notification_cache:
        user_cache = _notification_cache[user_id]
        
        # If we already sent 1h notification, don't send anything
        if "1_hour" in user_cache:
            return False
            
        # If we already sent 24h notification and trying to send it again, don't send
        if notification_type == "24_hours" and "24_hours" in user_cache:
            return False
            
        # If we already sent 24h notification and trying to send 1h, allow it
        if notification_type == "1_hour" and "24_hours" in user_cache:
            return True
    
    return True


def _mark_notification_sent(user_id: int, notification_type: str) -> None:
    """Mark that notification was sent for this user."""
    if user_id not in _notification_cache:
        _notification_cache[user_id] = {}
    _notification_cache[user_id][notification_type] = time.time()


async def check_subscription_expiry(
    session_factory: async_sessionmaker,
    vpn_service: VPNService,
    notification_service: NotificationService,
) -> None:
    """
    Check user subscriptions and send notifications about upcoming expiry.
    Notifications are sent 24 hours and 1 hour before expiration.
    """
    logger.info("[Background check] Starting subscription expiry check...")
    
    current_time = time.time() * 1000  # Current time in milliseconds
    
    logger.debug("[Background check] Current time: %d", current_time)
    
    session: AsyncSession
    async with session_factory() as session:
        # Get all users with active subscriptions
        query = select(User).where(User.server_id.isnot(None))
        result = await session.execute(query)
        users_with_subscriptions = result.scalars().all()
        
        if not users_with_subscriptions:
            logger.info("[Background check] No users with active subscriptions found.")
            return
        
        logger.info("[Background check] Checking %d users with subscriptions.", len(users_with_subscriptions))
        
        notifications_sent = 0
        
        for user in users_with_subscriptions:
            try:
                # Get VPN client data
                client_data = await vpn_service.get_client_data(user)
                
                if not client_data or client_data.has_subscription_expired:
                    # Skip users without data or with expired subscription
                    continue
                
                # Get subscription expiry time
                expiry_time = client_data.expiry_time_ms
                
                if expiry_time == -1:
                    # Unlimited subscription, skip
                    logger.debug("[Background check] User %d has unlimited subscription, skipping", user.tg_id)
                    continue
                
                # Calculate time differences for debugging
                time_until_expiry = expiry_time - current_time
                hours_until_expiry = time_until_expiry / (60 * 60 * 1000)
                
                logger.debug("[Background check] User %d: expiry_time=%d, current_time=%d, hours_until_expiry=%.2f", 
                           user.tg_id, expiry_time, current_time, hours_until_expiry)
                
                # Send notifications based on remaining time ranges
                # 1 hour notification: when less than 2 hours but more than 30 minutes remaining
                if 0.5 < hours_until_expiry <= 2.0:
                    if _should_send_notification(user.tg_id, "1_hour"):
                        await _send_expiry_notification(
                            notification_service=notification_service,
                            user=user,
                            notification_type="1_hour"
                        )
                        _mark_notification_sent(user.tg_id, "1_hour")
                        notifications_sent += 1
                        logger.info("[Background check] Sent 1-hour notification to user %d (%.2f hours remaining)", user.tg_id, hours_until_expiry)
                    else:
                        logger.debug("[Background check] Skipped 1-hour notification for user %d (already sent recently)", user.tg_id)
                
                # 24 hour notification: when less than 25 hours but more than 2 hours remaining  
                elif 2.0 < hours_until_expiry <= 25.0:
                    if _should_send_notification(user.tg_id, "24_hours"):
                        await _send_expiry_notification(
                            notification_service=notification_service,
                            user=user,
                            notification_type="24_hours"
                        )
                        _mark_notification_sent(user.tg_id, "24_hours")
                        notifications_sent += 1
                        logger.info("[Background check] Sent 24-hour notification to user %d (%.2f hours remaining)", user.tg_id, hours_until_expiry)
                    else:
                        logger.debug("[Background check] Skipped 24-hour notification for user %d (already sent recently)", user.tg_id)
                    
            except Exception as exception:
                logger.error("[Background check] Error checking user %d: %s", user.tg_id, exception)
                continue
        
        logger.info("[Background check] Expiry check completed. Sent %d notifications.", notifications_sent)


async def _send_expiry_notification(
    notification_service: NotificationService,
    user: User,
    notification_type: str,
) -> None:
    """
    Send expiry notification to user about upcoming subscription expiration.
    
    Args:
        notification_service: Notification service
        user: User database object
        notification_type: Notification type ("1_hour" or "24_hours")
    """
    try:
        # Use lazy gettext for localization
        if notification_type == "1_hour":
            text = __("expiry:notification:1_hour")
        elif notification_type == "24_hours":
            text = __("expiry:notification:24_hours")
        else:
            logger.error("Unknown notification type: %s", notification_type)
            return
        
        await notification_service.notify_by_id(
            chat_id=user.tg_id,
            text=str(text)
        )
        
    except Exception as exception:
        logger.error("Failed to send expiry notification to user %d: %s", user.tg_id, exception)


def start_scheduler(
    session_factory: async_sessionmaker,
    vpn_service: VPNService,
    notification_service: NotificationService,
) -> None:
    """
    Start scheduler for subscription expiry checks.
    Check is performed every 30 minutes.
    """
    scheduler = AsyncIOScheduler()
    
    scheduler.add_job(
        check_subscription_expiry,
        "interval",
        minutes=30,  # Check every 30 minutes
        args=[session_factory, vpn_service, notification_service],
        next_run_time=datetime.now(),  # First run immediately
        id="subscription_expiry_check",
        replace_existing=True,
    )
    
    scheduler.start()
