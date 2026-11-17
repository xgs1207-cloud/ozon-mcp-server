"""Nightly elastic-boosting maintenance for all Ozon shops in the MCP.

Run by a scheduled task every day ~00:00 Asia/Shanghai (see docs/NIGHTLY-TASK.md).

Per shop:
  1. ENROLL: products listed in the last 7 days (offer_id xzj.M.D.n, Asia/Shanghai
     date) that are candidates of the Elastic Boosting action and not yet
     participating -> enrol at half of current price via the MCP's updated
     actions_products_activate (POST /v1/actions/products/update, with
     pre-validation against the action limit).
  2. OTHER ACTIONS: for every other Ozon action (not Elastic Boosting), find
     participating products that Ozon auto-added (add_mode != SELLER),
     remove them via actions_products_deactivate, and restore the product's
     price to its last pre-promotion transaction price (latest FBS order's
     financial_data old_price within the last 30 days).
     (The planned auto-add list endpoint 404s for these boosting actions, so
     detection relies on add_mode in the participating scan.)
  3. Report everything; never touch products the seller added deliberately.

Paths resolve relative to this repo (no hardcoded home directory):
  - MCP code: <repo>/src, shop keys: <repo>/data (or $OZON_MCP_DATA_DIR)
  - Logs + pending restores: <repo>/logs/nightly-elastic (or $NIGHTLY_LOG_DIR)
Secrets: shop keys are loaded from the MCP's own encrypted shops.json via
ozon_mcp.settings (decrypted in memory only, never printed or written out).

Usage:
  python3 scripts/nightly_elastic.py --dry-run   # read-only preview
  python3 scripts/nightly_elastic.py --live      # performs writes
Run with the repo's virtualenv Python (it needs httpx/cryptography from the
MCP's dependencies), e.g. .venv/bin/python scripts/nightly_elastic.py --live
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

from ozon_mcp import settings as cfg
from ozon_mcp.client import OzonSellerClient

BASE = REPO_ROOT
DATA_DIR = os.environ.get("OZON_MCP_DATA_DIR", str(BASE / "data"))
LOG_DIR = os.environ.get("NIGHTLY_LOG_DIR", str(BASE / "logs" / "nightly-elastic"))
SHANGHAI = timezone(timedelta(hours=8))
OFFER_RE = re.compile(r"^xzj\.(\d{1,2})\.(\d{1,2})\.(\d+)$")


def recent_offer_dates(days=7):
    today = datetime.now(SHANGHAI).date()
    return {(today - timedelta(days=i)).strftime("%-m.%-d") for i in range(days)}


async def paged(fn, **kw):
    """Collect all pages of an actions list endpoint (last_id pagination)."""
    out, last = [], ""
    for _ in range(60):
        r = await fn(limit=100, last_id=last, **kw)
        prods = r.get("products") or (r.get("result") or {}).get("products") or []
        out.extend(prods)
        new = str(r.get("last_id") or (r.get("result") or {}).get("last_id") or "")
        if not new or new == last:
            break
        last = new
    return out


PENDING_FILE = os.path.join(LOG_DIR, "pending_price_restores.json")


def load_pending():
    try:
        return json.load(open(PENDING_FILE))
    except Exception:
        return []


def save_pending(items):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PENDING_FILE, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


async def try_set_price(client, offer_id, target, currency):
    """Set price; returns (ok, raw_result)."""
    r = await client.product_import_prices(
        [{"offer_id": offer_id, "price": str(target), "currency_code": currency}])
    ok = False
    try:
        ok = bool((r.get("result") or [{}])[0].get("updated"))
    except Exception:
        pass
    return ok, r


def money_amount(v):
    if isinstance(v, dict):
        v = v.get("amount", 0)
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0
    if isinstance(v, dict):
        v = v.get("amount", 0)
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


async def last_order_price(client, sku, days=30):
    """Latest FBS order (<=days) containing sku -> (offer_id, old_price, order_date)."""
    to = datetime.now(timezone.utc)
    since = to - timedelta(days=days)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cursor, best = "", None
    for _ in range(20):
        r = await client.posting_fbs_list(since.strftime(fmt), to.strftime(fmt), limit=50, cursor=cursor)
        for p in r.get("postings", []) or []:
            for prod in p.get("products", []) or []:
                if prod.get("sku") == sku:
                    fd = ((p.get("financial_data") or {}).get("products") or [{}])
                    f0 = next((x for x in fd if x.get("product_id") == sku), fd[0] if fd else {})
                    old = money_amount(f0.get("old_price") or f0.get("price"))
                    if old > 0:
                        best = (prod.get("offer_id"), old, p.get("in_process_at"))
        if not r.get("has_next"):
            break
        cursor = r.get("cursor") or ""
        if not cursor:
            break
    return best


async def process_shop(shop_id, shop, live, recent):
    client = OzonSellerClient(shop["ozon_client_id"], shop["ozon_api_key"])
    rep = {"shop": shop_id, "name": shop.get("name", shop_id),
           "enrolled": [], "enroll_failed": [], "removed": [],
           "price_restored": [], "price_skipped": [], "auto_add_deleted": [],
           "pending_retried": [], "still_in_action": [],
           "errors": []}
    # Retry price restores that failed on earlier runs.
    if live:
        pending = [p for p in load_pending() if p.get("shop") == shop_id]
        still = []
        for p in pending:
            try:
                cur = await client.product_info_prices(offer_id=[p["offer_id"]])
                items = cur.get("result", {}).get("items", []) or cur.get("items", [])
                cur_price, currency = 0.0, p.get("currency", "CNY")
                if items:
                    pr = items[0].get("price")
                    cur_price = money_amount(pr.get("price") if isinstance(pr, dict) else pr)
                    if isinstance(pr, dict) and pr.get("currency_code"):
                        currency = pr["currency_code"]
                if cur_price and abs(cur_price - p["target_price"]) < 0.005:
                    rep["pending_retried"].append({**p, "result": "already at target"})
                    continue
                ok, r = await try_set_price(client, p["offer_id"], p["target_price"], currency)
                if ok:
                    rep["pending_retried"].append({**p, "result": "restored"})
                else:
                    rep["errors"].append(f"pending price retry failed {p['offer_id']}: {r}")
                    still.append(p)
            except Exception as e:
                rep["errors"].append(f"pending price retry error {p.get('offer_id')}: {e}")
                still.append(p)
        keep = [p for p in load_pending() if p.get("shop") != shop_id] + still
        save_pending(keep)
    try:
        actions = await client.actions_list()
        alist = actions.get("result") or actions.get("actions") or []
        if isinstance(actions, list):
            alist = actions
        elastic = None
        others = []
        for a in alist:
            name = (a.get("title") or a.get("name") or "")
            aid = a.get("id") or a.get("action_id")
            if "ластич" in name:
                elastic = (aid, name)
            else:
                others.append((aid, name))
        rep["elastic_action"] = elastic
        rep["other_actions"] = others
        print(f"== {shop_id}: elastic={elastic} other_actions={len(others)}", flush=True)

        # ---- 1. ENROLL last-7-days candidates at half price ----
        if elastic:
            cands = await paged(client.actions_candidates, action_id=elastic[0])
            # BUGFIX 2026-11-17: candidate records carry NO offer_id/sku at all
            # (only id/price/limits) — the old code matched offer_id on the
            # candidate record itself, so enroll_candidates was always 0 and
            # nothing was ever enrolled. Resolve offer_id via product info.
            offers = {}
            ids = [c["id"] for c in cands]
            for i in range(0, len(ids), 100):
                try:
                    info = await client.product_info_list(ids[i:i + 100])
                    for it in (info.get("result", {}).get("items", []) or info.get("items", [])):
                        offers[it.get("id")] = it.get("offer_id")
                except Exception as e:
                    rep["errors"].append(f"product_info_list for candidates failed: {e}")
            todo = []
            for c in cands:
                offer_id = offers.get(c["id"]) or ""
                m = OFFER_RE.match(offer_id)
                if not m:
                    continue
                if f"{m.group(1)}.{m.group(2)}" not in recent:
                    continue
                price = money_amount(c.get("price"))
                if price <= 0:
                    continue
                todo.append({"product_id": c["id"], "action_price": round(price / 2, 2),
                             "offer_id": offer_id, "price": price})
            rep["enroll_candidates"] = len(todo)
            if todo:
                if live:
                    r = await client.actions_products_activate(elastic[0], [
                        {"product_id": t["product_id"], "action_price": t["action_price"]} for t in todo])
                    active = set(r.get("active_product_ids") or [])
                    for t in todo:
                        (rep["enrolled"] if t["product_id"] in active else rep["enroll_failed"]).append(t)
                    for rej in r.get("rejected") or []:
                        rep["errors"].append(f"enroll rejected {rej}")
                else:
                    rep["enrolled"] = todo
            print(f"   enroll: {len(todo)} candidates -> {[t['offer_id'] for t in todo]}", flush=True)

        # ---- 2. OTHER ACTIONS: remove Ozon auto-added products ----
        tomorrow = (datetime.now(SHANGHAI).date() + timedelta(days=1)).strftime("%Y-%m-%d")
        for aid, aname in others:
            if not aid:
                continue
            try:
                parts = await paged(client.actions_products, action_id=aid)
            except Exception as e:
                rep["errors"].append(f"action {aid} products scan failed: {e}")
                continue
            auto = [p for p in parts if (p.get("add_mode") or "SELLER") != "SELLER"]
            print(f"   action {aid} ({aname}): participating={len(parts)} auto-added={len(auto)}", flush=True)
            if auto and live:
                r = await client.actions_products_deactivate(aid, [p["id"] for p in auto])
                done = set((r.get("result") or {}).get("product_ids") or [])
                for p in auto:
                    if p["id"] in done:
                        rep["removed"].append({"action_id": aid, "action": aname,
                                               "product_id": p["id"], "sku": p.get("sku"),
                                               "offer_id": p.get("offer_id")})
                    else:
                        rep["errors"].append(f"remove failed action {aid} product {p['id']}")
            elif auto:
                rep["removed"].extend([{"action_id": aid, "action": aname, "product_id": p["id"],
                                        "sku": p.get("sku"), "offer_id": p.get("offer_id")} for p in auto])
            # NOTE: planned auto-add list endpoint (/v1/actions/auto-add/products/list)
            # returns 404 for these boosting actions (verified 2026-11-14) — detection
            # relies on add_mode in the participating scan instead.

        # ---- 3. RESTORE PRICE for removed products ----
        # Action records carry no sku/offer_id — resolve via product info first.
        idents = {}
        pids = sorted({it["product_id"] for it in rep["removed"]})
        for i in range(0, len(pids), 100):
            try:
                info = await client.product_info_list(pids[i:i + 100])
                for it in (info.get("result", {}).get("items", []) or info.get("items", [])):
                    idents[it.get("id")] = {"sku": it.get("sku"), "offer_id": it.get("offer_id")}
            except Exception as e:
                rep["errors"].append(f"product_info_list failed: {e}")
        # Dedupe: same product may be removed from several actions at once.
        seen_pids = set()
        for item in list(rep["removed"]):
            if item["product_id"] in seen_pids:
                continue
            seen_pids.add(item["product_id"])
            ident = idents.get(item["product_id"], {})
            item["sku"] = item.get("sku") or ident.get("sku")
            item["offer_id"] = item.get("offer_id") or ident.get("offer_id")
            sku = item.get("sku")
            if not sku:
                rep["price_skipped"].append({**item, "reason": "sku not resolvable"})
                continue
            got = await last_order_price(client, sku)
            if not got:
                rep["price_skipped"].append({**item, "reason": "no FBS order in 30d"})
                continue
            offer_id, old_price, odate = got
            cur = await client.product_info_prices(offer_id=[offer_id])
            cur_price, currency = 0.0, "CNY"
            try:
                items = cur.get("result", {}).get("items", []) or cur.get("items", [])
                if items:
                    # v5 shape: items[0]["price"] is a dict {"price": 18.8, "old_price": ...}
                    p = items[0].get("price")
                    cur_price = money_amount(p.get("price") if isinstance(p, dict) else p)
                    if isinstance(p, dict) and p.get("currency_code"):
                        currency = p["currency_code"]
            except Exception:
                pass
            entry = {**item, "restore_price": old_price, "order_date": odate,
                     "current_price": cur_price, "currency": currency}
            if cur_price and abs(cur_price - old_price) < 0.005:
                rep["price_skipped"].append({**entry, "reason": "already at that price"})
            elif live:
                ok, r = await try_set_price(client, offer_id, old_price, currency)
                entry["set_result"] = r
                if ok:
                    rep["price_restored"].append(entry)
                else:
                    rep["errors"].append(f"price set failed {offer_id} -> {old_price}: {r}")
                    rep.setdefault("price_pending", []).append(
                        {"shop": shop_id, "product_id": item["product_id"], "offer_id": offer_id,
                         "target_price": old_price, "currency": currency})
            else:
                rep["price_restored"].append(entry)
        # Verify removals: the deactivate response is unreliable (products removed
        # as a side effect of another action's removal are not listed) — re-scan.
        if live and rep["removed"]:
            by_action = {}
            for it in rep["removed"]:
                by_action.setdefault(it["action_id"], []).append(it["product_id"])
            for aid, pids in by_action.items():
                try:
                    parts = await paged(client.actions_products, action_id=aid)
                    present = {p["id"] for p in parts}
                    for pid in pids:
                        if pid in present:
                            rep["still_in_action"].append({"action_id": aid, "product_id": pid})
                except Exception as e:
                    rep["errors"].append(f"verify action {aid} failed: {e}")
        # Persist pending price restores for the next run.
        if live and rep.get("price_pending"):
            all_pending = load_pending()
            have = {(p.get("shop"), p.get("offer_id")) for p in all_pending}
            for p in rep["price_pending"]:
                if (p.get("shop"), p.get("offer_id")) not in have:
                    all_pending.append(p)
            save_pending(all_pending)
    except Exception as e:
        rep["errors"].append(f"shop failed: {e}")
    finally:
        await client.close()
    return rep


async def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--live", action="store_true")
    args = ap.parse_args()
    live = args.live
    recent = recent_offer_dates(7)
    print(f"mode={'LIVE' if live else 'DRY-RUN'} recent offer dates (Shanghai): {sorted(recent)}", flush=True)
    shops = cfg.load_shops(Path(DATA_DIR))
    print(f"shops: {list(shops)}", flush=True)
    reports = []
    for sid, shop in shops.items():
        if not shop.get("ozon_client_id") or not shop.get("ozon_api_key"):
            print(f"== {sid}: skipped (no Seller keys)", flush=True)
            continue
        reports.append(await process_shop(sid, shop, live, recent))
    out = {"mode": "live" if live else "dry-run",
           "run_at_shanghai": datetime.now(SHANGHAI).isoformat(), "reports": reports}
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now(SHANGHAI).strftime("%Y-%m-%d-%H%M")
    with open(os.path.join(LOG_DIR, f"nightly-elastic-{stamp}.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("=== SUMMARY ===")
    for r in reports:
        print(f"{r['shop']}: enrolled={len(r['enrolled'])} failed={len(r['enroll_failed'])} "
              f"removed={len(r['removed'])} price_restored={len(r['price_restored'])} "
              f"price_skipped={len(r['price_skipped'])} auto_add_deleted={len(r['auto_add_deleted'])} "
              f"errors={r['errors']}", flush=True)
    print(json.dumps(out, ensure_ascii=False)[:4000])


if __name__ == "__main__":
    asyncio.run(main())
