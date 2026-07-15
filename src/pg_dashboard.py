#!/usr/bin/env python3
"""Postgres-backed trading dashboard — reads directly from docker.klo:5433."""
import json
import os
import psycopg2, psycopg2.extras
from flask import Flask, jsonify

app = Flask(__name__)
PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")

TRADER_META = [
    {"id": "trader-kairos",   "name": "Kairós Capital",     "manager": "Zara Chen"},
    {"id": "trader-aldridge", "name": "Aldridge & Partners", "manager": "Edmund Whitfield"},
    {"id": "trader-stonks",   "name": "Stonks Capital",      "manager": "Stan Hoolihan"},
]

def get_db():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn

@app.route("/api/summary")
def api_summary():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    traders = []
    for t in TRADER_META:
        tid = t["id"]
        cur.execute(
            "SELECT count(*) as total, coalesce(sum(pnl), 0) as pnl "
            "FROM trading.executed_trades WHERE agent_id = %s", (tid,))
        trades = cur.fetchone()
        cur.execute(
            "SELECT count(*) as today FROM trading.decisions "
            "WHERE agent_id = %s AND timestamp > now() - interval '12 hours'", (tid,))
        decisions = cur.fetchone()
        traders.append({
            **t,
            "total_trades": trades["total"],
            "total_pnl": round(float(trades["pnl"]), 2),
            "decisions_today": decisions["today"],
        })
    conn.close()
    return jsonify(traders)

@app.route("/api/trades")
def api_trades():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT agent_id, ticker, action, quantity, entry_price, exit_price, "
        "pnl, entry_time, exit_time, status "
        "FROM trading.executed_trades "
        "WHERE entry_time > now() - interval '7 days' "
        "ORDER BY entry_time DESC LIMIT 200")
    rows = cur.fetchall()
    conn.close()
    return jsonify([{**r, "pnl": round(float(r["pnl"]), 2) if r["pnl"] else None,
                     "entry_price": round(float(r["entry_price"]), 2),
                     "exit_price": round(float(r["exit_price"]), 2) if r["exit_price"] else None}
                    for r in rows])

@app.route("/api/decisions")
def api_decisions():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT agent_id, ticker, action, conviction, thesis, timestamp "
        "FROM trading.decisions "
        "WHERE timestamp > now() - interval '24 hours' "
        "ORDER BY timestamp DESC LIMIT 100")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/pnl")
def api_pnl():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT agent_id, date(entry_time) as day, "
        "count(*) as trades, round(sum(coalesce(pnl,0))::numeric, 2) as pnl "
        "FROM trading.executed_trades "
        "WHERE entry_time > now() - interval '30 days' "
        "GROUP BY 1,2 ORDER BY 2 desc, 1")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/health")
def health():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM trading.executed_trades")
    trades = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM trading.decisions WHERE timestamp > now() - interval '1 hour'")
    recent = cur.fetchone()[0]
    conn.close()
    return jsonify({"status": "ok", "total_trades": trades, "decisions_last_hour": recent})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004, debug=False)