#!/usr/bin/env python3
"""Render Cron Job 09.00 น. v6.8"""
import sys
from app import send_daily_duty_prealert

if __name__ == "__main__":
    sys.exit(0 if send_daily_duty_prealert() else 1)
