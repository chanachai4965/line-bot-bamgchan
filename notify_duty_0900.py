#!/usr/bin/env python3
"""Render Cron Job 09.00 น. v6.8.7 — แจ้งเตือนเวรล่วงหน้า"""
import sys
from app import send_daily_duty_prealert

if __name__ == "__main__":
    success = send_daily_duty_prealert()
    print("09.00 duty prealert: sent" if success else "09.00 duty prealert: failed")
    sys.exit(0 if success else 1)
