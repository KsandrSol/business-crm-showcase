# Фрагмент services/telegram_sync_service.py (слой чистой логики, без telethon)
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import CustomerInteraction


@dataclass(slots=True)
class IncomingTgMessage:
    """
    Нейтральная (не зависящая от telethon) обёртка входящего сообщения.

    Telethon-обёртка конвертирует Message в этот dataclass, дальше вся
    бизнес-логика работает только с ним — поэтому она тестируется без telethon.
    """

    message_id: int
    peer_user_id: int | None   # Telegram user ID собеседника
    username: str | None       # с '@' или без, нормализуется при резолве
    text: str | None           # None для медиа без подписи / сервисных событий
    date: datetime             # UTC, naive — под текущую схему БД
    is_outgoing: bool          # True — исходящее, False — входящее


async def is_duplicate(
    session: AsyncSession,
    customer_id: str,
    telegram_message_id: int,
) -> bool:
    """
    Проверить, залогировано ли уже это TG-сообщение у данного контрагента.

    Дедупликация по паре (customer_id, telegram_message_id) — поле
    проиндексировано. Делает синхронизацию идемпотентной: повторный
    прогон истории не плодит дубли.
    """
    result = await session.execute(
        select(CustomerInteraction.id)
        .where(
            CustomerInteraction.customer_id == str(customer_id),
            CustomerInteraction.telegram_message_id == telegram_message_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None
