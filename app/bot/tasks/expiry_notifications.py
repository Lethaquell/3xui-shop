import logging
import time
from datetime import datetime, timedelta

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


async def check_subscription_expiry(
    session_factory: async_sessionmaker,
    vpn_service: VPNService,
    notification_service: NotificationService,
) -> None:
    """
    Check user subscriptions and send notifications about upcoming expiry.
    Notifications are sent 24 hours and 1 hour before expiration.
    """
    logger.info("[Expiry Check] Starting subscription expiry check...")
    
    current_time = time.time() * 1000  # Current time in milliseconds
    one_hour_later = current_time + ONE_HOUR_MS
    one_day_later = current_time + ONE_DAY_MS
    
    # Add small buffer to avoid duplicate notifications
    buffer_time = 30 * 60 * 1000  # 30 minutes buffer
    
    session: AsyncSession
    async with session_factory() as session:
        # Get all users with active subscriptions
        query = select(User).where(User.server_id.isnot(None))
        result = await session.execute(query)
        users_with_subscriptions = result.scalars().all()
        
        if not users_with_subscriptions:
            logger.info("[Expiry Check] No users with active subscriptions found.")
            return
        
        logger.info("[Expiry Check] Checking %d users with subscriptions.", len(users_with_subscriptions))
        
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
                    continue
                
                # Check if we need to send 1 hour notification
                if (one_hour_later - buffer_time <= expiry_time <= one_hour_later + buffer_time):
                    await _send_expiry_notification(
                        notification_service=notification_service,
                        user=user,
                        notification_type="1_hour"
                    )
                    notifications_sent += 1
                    logger.info("[Expiry Check] Sent 1-hour notification to user %d", user.tg_id)
                
                # Check if we need to send 24 hour notification
                elif (one_day_later - buffer_time <= expiry_time <= one_day_later + buffer_time):
                    await _send_expiry_notification(
                        notification_service=notification_service,
                        user=user,
                        notification_type="24_hours"
                    )
                    notifications_sent += 1
                    logger.info("[Expiry Check] Sent 24-hour notification to user %d", user.tg_id)
                    
            except Exception as exception:
                logger.error("[Expiry Check] Error checking user %d: %s", user.tg_id, exception)
                continue
        
        logger.info("[Expiry Check] Completed. Sent %d notifications.", notifications_sent)


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
        next_run_time=datetime.now() + timedelta(minutes=1),  # First run after 1 minute
        id="subscription_expiry_check",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("Subscription expiry notification scheduler started (30-minute interval)")
