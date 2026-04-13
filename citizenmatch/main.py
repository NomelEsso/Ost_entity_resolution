"""
Flask entry point for the CitizenMatch Cloud Run service.

Routes:
  POST /  → run the full pipeline
  GET  /  → health check
"""

import logging
import os
import sys

from flask import Flask, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Logging — Cloud Run captures stdout/stderr automatically
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


@app.route("/", methods=["POST"])
def run():
    """Trigger the full CitizenMatch pipeline."""
    try:
        log.info("Pipeline triggered via POST /")

        from pipeline import run_pipeline
        run_pipeline()

        log.info("Pipeline completed successfully.")
        return jsonify({"status": "success"}), 200

    except Exception as e:
        log.exception("Pipeline failed")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)