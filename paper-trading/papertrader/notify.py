"""Telegram alerts. Credentials come from env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Without credentials (or with --dry-run) messages are printed to the console instead.
"""
import html
import logging
import os

import requests

from .engine import Event

log = logging.getLogger(__name__)
LIMIT = 4000  # Telegram hard limit is 4096 chars


def fmt_price(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    return f"{x:.6f}"


def _side(side: int) -> str:
    return "LONG" if side == 1 else "SHORT"


class Notifier:
    def __init__(self, token: str | None = None, chat_id: str | None = None, dry_run: bool = False):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.dry_run = dry_run or not (self.token and self.chat_id)
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        chunks = [text[i:i + LIMIT] for i in range(0, len(text), LIMIT)] or [""]
        ok = True
        for chunk in chunks:
            self.sent.append(chunk)
            if self.dry_run:
                print(f"[telegram dry-run]\n{chunk}\n")
                continue
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=15,
                )
                if not r.ok:
                    ok = False
                    log.error("Telegram error %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as exc:
                ok = False
                log.error("Telegram request failed: %s", exc)
        return ok

    def event(self, ev: Event, account_summary: str = "") -> bool:
        return self.send(format_event(ev, account_summary))


def format_event(ev: Event, account_summary: str = "") -> str:
    if ev.kind == "entry":
        p = ev.position
        return (
            f"🧪 <b>PAPER {'🟢' if p.side == 1 else '🔴'} {_side(p.side)} {html.escape(p.symbol)}</b>\n"
            f"Strategy: <code>{p.strategy}</code> · {p.asset_class} · {p.timeframe}\n"
            f"Entry: {fmt_price(p.entry_price)}\n"
            f"Stop: {fmt_price(p.stop)} · Target: {fmt_price(p.target)}\n"
            f"Size: {p.qty:.6g} · Risk: ${p.risk_amount:,.2f}\n"
            f"Time: {p.entry_time}"
        )
    t = ev.trade
    win = t.pnl > 0
    head = "✅ <b>WIN</b>" if win else "❌ <b>LOSS</b>"
    msg = (
        f"🧪 {head} · PAPER {_side(t.side)} {html.escape(t.symbol)}\n"
        f"Strategy: <code>{t.strategy}</code> · {t.asset_class} · {t.timeframe}\n"
        f"{fmt_price(t.entry_price)} → {fmt_price(t.exit_price)} ({t.exit_reason})\n"
        f"P&L: <b>{'+' if t.pnl >= 0 else '-'}${abs(t.pnl):,.2f}</b> · {t.r_multiple:+.2f}R · {t.return_pct:+.2f}%\n"
        f"Held: {t.entry_time} → {t.exit_time}"
    )
    if account_summary:
        msg += f"\n{account_summary}"
    return msg
