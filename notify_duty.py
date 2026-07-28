#!/usr/bin/env python3
"""Render Cron Job compatibility entry point — แจ้งรายชื่อเวรเวลา 09.30 น."""
import sys

from app import send_daily_duty_notification


def main() -> int:
    success = send_daily_duty_notification()
    print("09.30 duty notification: sent" if success else "09.30 duty notification: failed")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
