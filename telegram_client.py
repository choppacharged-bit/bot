# -*- coding: utf-8 -*-
"""
Клиент для работы с Telegram API.
"""

import requests
from typing import Optional, Dict, Any
from logger import setup_logger, log_extra
from utils import clean_token
from models import TelegramMessage

log = setup_logger("telegram_client")

TELEGRAM_API_BASE = "https://api.telegram.org/bot"


class TelegramClient:
    """Клиент для отправки сообщений в Telegram."""
    
    def __init__(self, bot_token: str, timeout: float = 10.0):
        """Инициализирует клиент.
        
        Args:
            bot_token: Токен бота Telegram
            timeout: Таймаут для запросов в секундах
        """
        self.bot_token = clean_token(bot_token)
        self.timeout = timeout
        
        if not self.bot_token:
            raise ValueError("Invalid Telegram bot token")
    
    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Отправляет сообщение в Telegram.
        
        Args:
            chat_id: ID чата
            text: Текст сообщения
            reply_markup: Клавиатура (опционально)
        
        Returns:
            True если успешно, False иначе
        """
        try:
            if not text or len(text) > 4096:
                log.error(f"Invalid message length: {len(text) if text else 0}")
                return False
            
            url = f"{TELEGRAM_API_BASE}{self.bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
            }
            
            if reply_markup:
                payload["reply_markup"] = reply_markup
            
            response = requests.post(url, json=payload, timeout=self.timeout)
            
            if response.status_code == 200:
                log_extra(
                    log,
                    "INFO",
                    "Message sent successfully",
                    chat_id=chat_id,
                    text_length=len(text),
                )
                return True
            else:
                log.error(
                    f"Telegram API error ({response.status_code}): {response.text}"
                )
                return False
                
        except requests.exceptions.Timeout:
            log.error(f"Telegram API timeout for chat {chat_id}")
            return False
        except requests.exceptions.ConnectionError as e:
            log.error(f"Telegram connection error: {str(e)}")
            return False
        except Exception as e:
            log.error(f"Unexpected error sending Telegram message: {str(e)}", exc_info=True)
            return False
    
    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """Отвечает на inline кнопку.
        
        Args:
            callback_query_id: ID callback query
            text: Текст уведомления (опционально)
            show_alert: Показать как alert (опционально)
        
        Returns:
            True если успешно, False иначе
        """
        try:
            url = f"{TELEGRAM_API_BASE}{self.bot_token}/answerCallbackQuery"
            payload = {
                "callback_query_id": callback_query_id,
            }
            
            if text:
                payload["text"] = text
            if show_alert:
                payload["show_alert"] = True
            
            response = requests.post(url, json=payload, timeout=self.timeout)
            
            if response.status_code == 200:
                return True
            else:
                log.error(f"Callback answer error ({response.status_code}): {response.text}")
                return False
                
        except Exception as e:
            log.error(f"Error answering callback: {str(e)}", exc_info=True)
            return False
