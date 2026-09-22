"""
Фрагмент production-кода, обезличено.

StockService.deduct_from_purchases — списание товара со склада по FEFO
(First Expired, First Out) с блокировкой строк партий (SELECT ... FOR UPDATE),
чтобы два параллельных списания (например, два курьера) не ушли в минус.

Импорты внутренних модулей заменены на пояснения:
  Purchase          — ORM-модель партии закупки (remaining_quantity, expiry_date,
                      purchase_date, unit_cost, is_active, гибридное свойство is_expired)
  Product           — ORM-модель товара
  OperationType     — enum типов операций для журнала
  BusinessLogicError — базовое исключение бизнес-логики
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select

logger = logging.getLogger(__name__)


@dataclass
class StockDeduction:
    """Результат списания из одной партии."""
    purchase_id: UUID
    quantity: Decimal
    unit_cost: Decimal
    expiry_date: Optional[str] = None


class InsufficientStockError(BusinessLogicError):
    """Недостаточно товара на складе (единый класс для всего проекта)."""

    def __init__(self, message, *, product_name=None, requested=None, available=None):
        super().__init__(message)
        self.product_name = product_name
        self.requested = requested
        self.available = available


class StockService:
    async def deduct_from_purchases(
        self,
        product_id: UUID,
        quantity: Decimal,
        order_item_id: Optional[UUID] = None,
        *,
        include_expired: bool = False,
    ) -> list[StockDeduction]:
        """
        Списать quantity из закупок по FEFO (First Expired, First Out).

        Порядок списания:
        1. Сначала партии с ближайшим сроком годности
        2. При равных сроках — сначала более старые закупки

        include_expired: по умолчанию False — продажи и доставки не трогают
        просрочку; списания и ручные корректировки вызывают с True.

        Raises:
            InsufficientStockError: если недостаточно товара
        """
        # with_for_update() — блокировка строк партий, чтобы параллельные
        # списания не пересеклись
        conditions = [
            Purchase.product_id == product_id,
            Purchase.remaining_quantity > 0,
            Purchase.is_active == True,
        ]
        if not include_expired:
            conditions.append(~Purchase.is_expired)

        result = await self.session.execute(
            select(Purchase)
            .where(*conditions)
            .order_by(
                Purchase.expiry_date.asc().nullslast(),  # FEFO
                Purchase.purchase_date.asc(),            # при равных сроках — старые первыми
            )
            .with_for_update()
        )
        purchases = result.scalars().all()

        total_available = sum((p.remaining_quantity for p in purchases), Decimal("0"))
        if total_available < quantity:
            product = await self.session.get(Product, product_id)
            product_name = product.name if product else str(product_id)
            raise InsufficientStockError(
                f"Недостаточно товара '{product_name}': "
                f"требуется {quantity}, доступно {total_available}",
                product_name=product_name,
                requested=quantity,
                available=total_available,
            )

        remaining_to_deduct = quantity
        deductions: list[StockDeduction] = []

        for purchase in purchases:
            if remaining_to_deduct <= 0:
                break

            old_remaining = purchase.remaining_quantity
            deduct_amount = min(remaining_to_deduct, purchase.remaining_quantity)
            purchase.remaining_quantity -= deduct_amount

            if purchase.remaining_quantity <= 0:
                purchase.is_active = False  # пустая партия деактивируется

            deductions.append(StockDeduction(
                purchase_id=purchase.id,
                quantity=deduct_amount,
                unit_cost=purchase.unit_cost,
                expiry_date=str(purchase.expiry_date) if purchase.expiry_date else None,
            ))

            # Журнал: каждое списание отдельной записью
            await self._log_stock_operation(
                operation_type=OperationType.STOCK_DEDUCTION,
                product_id=product_id,
                quantity=deduct_amount,
                description=f"FEFO списание: {deduct_amount} из закупки",
                purchase_id=purchase.id,
                order_item_id=order_item_id,
                old_remaining=old_remaining,
                new_remaining=purchase.remaining_quantity,
            )

            remaining_to_deduct -= deduct_amount
            logger.debug(
                f"Deducted {deduct_amount} from purchase {purchase.id}, "
                f"remaining in batch: {purchase.remaining_quantity}"
            )

        # Пересчитать агрегированный остаток товара из партий
        await self.sync_stock(product_id)

        logger.info(f"Deducted {quantity} of product {product_id} from {len(deductions)} batches")
        return deductions
