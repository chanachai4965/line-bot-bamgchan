#!/usr/bin/env python3
"""Render Cron Job: ส่งแจ้งเตือนเวรวันนี้"""
import os
import sys

from app import send_daily_duty_notification


def main() -> int:
    target = os.environ.get("DUTY_NOTIFY_TARGET", "").strip()
    if not target:
        print("ERROR: กรุณาตั้ง DUTY_NOTIFY_TARGET")
        return 1

    return 0 if send_daily_duty_notification(target) else 1


if __name__ == "__main__":
    sys.exit(main())
