"""Convenience entry point describing runnable AlphaLab commands."""
import logging
from alpha_lab.utils.logging import configure_logging


def main() -> None:
    configure_logging()
    logging.getLogger(__name__).info("Use `streamlit run app/dashboard/main.py` to start AlphaLab")


if __name__ == "__main__":
    main()
