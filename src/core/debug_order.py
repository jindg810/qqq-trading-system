
from src.broker.models import OrderRequest, OrderSide, OrderType, TimeInForce
from src.broker.longbridge import LongbridgeAdapter
from src.config import CONFIG

broker = LongbridgeAdapter()
broker.connect()

order = OrderRequest(
                symbol="QQQ.US",
                quantity=CONFIG["max_position_size"],
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                price=300.00
            )

print(f"提交订单: {order}")
#order_id = broker.submit_order(order)
#print(f"订单提交结果: order_id={order_id}")