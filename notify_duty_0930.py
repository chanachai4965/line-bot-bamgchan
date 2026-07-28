#!/usr/bin/env python3
"""Render Cron Job 09.30 น. v6.8.7 — แจ้งรายชื่อเวรประจำวัน"""
import sys
from app import send_daily_duty_notification

if __name__ == "__main__":
    success = send_daily_duty_notification()
    print("09.30 duty notification: sent" if success else "09.30 duty notification: failed")
    sys.exit(0 if success else 1)
