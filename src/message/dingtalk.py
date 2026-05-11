#!/usr/bin/env python3
"""钉钉机器人通知实现（支持加签/限流/重试）"""
import time, hmac, hashlib, base64, urllib.parse, requests, logging

from .base import BaseNotifier, register_notifier

logger = logging.getLogger("message.dingtalk")

@register_notifier("dingtalk")
class DingTalkNotifier(BaseNotifier):
    MAX_RETRIES = 3
    RATE_LIMIT_ERRCODE = 310000  # 钉钉频限错误码

    def __init__(self, webhook: str, secret: str | None = None):
        if not webhook.startswith("https://oapi.dingtalk.com/robot/send"):
            raise ValueError("无效的钉钉 Webhook 地址")
        self.webhook = webhook
        self.secret = secret
        self._last_send_time = 0.0
        self._send_interval = 1.5  # 基础间隔：1秒（钉钉限制20条/分钟）

    def _build_signed_url(self) -> str:
        if not self.secret: return self.webhook
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook}&timestamp={timestamp}&sign={sign}"

    def _rate_limit_wait(self):
        """平滑频限：保证间隔 >= 1.5秒"""
        elapsed = time.time() - self._last_send_time
        if elapsed < self._send_interval:
            time.sleep(self._send_interval - elapsed)
        self._last_send_time = time.time()

    def send(self, title: str, content: str) -> bool:
        self._rate_limit_wait()
        url = self._build_signed_url()
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": content}}

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(url, json=payload, timeout=5)
                resp.raise_for_status()
                result = resp.json()
                errcode = result.get("errcode", 0)
                
                if errcode == 0: return True
                if errcode == self.RATE_LIMIT_ERRCODE:
                    logger.warning("⚠️ 触发钉钉频限，等待 2 秒重试...")
                    time.sleep(2)
                    continue
                logger.error(f"钉钉返回错误: {result}")
                return False
            except requests.RequestException as e:
                if attempt == self.MAX_RETRIES - 1:
                    logger.error(f"钉钉请求失败 (重试{attempt+1}次): {e}")
                    return False
                time.sleep(1 * (attempt + 1))  # 指数退避
        return False
    