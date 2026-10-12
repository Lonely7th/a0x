"""Install the virtual portfolio terms selected on 2026-10-12.

Existing virtual fills and positions keep their historical settings version.
Untriggered plans for the current day are repriced with the new fee schedule.
"""

from __future__ import annotations

from assistant.config import Config, now_cn, now_utc_iso
from assistant.money import planned_shares, yuan
from assistant.storage import append_event, connect, setting_for, transaction


def main() -> None:
    config = Config.from_env()
    day = now_cn().date().isoformat()
    with connect(config.state_db) as db, transaction(db):
        existing = db.execute(
            "SELECT id FROM settings WHERE effective_date=?", (day,)
        ).fetchone()
        if existing:
            raise RuntimeError(f"Settings version already exists for {day}")
        prior = setting_for(db, day)
        cursor = db.execute("""
            INSERT INTO settings VALUES(NULL,?,?,?,?,?,?,?,?,?)
        """, (day, 100_000_000, 7_500_000, "0.00025",
              prior["minimum_commission"], prior["transfer_rate"],
              prior["stamp_rate"], "0", now_utc_iso()))
        new_id = cursor.lastrowid
        current = setting_for(db, day)
        plans = db.execute("""
            SELECT id,buy_limit_cents,order_budget_cents FROM watchlist
            WHERE entry_date=? AND status='watching'
        """, (day,)).fetchall()
        for plan in plans:
            budget = plan["order_budget_cents"] or current["budget_cents"]
            shares = planned_shares(yuan(plan["buy_limit_cents"]), budget, current)
            db.execute("""
                UPDATE watchlist SET settings_id=?,planned_shares=? WHERE id=?
            """, (new_id, shares, plan["id"]))
        append_event(db, f"virtual_settings_migrated:{day}",
                     "virtual_settings_migrated", None, None, None,
                     {"previous_settings_id": prior["id"],
                      "new_settings_id": new_id,
                      "repriced_pending_orders": len(plans)})
    print(f"Installed virtual settings {new_id}; repriced {len(plans)} pending orders")


if __name__ == "__main__":
    main()
