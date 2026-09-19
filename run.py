"""本地启动入口：python run.py 后访问 http://127.0.0.1:8000/docs"""
from __future__ import annotations

import argparse

import uvicorn

from app.db import connect, init_db
from app.api import app, DB_PATH
from app.seed import seed

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--seed", action="store_true", help="写入演示数据")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import app.api as api_mod
    api_mod.DB_PATH = args.db
    conn = connect(args.db)
    init_db(conn)
    if args.seed:
        seed(conn)
    app.state.conn = conn

    uvicorn.run(app, host="127.0.0.1", port=args.port)
