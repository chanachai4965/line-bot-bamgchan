#!/usr/bin/env python3
"""Render Cron Job 09.30 น. v6.8"""
import sys
from app import send_daily_duty_notification

if __name__ == "__main__":
    sys.exit(0 if send_daily_duty_notification() else 1)
