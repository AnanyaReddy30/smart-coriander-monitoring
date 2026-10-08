"""Start the Smart Coriander dashboard.

    python run.py            keep previously replayed data
    python run.py --fresh    start with an empty database
    python run.py --port 8000
"""
import argparse

from app.server import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="delete stored readings before starting")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app = create_app(fresh=args.fresh)
    print("Open http://%s:%d" % (args.host, args.port))
    app.run(host=args.host, port=args.port, debug=False)
